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

# dish photos: shown across the Nook's text column (600 px screen minus margins);
# taller photos are center-cropped so the ingredients stay near the top
PHOTO_W, PHOTO_MAX_H = 544, 380
MAX_CANDIDATES = 8
# list thumbnails: <recipe name>.thumb.jpg, a square cut from the photo or,
# without one, a simple icon Claude draws; corners rounded onto the list's
# background color
THUMB, THUMB_RADIUS, THUMB_BG = 80, 12, (250, 247, 240)
ICON_TILE = (240, 232, 216)  # icons sit on a soft tile, so they line up with photos

_jobs = queue.Queue()
_on_recipe = None  # called after a recipe is written (rebuilds index.txt)


def _load_video_intake():
    import importlib.util
    spec = importlib.util.spec_from_file_location("video_intake", os.path.join(HERE, "video_intake.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


video = _load_video_intake()  # video links and uploads (YouTube, TikTok, Instagram, ...)


def _this():
    """This module's functions, for video_intake (it's loaded by path, so not in sys.modules)."""
    import types
    return types.SimpleNamespace(**globals())


def token():
    if not os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "w") as f:
            f.write(secrets.token_urlsafe(12) + "\n")
    with open(TOKEN_FILE) as f:
        return f.read().strip()


# ---- accepting uploads ------------------------------------------------------

def sniff(body, content_type, filename=""):
    """Returns (kind, extension, data) for an upload. kind is url, text, image, pdf, or video."""
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
    video_ext = video.upload_ext(body, ct, filename)
    if video_ext:
        return "video", video_ext, body
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
        if video.is_video_url(job["url"]):
            job["kind"] = "video"
    elif kind == "video" and filename:
        job["filename"] = os.path.basename(filename)[:100]
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


def curl_binaries():
    """Every curl to try, Git for Windows' first. Its OpenSSL TLS handshake gets past
    Cloudflare sites that answer the built-in Windows curl (Schannel) with HTTP 403."""
    found = []
    for c in (os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "mingw64", "bin", "curl.exe"),
              shutil.which("curl")):
        if c and os.path.isfile(c) and os.path.normcase(c) not in map(os.path.normcase, found):
            found.append(c)
    return found


def run_curl(args, url, timeout):
    """Tries each curl in turn. Returns (body or None, error text)."""
    errors = []
    for curl in curl_binaries():
        proc = subprocess.run([curl, *args, "-w", "\n%{http_code}", url], capture_output=True, timeout=timeout)
        body, _, code = proc.stdout.rpartition(b"\n")
        if proc.returncode == 0 and code.strip() == b"200":
            return body, ""
        errors.append(f"{curl}: {proc.stderr.decode(errors='replace').strip() or 'HTTP ' + code.decode().strip()}")
    return None, "; ".join(errors) or "curl not found"


def download(url):
    """Returns the page as text. Tries curl first: it uses the system certificate
    store, which Python's bundled OpenSSL may not trust."""
    body, curl_error = run_curl(["-sSL", "--compressed", "--max-time", "30", "--max-filesize", "5000000",
                                 "-A", UA, "-H", "Accept-Language: en-US,en"], url, 60)
    if body is not None:
        return body.decode("utf-8", errors="replace")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read(5_000_000)
            charset = resp.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")
    except Exception as e:  # noqa: BLE001 - reported alongside the curl error
        raise RuntimeError(f"{curl_error}; urllib: {e}")


def fetch_page(url, folder):
    """Downloads a recipe page and saves what Claude needs as page.txt.
    Returns (note for Claude, page HTML or None)."""
    try:
        page = download(url)
    except Exception as e:  # noqa: BLE001 - any failure falls back to WebFetch
        return f"Downloading the page failed ({e}). Use WebFetch on the link instead.", None
    structured = recipe_json_ld(page)
    text = page_text(page)[:60000]
    with open(os.path.join(folder, "page.txt"), "w", encoding="utf-8") as f:
        if structured:
            f.write("STRUCTURED RECIPE DATA FROM THE PAGE (schema.org):\n" + structured + "\n\n")
        f.write("PAGE TEXT:\n" + text)
    return "The page was downloaded to page.txt in the current folder. Read it.", page


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


# ---- dish photos ------------------------------------------------------------
#
# Python gathers the candidate images (for a link: the recipe data's image,
# og:image, and the photos in the page; for a PDF: its embedded images; for a
# screenshot: the screenshot itself) and puts them on one numbered contact
# sheet. Claude looks at it and names the one that shows the finished dish,
# with a box when the photo is only part of the image. Python crops it, sizes
# it for the Nook, and saves it next to the recipe.

def _pil():
    try:
        from PIL import Image
        return Image
    except ImportError:
        return None


class _Images(HTMLParser):
    """Collects og:image/twitter:image and <img> sources, in page order."""
    SKIP = re.compile(r"logo|icon|avatar|sprite|pixel|badge|placeholder|spinner|blank\.|\.svg|\.gif|gravatar|"
                      r"facebook\.com|doubleclick|/ads?/", re.I)

    def __init__(self):
        super().__init__()
        self.meta, self.imgs = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key in ("og:image", "og:image:secure_url", "og:image:url", "twitter:image", "twitter:image:src"):
                self.meta.append(a.get("content") or "")
        elif tag in ("img", "source"):
            best, best_w = None, 0
            for key in ("srcset", "data-srcset", "data-lazy-srcset"):
                for part in (a.get(key) or "").split(","):
                    bits = part.split()
                    if not bits:
                        continue
                    w = int(bits[1][:-1]) if len(bits) > 1 and bits[1][:-1].isdigit() else 1
                    if w > best_w:
                        best, best_w = bits[0], w
            for key in ("data-src", "data-lazy-src", "data-original", "data-hi-res-src", "src"):
                if not best and a.get(key) and not a[key].startswith("data:"):
                    best = a[key]
            if best:
                self.imgs.append(best)


def _ld_images(node, out):
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, list):
        for n in node:
            _ld_images(n, out)
    elif isinstance(node, dict):
        _ld_images(node.get("url") or node.get("contentUrl") or "", out)


def page_image_urls(page, url):
    """Candidate photo links from a recipe page, most likely first."""
    from urllib.parse import urljoin
    found = []
    structured = recipe_json_ld(page)
    for recipe in json.loads(structured) if structured else []:
        _ld_images(recipe.get("image"), found)
    parser = _Images()
    try:
        parser.feed(page)
    except Exception:  # noqa: BLE001 - a broken page still has its JSON-LD
        pass
    found += parser.meta + [u for u in parser.imgs if not _Images.SKIP.search(u)]
    out, seen = [], set()
    for u in found:
        u = urljoin(url, html.unescape(u.strip()))
        if u.startswith("http") and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def download_bytes(url, referer=""):
    if curl_binaries():
        body, _ = run_curl(["-sSL", "--max-time", "15", "--max-filesize", "15000000", "-A", UA,
                            "-e", referer or url], url, 60)
        return body
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer or url})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read(15_000_000)
    except Exception:  # noqa: BLE001 - a missing photo is not an error
        return None


def _fingerprint(img):
    small = img.convert("L").resize((8, 8))
    px = list(small.tobytes())
    avg = sum(px) / len(px)
    return tuple(p > avg for p in px)


def _open_image(data_or_path):
    import io
    Image = _pil()
    try:
        img = Image.open(io.BytesIO(data_or_path) if isinstance(data_or_path, bytes) else data_or_path)
        img.load()
        return img.convert("RGB")
    except Exception:  # noqa: BLE001 - not an image Pillow can read
        return None


def gather_candidates(job, folder, page=None):
    """Saves candidate photos as cand-<n>.jpg and a numbered contact sheet as
    photos.jpg. Returns the candidate file names (empty if there are none)."""
    Image = _pil()
    if not Image:
        return []
    images = []
    if job["kind"] == "url" and page:
        for u in page_image_urls(page, job["url"])[:30]:
            data = download_bytes(u, job["url"])
            img = _open_image(data) if data else None
            if img:
                images.append(img)
            if len(images) >= MAX_CANDIDATES + 4:
                break
    elif job["kind"] == "pdf":
        images = _pdf_images(os.path.join(folder, job["source"]))
    elif job["kind"] == "image":
        img = _open_image(os.path.join(folder, job["source"]))
        if img:
            return [_save_candidate(img, folder, 1)]
        return []
    names, seen = [], set()
    for img in images:
        w, h = img.size
        # too small to show, or a banner/strip rather than a photo
        if w < 300 or h < 200 or w > 3.5 * h or h > 2.5 * w:
            continue
        fp = _fingerprint(img)
        if fp in seen:  # the same photo at another size
            continue
        seen.add(fp)
        names.append(_save_candidate(img, folder, len(names) + 1))
        if len(names) >= MAX_CANDIDATES:
            break
    if names:
        _contact_sheet(folder, names)
    return names


def _save_candidate(img, folder, n):
    img = img.copy()
    img.thumbnail((1600, 1600))
    name = f"cand-{n}.jpg"
    img.save(os.path.join(folder, name), quality=90)
    return name


def _pdf_images(path):
    images = []
    try:
        from pypdf import PdfReader
        for page in PdfReader(path).pages[:10]:
            for im in page.images:
                img = _open_image(im.data)
                if img:
                    images.append(img)
    except Exception:  # noqa: BLE001 - no pypdf, or an odd PDF: just no photo
        pass
    return images


def _contact_sheet(folder, names):
    from PIL import Image, ImageDraw, ImageFont
    tile_w, tile_h, cols = 420, 315, 3 if len(names) > 4 else 2
    rows = (len(names) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tile_w, rows * tile_h), "white")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=36)
    except TypeError:
        font = ImageFont.load_default()
    for i, name in enumerate(names):
        img = Image.open(os.path.join(folder, name))
        img.thumbnail((tile_w - 10, tile_h - 10))
        x, y = (i % cols) * tile_w, (i // cols) * tile_h
        sheet.paste(img, (x + (tile_w - img.width) // 2, y + (tile_h - img.height) // 2))
        draw.rectangle((x + 5, y + 5, x + 55, y + 50), fill="black")
        draw.text((x + 18, y + 8), str(i + 1), fill="yellow", font=font)
    sheet.save(os.path.join(folder, "photos.jpg"), quality=85)


def photo_prompt(job, names):
    if not names:
        return ""
    if job["kind"] == "image":
        return f"""
DISH PHOTO: if the shared image contains a photo of the finished dish (not a step,
ingredient, or person photo), add a line after SLUG giving a tight box around just
that photo, in percent of the image's width and height, measured from the top left:
PHOTO: {names[0]} <left>,<top>,<right>,<bottom>
If there is no photo of the finished dish, add: PHOTO: none
"""
    listing = ", ".join(names)
    return f"""
DISH PHOTO: photos.jpg in the current folder is a contact sheet of the {len(names)} images
found in the source, numbered to match the files {listing}. Read photos.jpg (and any
single cand-<n>.jpg if you need a closer look). Pick the one photo that shows the
FINISHED dish of THIS recipe, plated or whole. Not a step-by-step or ingredient
photo, a person, a logo, an ad, or a different recipe. Images that come first are
usually from the recipe data, so prefer those when they fit. Add a line after SLUG:
PHOTO: cand-<n>.jpg
If the dish photo is only part of that image (for example a collage or a photo
with text around it), add a tight box in percent of its width and height:
PHOTO: cand-<n>.jpg <left>,<top>,<right>,<bottom>
If none of them shows the finished dish, add: PHOTO: none
"""


def parse_photo(reply, names):
    """Returns (file, box or None) from the PHOTO line, or None."""
    m = re.search(r"^PHOTO:\s*(cand-\d+\.jpg)\s*([\d.]+)?\s*,?\s*([\d.]+)?\s*,?\s*([\d.]+)?\s*,?\s*([\d.]+)?",
                  reply, re.M | re.I)
    if not m or m.group(1).lower() not in names:
        return None
    box = None
    if m.group(5):
        box = [float(m.group(i)) for i in range(2, 6)]
        if not (0 <= box[0] < box[2] <= 100 and 0 <= box[1] < box[3] <= 100):
            box = None
    return m.group(1).lower(), box


def _trim_flat_edges(img):
    """Drops rows and columns that are flat page background (the corner's color)
    left in a loose box, so the crop hugs the photo."""
    gray = img.convert("L")
    w, h = gray.size
    px = gray.load()
    bg = px[0, 0]

    def flat(values):
        values = list(values)
        avg = sum(values) / len(values)
        return abs(avg - bg) < 12 and sum((v - avg) ** 2 for v in values) / len(values) < 30

    top, bottom, left, right = 0, h, 0, w
    for _ in range(2):  # again, now that the other edges are trimmed
        top, bottom, left, right = _trim_pass(px, w, h, flat, top, bottom, left, right)
    return img.crop((left, top, right, bottom))


def _trim_pass(px, w, h, flat, top, bottom, left, right):
    while bottom - top > h // 3 and flat(px[x, top] for x in range(left, right, 2)):
        top += 1
    while bottom - top > h // 3 and flat(px[x, bottom - 1] for x in range(left, right, 2)):
        bottom -= 1
    while right - left > w // 3 and flat(px[left, y] for y in range(top, bottom, 2)):
        left += 1
    while right - left > w // 3 and flat(px[right - 1, y] for y in range(top, bottom, 2)):
        right -= 1
    return top, bottom, left, right


def make_photo(folder, choice):
    """Crops and sizes the chosen candidate for the Nook. Returns a PIL image or None."""
    name, box = choice
    img = _open_image(os.path.join(folder, name))
    if not img:
        return None
    if box:
        w, h = img.size
        pad = 1  # percent: a slightly loose box, then trim the background back off
        l, t = max(0, box[0] - pad) * w / 100, max(0, box[1] - pad) * h / 100
        r, b = min(100, box[2] + pad) * w / 100, min(100, box[3] + pad) * h / 100
        img = _trim_flat_edges(img.crop((int(l), int(t), int(r), int(b))))
    w, h = img.size
    if w < 120 or h < 80:
        return None
    max_h = w * PHOTO_MAX_H // PHOTO_W
    if h > max_h:  # center-crop tall photos
        top = (h - max_h) // 2
        img = img.crop((0, top, w, top + max_h))
    if img.width > PHOTO_W:
        img = img.resize((PHOTO_W, round(img.height * PHOTO_W / img.width)), _pil().LANCZOS)
    return img


def insert_photo_line(markdown, name, title):
    """Puts ![...](name) after the subtitle (the first plain line under the title)."""
    lines = markdown.split("\n")
    at = 1
    for i, line in enumerate(lines[1:], 1):
        if line.startswith("#"):
            break
        if line.strip():
            at = i + 1
            break
    lines[at:at] = ["", f"![Photo of {title}]({name})"]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines))


# ---- list thumbnails and icons -----------------------------------------------

ICON_PROMPT = """Draw a minimalist icon of this dish (shown small, about 80 px, in
the recipe list when there's no photo) as ONE line of SVG:
ICON: <svg viewBox="0 0 100 100">...</svg>
Use only circle, ellipse, rect, line, polyline, polygon, and path (commands M L H V
C Q Z only), with fill, stroke, and stroke-width attributes. Keep it flat and simple:
3 to 10 shapes, bold strokes (stroke-width 4 to 6) in #3A322B, with at most two soft
fill colors (like #E9C46A, #E76F51, #9DB87A, #F4E3C1, or the dish's real colors).
Fill most of the 100x100 box (stay within 8 to 92). No text, no background rect.
"""


def _num(v, default=0.0):
    try:
        return float(re.sub(r"[a-z%]+$", "", str(v).strip()))
    except ValueError:
        return default


def _path_points(d):
    """Flattens an SVG path (M L H V C Q Z, absolute or relative) into polylines."""
    tokens = re.findall(r"[MLHVCQZmlhvcqz]|-?\d*\.?\d+(?:e-?\d+)?", d)
    polys, cur, x, y, cmd, i = [], [], 0.0, 0.0, "M", 0

    def take(n):
        nonlocal i
        vals = [float(t) for t in tokens[i:i + n]]
        i += n
        return vals

    while i < len(tokens):
        if tokens[i].isalpha():
            cmd = tokens[i]
            i += 1
            if cmd in "Zz":
                if cur:
                    cur.append(cur[0])
                    polys.append((cur, True))
                    x, y = cur[0]
                cur = []
                continue
        rel = cmd.islower()
        c = cmd.upper()
        ox, oy = (x, y) if rel else (0.0, 0.0)
        try:
            if c == "M":
                px, py = take(2)
                if cur:
                    polys.append((cur, False))
                x, y = ox + px, oy + py
                cur = [(x, y)]
                cmd = "l" if rel else "L"
            elif c == "L":
                px, py = take(2)
                x, y = ox + px, oy + py
                cur.append((x, y))
            elif c == "H":
                x = (x if rel else 0) + take(1)[0]
                cur.append((x, y))
            elif c == "V":
                y = (y if rel else 0) + take(1)[0]
                cur.append((x, y))
            elif c in "CQ":
                vals = take(6 if c == "C" else 4)
                pts = [(x, y)] + [(ox + vals[k], oy + vals[k + 1]) for k in range(0, len(vals), 2)]
                for step in range(1, 13):
                    t = step / 12
                    if c == "C":
                        a, b, cc, dd = pts
                        px = (1-t)**3*a[0] + 3*(1-t)**2*t*b[0] + 3*(1-t)*t*t*cc[0] + t**3*dd[0]
                        py = (1-t)**3*a[1] + 3*(1-t)**2*t*b[1] + 3*(1-t)*t*t*cc[1] + t**3*dd[1]
                    else:
                        a, b, cc = pts
                        px = (1-t)**2*a[0] + 2*(1-t)*t*b[0] + t*t*cc[0]
                        py = (1-t)**2*a[1] + 2*(1-t)*t*b[1] + t*t*cc[1]
                    cur.append((px, py))
                x, y = pts[-1]
            else:
                i += 1  # unsupported command: skip its numbers
        except (ValueError, IndexError):
            break
        if not cur:
            cur = [(x, y)]
    if len(cur) > 1:
        polys.append((cur, False))
    return polys


def render_icon(svg, size=THUMB):
    """Draws the simple SVG subset from ICON_PROMPT with Pillow. Returns an RGB image or None."""
    import xml.etree.ElementTree as ET
    from PIL import Image, ImageColor, ImageDraw
    try:
        root = ET.fromstring(svg)
    except ET.ParseError:
        return None
    box = [_num(v) for v in (root.get("viewBox") or "0 0 100 100").replace(",", " ").split()]
    if len(box) != 4 or box[2] <= 0 or box[3] <= 0:
        box = [0, 0, 100, 100]
    big = size * 5
    k = big / max(box[2], box[3])
    img = Image.new("RGB", (big, big), ICON_TILE)
    draw = ImageDraw.Draw(img)
    drawn = 0

    def color(v):
        if not v or v in ("none", "transparent"):
            return None
        try:
            return ImageColor.getrgb(v)
        except ValueError:
            return None

    def pt(px, py):
        return ((px - box[0]) * k, (py - box[1]) * k)

    def walk(el, inherited):
        nonlocal drawn
        attrs = dict(inherited)
        for key in ("fill", "stroke", "stroke-width"):
            if el.get(key) is not None:
                attrs[key] = el.get(key)
        for part in (el.get("style") or "").split(";"):
            if ":" in part:
                key, value = (p.strip() for p in part.split(":", 1))
                if key in ("fill", "stroke", "stroke-width"):
                    attrs[key] = value
        tag = el.tag.split("}")[-1]
        fill, stroke = color(attrs.get("fill", "black")), color(attrs.get("stroke"))
        width = max(1, round(_num(attrs.get("stroke-width"), 1) * k)) if stroke else 0
        g = lambda name, d=0.0: _num(el.get(name), d)  # noqa: E731
        if tag in ("circle", "ellipse"):
            rx = g("r") if tag == "circle" else g("rx")
            ry = g("r") if tag == "circle" else g("ry")
            (x0, y0), (x1, y1) = pt(g("cx") - rx, g("cy") - ry), pt(g("cx") + rx, g("cy") + ry)
            if x1 > x0 and y1 > y0:
                draw.ellipse((x0, y0, x1, y1), fill=fill, outline=stroke, width=width)
                drawn += 1
        elif tag == "rect":
            (x0, y0), (x1, y1) = pt(g("x"), g("y")), pt(g("x") + g("width"), g("y") + g("height"))
            if x1 > x0 and y1 > y0:
                draw.rounded_rectangle((x0, y0, x1, y1), radius=g("rx", g("ry")) * k, fill=fill,
                                       outline=stroke, width=width)
                drawn += 1
        elif tag in ("line", "polyline", "polygon", "path"):
            if tag == "line":
                polys = [([(g("x1"), g("y1")), (g("x2"), g("y2"))], False)]
                fill = None
            elif tag == "path":
                polys = _path_points(el.get("d") or "")
            else:
                nums = [_num(v) for v in re.findall(r"-?\d*\.?\d+", el.get("points") or "")]
                pts = list(zip(nums[0::2], nums[1::2]))
                polys = [(pts + pts[:1], True)] if tag == "polygon" else [(pts, False)]
            for pts, closed in polys:
                xy = [pt(*p) for p in pts]
                if len(xy) < 2:
                    continue
                if fill and len(xy) > 2:
                    draw.polygon(xy, fill=fill)
                if stroke:
                    draw.line(xy, fill=stroke, width=width, joint="curve")
                    r = width / 2
                    for cx, cy in (xy[0], xy[-1]):  # round caps
                        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=stroke)
                drawn += 1
        for child in el:
            walk(child, attrs)

    walk(root, {})
    if not drawn:
        return None
    return img.resize((size, size), Image.LANCZOS)


def parse_icon(reply):
    m = re.search(r"^ICON:\s*(<svg\b.*?</svg>)", reply, re.M | re.S | re.I)
    return m.group(1) if m else None


def make_thumb(img):
    """Square center cut of a photo (or an icon), corners rounded onto the list background."""
    from PIL import Image, ImageDraw
    w, h = img.size
    side = min(w, h)
    img = img.crop(((w - side) // 2, (h - side) // 2, (w - side) // 2 + side, (h - side) // 2 + side))
    img = img.resize((THUMB, THUMB), Image.LANCZOS)
    mask = Image.new("L", (THUMB * 4, THUMB * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, THUMB * 4 - 1, THUMB * 4 - 1), radius=THUMB_RADIUS * 4, fill=255)
    out = Image.new("RGB", (THUMB, THUMB), THUMB_BG)
    out.paste(img, (0, 0), mask.resize((THUMB, THUMB), Image.LANCZOS))
    return out


def save_thumb(img, md_path):
    make_thumb(img).save(md_path[:-3] + ".thumb.jpg", "JPEG", quality=92)


# ---- running Claude ---------------------------------------------------------

def prompt_for(job, note, photos=()):
    with open(os.path.join(LIBRARY, "README.md"), encoding="utf-8") as f:
        guide = f.read()
    guide = guide[guide.find("## Format"):] if "## Format" in guide else guide
    if job["kind"] == "video":
        where = note
    elif job["kind"] == "url":
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

Don't put image lines in the Markdown; the photo is added separately.
{photo_prompt(job, photos)}
Also add a line after SLUG with an icon. {ICON_PROMPT}
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


def run_claude(job, folder, prompt, tools="Read,WebFetch", max_turns=15, log="claude.log"):
    args = [claude_exe(), "-p", "--output-format", "json", "--max-turns", str(max_turns), "--model", CLAUDE_MODEL,
            # only the tools it needs, and no MCP servers or skills, keeps each run small
            "--tools", tools, "--allowedTools", tools,
            "--strict-mcp-config", "--disable-slash-commands"]
    proc = subprocess.run(args, input=prompt, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=folder, timeout=CLAUDE_TIMEOUT)
    with open(os.path.join(folder, log), "w", encoding="utf-8") as f:
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


def write_recipe(category, slug, markdown, photo=None, icon=None):
    """Writes the recipe, its photo (a PIL image) as <same name>.jpg, and its list
    thumbnail, cut from the photo or else drawn from the icon SVG."""
    folder = os.path.join(LIBRARY, category)
    os.makedirs(folder, exist_ok=True)
    name, n = f"{category}--{slug}.md", 2
    while os.path.exists(os.path.join(folder, name)):
        name, n = f"{category}--{slug}-{n}.md", n + 1
    if photo is not None:
        save_photo(photo, os.path.join(folder, name[:-3] + ".jpg"))
        markdown = insert_photo_line(markdown, name[:-3] + ".jpg", markdown.splitlines()[0][2:].strip())
    with open(os.path.join(folder, name), "w", encoding="utf-8", newline="\n") as f:
        f.write(markdown)
    try:
        thumb = photo if photo is not None else (render_icon(icon) if icon else None)
        if thumb is not None:
            save_thumb(thumb, os.path.join(folder, name))
    except Exception as e:  # noqa: BLE001 - the recipe is written; the list shows a letter instead
        print(f"thumbnail for {name} failed: {e}", flush=True)
    return f"{category}/{name}"


def save_photo(photo, path):
    # baseline JPEG: the Nook's decoder (stb_image) is fastest with it
    photo.save(path, "JPEG", quality=85, progressive=False)


def first_try(job, folder, found):
    """Digests the job the direct way. Returns (Claude's reply, photo candidates).
    Fills found["photos"] as it goes, so a rescue can reuse them."""
    note, page = "", None
    if job["kind"] == "url":
        note, page = fetch_page(job["url"], folder)
        job["fetch"] = note
        # another video site yt-dlp knows, with no recipe data on the page
        if not (page and recipe_json_ld(page)) and video.has_extractor(job["url"]):
            job["kind"] = "video"
            save(job)
    elif job["source"].endswith(".heic"):
        convert_heic(folder, job)
    if job["kind"] == "video":
        note, photos = video.prepare(job, folder, _this())
        save(job)
    else:
        try:
            photos = gather_candidates(job, folder, page)
        except Exception as e:  # noqa: BLE001 - the recipe matters more than its photo
            photos, job["photo"] = [], f"failed: {e}"
    found["photos"] = photos
    reply = run_claude(job, folder, prompt_for(job, note, photos))
    parse_reply(reply)  # raises on NOT_A_RECIPE or a garbled reply, which starts a rescue
    return reply, photos, folder


def rescue_prompt(job, error, photos):
    with open(os.path.join(LIBRARY, "README.md"), encoding="utf-8") as f:
        guide = f.read()
    guide = guide[guide.find("## Format"):] if "## Format" in guide else guide
    if job.get("url"):
        shared = f"a link: {job['url']}"
    else:
        shared = f"a file: {job['source']} in the current folder (read it)"
    return f"""You are adding one recipe to a recipe library for a Nook e-reader.
The recipe was shared as {shared}

A first attempt to digest it failed with this error:
{error}

Don't give up. The person shared this because there is a recipe behind it. Work out
where the recipe actually is, using WebSearch and WebFetch:
- The page may load its recipe or video with JavaScript. Search for the dish name
  with the site or author's name to find the full recipe, a print or AMP version,
  the same recipe on the author's other sites, or the video (often on YouTube).
- A blocked or broken page may be readable at
  https://web.archive.org/web/2024/<the link>, or through a search result.
- A video's description or pinned comment often links to, or contains, the recipe.
- A screenshot or text that's cut off may be finishable by searching for its title.
Use only the same recipe from the same source or author. Don't swap in some other
recipe for the same dish.

When you have the full recipe as text, reply in this format, and nothing else:

CATEGORY: <one of: {", ".join(CATEGORIES)}>
SLUG: <2 to 5 lowercase words joined by hyphens, like chicken-tikka-masala>
---
<the recipe Markdown, starting with "# ">

Rewrite it in the library format below. Keep every ingredient, quantity, and step;
drop ads, life stories, and comments. Notes must end with the link the recipe text
came from ("Recipe page: <link>"), then "Source: {job.get('url') or 'shared file'}" as the last line.
Don't put image lines in the Markdown.

{guide}
{photo_prompt(job, photos)}
Also add a line after SLUG with an icon. {ICON_PROMPT}

If the recipe only exists inside a video (it's spoken or shown, not written down
anywhere you can fetch), reply with only: VIDEO: <the video's link>
The video will then be downloaded, transcribed, and digested for you.

Only if you truly can't find the recipe anywhere, reply with only:
NOT_A_RECIPE: <what you tried, briefly>
"""


def rescue(job, folder, error, photos):
    """Second pass after a failed digest: Claude searches the web for the recipe
    itself, or names the video it lives in. Returns (reply, photo candidates, their folder)."""
    job["first_error"] = error
    save(job)
    reply = run_claude(job, folder, rescue_prompt(job, error, photos), tools="Read,WebFetch,WebSearch",
                       max_turns=40, log="rescue.log")
    m = re.search(r"^VIDEO:\s*(https?://\S+)", reply.strip().strip("`").strip(), re.M)
    if not m or "CATEGORY:" in reply:
        parse_reply(reply)
        return reply, photos, folder
    video_url = m.group(1)
    job["video_url"] = video_url
    save(job)
    vjob = dict(job, kind="video", url=video_url)
    vfolder = os.path.join(folder, "video")
    os.makedirs(vfolder, exist_ok=True)
    note, vphotos = video.prepare(vjob, vfolder, _this())
    for key in ("duration", "transcript", "frames"):
        if key in vjob:
            job[key] = vjob[key]
    if job.get("url"):
        note += f'Put the line "Shared link: {job["url"]}" just before the Source line in ## Notes.\n'
    reply = run_claude(vjob, vfolder, prompt_for(vjob, note, vphotos))
    parse_reply(reply)
    return reply, vphotos, vfolder


def process(job_id):
    job = load(job_id)
    folder = os.path.join(INBOX, job_id)
    job["status"], job["started"] = "working", time.strftime("%Y-%m-%d %H:%M:%S")
    job.pop("error", None)
    save(job)
    try:
        found = {"photos": []}
        try:
            reply, photos, photo_folder = first_try(job, folder, found)
        except Exception as e:  # noqa: BLE001 - any failure gets a rescue pass before giving up
            print(f"inbox {job_id}: first try failed ({e}); asking Claude to find the recipe", flush=True)
            job["status"] = "searching"
            save(job)
            reply, photos, photo_folder = rescue(job, folder, str(e), found["photos"])
        category, slug, markdown = parse_reply(reply)
        photo = None
        choice = parse_photo(reply, photos)
        if choice:
            try:
                photo = make_photo(photo_folder, choice)
                job["photo"] = " ".join([choice[0]] + ([",".join(f"{v:g}" for v in choice[1])] if choice[1] else []))
            except Exception as e:  # noqa: BLE001
                job["photo"] = f"failed: {e}"
        elif photos:
            job["photo"] = "none picked"
        job["recipe"] = write_recipe(category, slug, markdown, photo, parse_icon(reply))
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
        if job.get("status") in ("queued", "working", "searching"):
            _jobs.put(job["id"])
    threading.Thread(target=_worker, daemon=True).start()


# ---- adding a photo to a recipe that has none -------------------------------

def add_photo(recipe):
    """Finds and adds a dish photo for an existing library recipe (category/name.md),
    from the inbox job that made it or its "Source:" link. Returns a message."""
    path = os.path.join(LIBRARY, recipe)
    with open(path, encoding="utf-8") as f:
        markdown = f.read()
    if re.search(r"^!\[", markdown, re.M):
        return f"{recipe}: already has a photo"
    job = next((j for j in recent(limit=1000) if j.get("recipe", "").replace("\\", "/") == recipe), None)
    if job:
        job = dict(job)
    else:
        m = re.search(r"^Source:\s*(https?://\S+)", markdown, re.M)
        if not m:
            return f"{recipe}: no inbox job or Source: link to get a photo from"
        job = {"kind": "url", "url": m.group(1)}
    folder = os.path.join(INBOX, "photo-" + os.path.basename(recipe)[:-3])
    os.makedirs(folder, exist_ok=True)
    page = None
    if job["kind"] == "url":
        _, page = fetch_page(job["url"], folder)
    elif job["kind"] in ("image", "pdf"):
        src = os.path.join(INBOX, job["id"], job["source"])
        shutil.copy(src, os.path.join(folder, job["source"]))
    else:
        return f"{recipe}: it came from {job['kind']}, which has no photo to pick again"
    photos = gather_candidates(job, folder, page)
    if not photos:
        return f"{recipe}: no images found in the source"
    title = markdown.splitlines()[0][2:].strip()
    prompt = f"""You are picking the photo for a recipe called "{title}" in a recipe library.
{photo_prompt(job, photos).replace(" after SLUG", "")}
Reply with ONLY the PHOTO line.
"""
    choice = parse_photo(run_claude(job, folder, prompt), photos)
    photo = make_photo(folder, choice) if choice else None
    if photo is None:
        return f"{recipe}: no photo of the finished dish found"
    name = os.path.basename(recipe)[:-3] + ".jpg"
    save_photo(photo, os.path.join(os.path.dirname(path), name))
    save_thumb(photo, path)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(insert_photo_line(markdown, name, title))
    return f"{recipe}: added {name} (from {choice[0]})"


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
.done {{ color: #1a7f37; }} .failed {{ color: #c62828; }} .working, .queued, .searching {{ color: #9a6700; }}
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
setTimeout(() => {{ if (document.querySelector('.working, .queued, .searching')) location.reload(); }}, 10000);
</script></body></html>"""


def add_thumbs(redo=False):
    """Makes list thumbnails for library recipes that have none: from the recipe's
    photo, or an icon Claude draws from the recipe. Returns messages."""
    out = []
    for cat in sorted(os.listdir(LIBRARY)):
        folder = os.path.join(LIBRARY, cat)
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if not name.endswith(".md") or (os.path.exists(path[:-3] + ".thumb.jpg") and not redo):
                continue
            with open(path, encoding="utf-8") as f:
                markdown = f.read()
            m = re.search(r"^!\[[^\]]*\]\(([^)?]+)\)", markdown, re.M)
            photo = os.path.join(folder, os.path.basename(m.group(1))) if m else None
            if photo and os.path.exists(photo):
                save_thumb(_open_image(photo), path)
                out.append(f"{cat}/{name}: thumbnail from its photo")
                continue
            work = os.path.join(INBOX, "icon-" + name[:-3])
            os.makedirs(work, exist_ok=True)
            prompt = ("Here is a recipe:\n\n" + markdown[:3000] + "\n\n" + ICON_PROMPT +
                      "\nReply with ONLY the ICON line.\n")
            svg = parse_icon(run_claude({}, work, prompt))
            icon = render_icon(svg) if svg else None
            if icon is None:
                out.append(f"{cat}/{name}: no usable icon (see {work}\\claude.log)")
                continue
            with open(os.path.join(work, "icon.svg"), "w", encoding="utf-8") as f:
                f.write(svg)
            save_thumb(icon, path)
            out.append(f"{cat}/{name}: drew an icon")
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 2 and sys.argv[1] == "--photo":
        for r in sys.argv[2:]:
            print(add_photo(r.replace("\\", "/").removeprefix("library/")), flush=True)
    elif len(sys.argv) > 1 and sys.argv[1] == "--thumbs":
        for line in add_thumbs(redo="--redo" in sys.argv):
            print(line, flush=True)
    else:
        print("usage: py -3 tools\\recipe-inbox.py --photo <category>/<name>.md ...\n"
              "       py -3 tools\\recipe-inbox.py --thumbs [--redo]")
