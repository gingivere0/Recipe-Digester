"""Phone inbox: turns a shared link, screenshot, or text into a library recipe.

serve-recipes.py accepts uploads at POST /inbox and hands them to this module.
Each upload becomes a job folder under inbox/. One worker thread runs the jobs
in order: it prepares the source (for links, it downloads the page and pulls
out the recipe data most sites embed), runs Claude Code headless
(`claude -p`) to rewrite it in the Nook format, and writes the result to
library/<category>/<category>--<slug>.md.

Uploads must carry the token from tools/inbox-token.txt (made on first run)
in an X-Inbox-Token header, so other devices on the LAN can't start runs.
"""
import html
import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.request
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
LIBRARY = os.path.join(ROOT, "library")
INBOX = os.path.join(ROOT, "inbox")
TOKEN_FILE = os.path.join(HERE, "inbox-token.txt")
CATEGORIES = ["breakfast", "mains", "sides", "soups", "baking", "desserts", "drinks", "misc"]
CLAUDE_TIMEOUT = 600
CLAUDE_MODEL = os.environ.get("RECIPE_INBOX_MODEL", "sonnet")  # cheaper and plenty for this
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")

IMAGE_TYPES = {b"\xff\xd8\xff": ".jpg", b"\x89PNG": ".png", b"GIF8": ".gif"}

_jobs = queue.Queue()
_on_recipe = None  # called after a recipe is written (rebuilds index.txt)


def token():
    if not os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "w") as f:
            f.write(secrets.token_urlsafe(12) + "\n")
    with open(TOKEN_FILE) as f:
        return f.read().strip()


# ---- accepting uploads ------------------------------------------------------

def sniff(body, content_type, filename=""):
    """Returns (kind, extension, data) for an upload. kind is url, text, image, or pdf."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct == "application/json":
        try:
            obj = json.loads(body.decode("utf-8-sig"))
        except ValueError:
            obj = {}
        if isinstance(obj, dict):
            for key in ("url", "text"):
                value = obj.get(key)
                if isinstance(value, list):
                    value = "\n".join(str(v) for v in value)
                if value and str(value).strip():
                    return sniff(str(value).encode("utf-8"), "text/plain")
    for magic, ext in IMAGE_TYPES.items():
        if body.startswith(magic):
            return "image", ext, body
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image", ".webp", body
    if body[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftyphevc"):
        return "image", ".heic", body
    if body.startswith(b"%PDF"):
        return "pdf", ".pdf", body
    if ct.startswith("image/"):
        return "image", "." + ct.split("/")[1], body
    text = body.decode("utf-8-sig", errors="replace").strip()
    if re.fullmatch(r"https?://\S+", text):
        return "url", ".txt", text.encode("utf-8")
    if text.lstrip().lower().startswith(("<!doctype html", "<html")):
        return "text", ".txt", page_text(text).encode("utf-8")
    if filename.lower().endswith((".rtf",)):
        text = re.sub(r"\\[a-z]+-?\d* ?|[{}]", "", text)
    return "text", ".txt", text.encode("utf-8")


def submit(body, content_type, filename=""):
    """Saves an upload as a new job and queues it. Returns the job dict."""
    kind, ext, data = sniff(body, content_type, filename)
    if not data.strip():
        raise ValueError("empty upload")
    job_id = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    folder = os.path.join(INBOX, job_id)
    os.makedirs(folder)
    source = "source" + ext
    with open(os.path.join(folder, source), "wb") as f:
        f.write(data)
    job = {"id": job_id, "kind": kind, "source": source, "status": "queued",
           "received": time.strftime("%Y-%m-%d %H:%M:%S")}
    if kind == "url":
        job["url"] = data.decode("utf-8")
    save(job)
    _jobs.put(job_id)
    return job


def save(job):
    path = os.path.join(INBOX, job["id"], "job.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(job, f, indent=2)
    os.replace(path + ".tmp", path)


def load(job_id):
    with open(os.path.join(INBOX, job_id, "job.json"), encoding="utf-8") as f:
        return json.load(f)


def recent(limit=25):
    if not os.path.isdir(INBOX):
        return []
    jobs = []
    for name in sorted(os.listdir(INBOX), reverse=True)[:limit]:
        try:
            jobs.append(load(name))
        except (OSError, ValueError):
            pass
    return jobs


# ---- preparing the source ---------------------------------------------------

class _TextOnly(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe"}

    def __init__(self):
        super().__init__()
        self.parts, self.depth = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.depth += 1
        elif tag in ("p", "li", "br", "h1", "h2", "h3", "h4", "tr", "div"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if not self.depth:
            self.parts.append(data)


def page_text(page):
    parser = _TextOnly()
    parser.feed(page)
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(parser.parts))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def recipe_json_ld(page):
    """Returns the schema.org Recipe objects embedded in a page, as JSON text."""
    found = []

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            kind = node.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if "Recipe" in kinds:
                found.append(node)
            for key in ("@graph", "mainEntity", "itemListElement"):
                if key in node:
                    walk(node[key])

    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            walk(json.loads(html.unescape(block.strip())))
        except ValueError:
            continue
    return json.dumps(found, indent=1, ensure_ascii=False) if found else ""


def download(url):
    """Returns the page as text. Tries curl first: it uses the Windows certificate
    store, which Python's bundled OpenSSL may not trust."""
    curl = shutil.which("curl")
    if curl:
        proc = subprocess.run([curl, "-sSL", "--compressed", "--max-time", "30", "--max-filesize", "5000000",
                               "-A", UA, "-H", "Accept-Language: en-US,en", "-w", "\n%{http_code}", url],
                              capture_output=True, timeout=60)
        body, _, code = proc.stdout.rpartition(b"\n")
        if proc.returncode == 0 and code.strip() == b"200":
            return body.decode("utf-8", errors="replace")
        curl_error = f"curl: {proc.stderr.decode(errors='replace').strip() or 'HTTP ' + code.decode().strip()}"
    else:
        curl_error = "curl not found"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read(5_000_000)
            charset = resp.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")
    except Exception as e:  # noqa: BLE001 - reported alongside the curl error
        raise RuntimeError(f"{curl_error}; urllib: {e}")


def fetch_page(url, folder):
    """Downloads a recipe page and saves what Claude needs as page.txt. Returns a note."""
    try:
        page = download(url)
    except Exception as e:  # noqa: BLE001 - any failure falls back to WebFetch
        return f"Downloading the page failed ({e}). Use WebFetch on the link instead."
    structured = recipe_json_ld(page)
    text = page_text(page)[:60000]
    with open(os.path.join(folder, "page.txt"), "w", encoding="utf-8") as f:
        if structured:
            f.write("STRUCTURED RECIPE DATA FROM THE PAGE (schema.org):\n" + structured + "\n\n")
        f.write("PAGE TEXT:\n" + text)
    return "The page was downloaded to page.txt in the current folder. Read it."


def convert_heic(folder, job):
    try:
        from PIL import Image
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        raise RuntimeError("HEIC photo: run `py -3 -m pip install pillow pillow-heif` or share a screenshot")
    src = os.path.join(folder, job["source"])
    Image.open(src).convert("RGB").save(os.path.join(folder, "source.jpg"), quality=90)
    job["source"] = "source.jpg"


# ---- running Claude ---------------------------------------------------------

def prompt_for(job, note):
    with open(os.path.join(LIBRARY, "README.md"), encoding="utf-8") as f:
        guide = f.read()
    guide = guide[guide.find("## Format"):] if "## Format" in guide else guide
    if job["kind"] == "url":
        where = f"The recipe was shared as a link: {job['url']}\n{note}"
    elif job["kind"] == "image":
        where = f"The recipe was shared as an image: read {job['source']} in the current folder."
    elif job["kind"] == "pdf":
        where = f"The recipe was shared as a PDF: read {job['source']} in the current folder."
    else:
        where = f"The recipe was shared as text: read {job['source']} in the current folder."
    return f"""You are adding one recipe to a recipe library for a Nook e-reader.

{where}

Rewrite it in the library format below. Keep every ingredient, quantity, and step
from the source; drop ads, life stories, and comments. Fix obvious OCR mistakes.
For a link, add the link as the last line of Notes ("Source: <link>").

{guide}

Reply with ONLY this, no other text:

CATEGORY: <one of: {", ".join(CATEGORIES)}>
SLUG: <2 to 5 lowercase words joined by hyphens, like chicken-tikka-masala>
---
<the recipe Markdown, starting with "# ">

If the source has no recipe in it, reply with only: NOT_A_RECIPE: <short reason>
"""


def claude_exe():
    exe = shutil.which("claude")
    if not exe:
        home = os.path.expanduser("~")
        for guess in (os.path.join(home, ".local", "bin", "claude.exe"),
                      os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd")):
            if os.path.exists(guess):
                return guess
        raise RuntimeError("Claude Code (claude) is not installed or not on PATH")
    return exe


def run_claude(job, folder, prompt):
    args = [claude_exe(), "-p", "--output-format", "json", "--max-turns", "10", "--model", CLAUDE_MODEL,
            # only the tools it needs, and no MCP servers or skills, keeps each run small
            "--tools", "Read,WebFetch", "--allowedTools", "Read,WebFetch",
            "--strict-mcp-config", "--disable-slash-commands"]
    proc = subprocess.run(args, input=prompt, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=folder, timeout=CLAUDE_TIMEOUT)
    with open(os.path.join(folder, "claude.log"), "w", encoding="utf-8") as f:
        f.write(f"exit {proc.returncode}\n--- stdout\n{proc.stdout}\n--- stderr\n{proc.stderr}")
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        raise RuntimeError(f"claude failed (exit {proc.returncode}): {(proc.stderr or proc.stdout).strip()[:300]}")
    if out.get("is_error"):
        raise RuntimeError(f"claude error: {str(out.get('result'))[:300]}")
    return out.get("result") or ""


def parse_reply(reply):
    reply = reply.strip().strip("`").strip()
    m = re.search(r"NOT_A_RECIPE:\s*(.*)", reply)
    if m and "CATEGORY:" not in reply:
        raise RuntimeError("not a recipe: " + m.group(1).strip()[:200])
    cat = re.search(r"^CATEGORY:\s*([a-z]+)", reply, re.M | re.I)
    slug = re.search(r"^SLUG:\s*([A-Za-z0-9-]+)", reply, re.M)
    body = reply.split("\n---\n", 1)
    if not (cat and slug and len(body) == 2 and "# " in body[1]):
        raise RuntimeError("could not read Claude's reply (see claude.log)")
    category = cat.group(1).lower()
    if category not in CATEGORIES:
        category = "misc"
    markdown = body[1][body[1].index("# "):].strip() + "\n"
    slug = re.sub(r"-+", "-", slug.group(1).lower()).strip("-")[:60] or "recipe"
    return category, slug, markdown


def write_recipe(category, slug, markdown):
    folder = os.path.join(LIBRARY, category)
    os.makedirs(folder, exist_ok=True)
    name, n = f"{category}--{slug}.md", 2
    while os.path.exists(os.path.join(folder, name)):
        name, n = f"{category}--{slug}-{n}.md", n + 1
    with open(os.path.join(folder, name), "w", encoding="utf-8", newline="\n") as f:
        f.write(markdown)
    return f"{category}/{name}"


def process(job_id):
    job = load(job_id)
    folder = os.path.join(INBOX, job_id)
    job["status"], job["started"] = "working", time.strftime("%Y-%m-%d %H:%M:%S")
    save(job)
    try:
        note = ""
        if job["kind"] == "url":
            note = job["fetch"] = fetch_page(job["url"], folder)
        elif job["source"].endswith(".heic"):
            convert_heic(folder, job)
        category, slug, markdown = parse_reply(run_claude(job, folder, prompt_for(job, note)))
        job["recipe"] = write_recipe(category, slug, markdown)
        job["title"] = markdown.splitlines()[0][2:].strip()
        job["status"] = "done"
        if _on_recipe:
            _on_recipe()
    except Exception as e:  # noqa: BLE001 - recorded on the job for the status page
        job["status"], job["error"] = "failed", str(e)
    job["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save(job)
    print(f"inbox {job_id}: {job['status']} {job.get('recipe') or job.get('error')}", flush=True)


def _worker():
    while True:
        job_id = _jobs.get()
        try:
            process(job_id)
        except Exception as e:  # noqa: BLE001 - keep the worker alive
            print(f"inbox {job_id}: crashed: {e}", flush=True)


def start(on_recipe=None):
    """Starts the worker and requeues jobs left unfinished by the last run."""
    global _on_recipe
    _on_recipe = on_recipe
    os.makedirs(INBOX, exist_ok=True)
    for job in reversed(recent(limit=1000)):
        if job.get("status") in ("queued", "working"):
            _jobs.put(job["id"])
    threading.Thread(target=_worker, daemon=True).start()


# ---- status page ------------------------------------------------------------

def page(port):
    rows = []
    for job in recent():
        what = job.get("url") or job["kind"]
        result = job.get("title") or job.get("error") or ""
        if job.get("recipe"):
            result = f'<a href="/{html.escape(job["recipe"])}">{html.escape(result)}</a>'
        else:
            result = html.escape(result)
        rows.append(f"<tr><td>{html.escape(job['received'][5:16])}</td>"
                    f"<td class='{job['status']}'>{job['status']}</td>"
                    f"<td class='what'>{html.escape(what[:80])}</td><td>{result}</td></tr>")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Recipe inbox</title>
<style>
body {{ font: 16px -apple-system, system-ui, sans-serif; margin: 16px; max-width: 760px; color: #222; background: #fff; }}
textarea, input {{ font: inherit; width: 100%; box-sizing: border-box; margin: 4px 0 10px; }}
button {{ font: inherit; padding: 8px 18px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
td {{ border-top: 1px solid #ddd; padding: 6px 4px; vertical-align: top; }}
.what {{ word-break: break-all; color: #666; }}
.done {{ color: #1a7f37; }} .failed {{ color: #c62828; }} .working, .queued {{ color: #9a6700; }}
</style></head><body>
<h1>Recipe inbox</h1>
<form id="f">
<label>Link or recipe text<textarea id="t" rows="4"></textarea></label>
<label>Or a photo, screenshot, or file<input id="file" type="file"></label>
<label>Token<input id="tok" autocomplete="off"></label>
<button>Add recipe</button> <span id="msg"></span>
</form>
<h2>Recent</h2>
<table>{"".join(rows) or "<tr><td>Nothing yet.</td></tr>"}</table>
<script>
const tok = document.getElementById('tok');
try {{ tok.value = localStorage.getItem('inboxToken') || ''; }} catch (e) {{}}
document.getElementById('f').onsubmit = async (e) => {{
  e.preventDefault();
  try {{ localStorage.setItem('inboxToken', tok.value); }} catch (e) {{}}
  const file = document.getElementById('file').files[0];
  const body = file || document.getElementById('t').value;
  const msg = document.getElementById('msg');
  msg.textContent = 'Sending...';
  const r = await fetch('/inbox', {{ method: 'POST', body,
    headers: {{ 'X-Inbox-Token': tok.value, 'X-Filename': file ? file.name : '' }} }});
  msg.textContent = await r.text();
  if (r.ok) setTimeout(() => location.reload(), 1500);
}};
setTimeout(() => {{ if (document.querySelector('.working, .queued')) location.reload(); }}, 10000);
</script></body></html>"""
