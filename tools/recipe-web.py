"""Phone-friendly recipe pages, served by serve-recipes.py under /cook/.

  /cook/                    all recipes, favorites first, with search
  /cook/r/<cat>/<file>.md   one recipe: scaler, star, notes, add to list or plan
  /cook/edit/<cat>/<file>   edit or delete a recipe
  /cook/plan                meal planner; sends a week to the shopping list
  /cook/list                shopping list: tap what you already have
  /cook/api/...             JSON used by the pages (see handle_post)

Favorites are library/favorites.txt (one file name per line) and notes are a
final "## My notes" section in the recipe, so the Nook can show both. The
shopping list and meal plan are in state/cook-state.json. Editing, deleting,
and notes only work through Tailscale (requests arrive from 127.0.0.1 via
`tailscale serve`), not from other devices on the home Wi-Fi. Old versions
and deleted recipes go to trash/. See PHONE.md.
"""
import datetime
import html
import json
import os
import re
import shutil
import subprocess
import threading
import time
from urllib.parse import quote, unquote

CATEGORIES = ["breakfast", "mains", "sides", "soups", "baking", "desserts", "drinks", "misc"]
NAMES = {"misc": "Other"}
NOTES = "My notes"
LOCK = threading.Lock()
ASK_MODEL = os.environ.get("RECIPE_SEARCH_MODEL", "claude-haiku-5-5")
ASK_CACHE = {}


def esc(text):
    return html.escape(text, quote=True)


def parse(text):
    """Splits a recipe .md into title, subtitle, photo, and sections."""
    recipe = {"title": "", "subtitle": "", "photo": "", "sections": []}
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("# ") and not recipe["title"]:
            recipe["title"] = line[2:].strip()
        elif line.startswith("## "):
            section = {"name": line[3:].strip(), "items": []}
            recipe["sections"].append(section)
        elif section is None:
            photo = re.match(r"!\[[^\]]*\]\(([^)\s]+)\)", line)
            if photo:
                recipe["photo"] = photo.group(1)
            elif not recipe["subtitle"]:
                recipe["subtitle"] = line
        else:
            if line.startswith("#"):
                section["items"].append(("head", line.lstrip("#").strip()))
            elif re.match(r"[-*] ", line):
                section["items"].append(("bullet", line[2:].strip()))
            elif re.match(r"\d+[.)] ", line):
                section["items"].append(("step", line.split(" ", 1)[1].strip()))
            else:
                section["items"].append(("text", line))
    return recipe


def ingredients_of(recipe):
    return [text for s in recipe["sections"] if s["name"].lower().startswith("ingredient")
            for kind, text in s["items"] if kind == "bullet"]


def notes_of(recipe):
    for s in recipe["sections"]:
        if s["name"].lower() == NOTES.lower():
            return "\n".join(("- " + t) if k == "bullet" else t for k, t in s["items"])
    return ""


def recipe_path(library, rel):
    """Absolute path for "<category>/<file>.md" inside the library, else None."""
    rel = rel.replace("\\", "/")
    if rel.count("/") != 1 or not rel.endswith(".md"):
        return None
    path = os.path.abspath(os.path.join(library, rel))
    if not path.startswith(library + os.sep) or not os.path.isfile(path):
        return None
    return path


def read_text(path):
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return f.read()


def read(library, rel):
    path = recipe_path(library, rel)
    return parse(read_text(path)) if path else None


def photo_url(library, cat, base):
    """The dish photo, else the 80x80 stand-in icon (<name>.thumb.jpg), else ""."""
    for candidate in (base + ".jpg", base + ".thumb.jpg"):
        if os.path.isfile(os.path.join(library, cat, candidate)):
            return f"/{quote(cat)}/{quote(candidate)}"
    return ""


def recipes(library):
    """[(category, [recipe dicts])] in display order."""
    found = {}
    for cat in os.listdir(library):
        folder = os.path.join(library, cat)
        if not os.path.isdir(folder) or cat.startswith("."):
            continue
        for name in os.listdir(folder):
            if not name.endswith(".md"):
                continue
            rel = f"{cat}/{name}"
            recipe = read(library, rel)
            found.setdefault(cat, []).append({
                "id": rel,
                "title": recipe["title"] or name[:-3].split("--")[-1].replace("-", " "),
                "subtitle": recipe["subtitle"],
                "thumb": photo_url(library, cat, name[:-3]),
                "items": ingredients_of(recipe),
            })
    order = sorted(found, key=lambda c: (CATEGORIES.index(c) if c in CATEGORIES else 99, c))
    return [(c, sorted(found[c], key=lambda r: r["title"].lower())) for c in order]


# ---- asking Claude -------------------------------------------------------

ASK_PROMPT = """You run the search box of a home cook's recipe app. Today is {today}.
They typed: {q}

It may be a recipe search, a question about their meal plan or shopping list,
or a request to add things to the shopping list. Reply in these lines only:
RECIPE: <id>      one per recipe that fits, best first. Match by meaning:
                  cuisine ("Indian"), dish type, ingredient, diet, occasion, mood.
                  Also list the recipes your answer is about.
SAY: <answer>     only for a question or a list change; one or two short sentences.
ADD: <item>       one per thing to put on the shopping list, as they would write it
                  ("eggs", "2 lb chicken thighs"). Only when they ask to add.
ADD_RECIPE: <id>  put all of a recipe's ingredients on the list. Only when asked.
If nothing fits, reply with only: NONE

Meal plan (date | recipe ids; nothing planned on dates not shown):
{plan}

Shopping list (still to buy):
{need}
Already have: {have}

Recipes, one per line as id | title | description | ingredients:
{catalog}"""
EXTRA = "extra"  # list entry holding things added by hand, not from a recipe


def _claude():
    exe = shutil.which("claude")
    for guess in (os.path.join(os.path.expanduser("~"), ".local", "bin", "claude.exe"),
                  os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd")):
        if not exe and os.path.exists(guess):
            exe = guess
    if not exe:
        raise RuntimeError("Claude Code (claude) is not installed on the PC.")
    return exe


def _catalog(rows):
    lines = []
    for r in rows:
        items = [re.sub(r"\s*\([^)]*\)", "", t)[:40] for t in r["items"][:15]]
        lines.append(f'{r["id"]} | {r["title"]} | {r["subtitle"][:150]} | {"; ".join(items)}')
    return "\n".join(lines)


def _prompt(q, state, rows):
    today = datetime.date.today()
    titles = {r["id"]: r["title"] for r in rows}
    plan = []
    for day in sorted(state["plan"]):
        try:
            d = datetime.date.fromisoformat(day)
        except ValueError:
            continue
        if -7 <= (d - today).days <= 21 and state["plan"][day]:
            meals = ", ".join(f'{m.get("id")} ({titles.get(m.get("id"), "?")})' for m in state["plan"][day])
            plan.append(f"{d:%A %b %d} ({day}) | {meals}")
    items = [(i.get("text", ""), i.get("have")) for r in state["list"] for i in r.get("items", [])]
    return ASK_PROMPT.format(
        today=f"{today:%A, %B %d, %Y}", q=q, catalog=_catalog(rows),
        plan="\n".join(plan) or "(nothing planned)",
        need="\n".join("- " + t for t, have in items if not have) or "(empty)",
        have=", ".join(t for t, have in items if have) or "(nothing)")


def _run_claude(prompt, cwd):
    args = [_claude(), "-p", "--output-format", "json", "--model", ASK_MODEL, "--effort", "low",
            "--max-turns", "1", "--tools", "", "--strict-mcp-config", "--disable-slash-commands",
            "--no-session-persistence", "--setting-sources", "",
            "--system-prompt", "You answer a recipe app's search box in the exact line format asked for."]
    proc = subprocess.run(args, input=prompt, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=cwd, timeout=60,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        raise RuntimeError(f"Claude failed: {(proc.stderr or proc.stdout).strip()[:200]}")
    if out.get("is_error"):
        raise RuntimeError(f"Claude error: {str(out.get('result'))[:200]}")
    return out.get("result") or ""


def _add_to_list(library, items, recipe_ids, rows):
    """Adds loose items and whole recipes (at 1x, keeping ticks) to the shopping list."""
    by_id = {r["id"]: r for r in rows}
    with LOCK:
        state = load_state(library)
        lst = state["list"]
        for rid in recipe_ids:
            old = next((x for x in lst if x.get("id") == rid), None)
            have = [i.get("have") for i in old["items"]] if old else []
            lst[:] = [x for x in lst if x.get("id") != rid]
            lst.append({"id": rid, "title": by_id[rid]["title"], "scale": 1,
                        "items": [{"text": t, "have": bool(n < len(have) and have[n])}
                                  for n, t in enumerate(by_id[rid]["items"])]})
        if items:
            extra = next((x for x in lst if x.get("id") == EXTRA), None)
            if not extra:
                extra = {"id": EXTRA, "title": "Added by hand", "scale": 1, "items": []}
                lst.append(extra)
            for t in items:
                found = next((i for i in extra["items"] if i["text"].lower() == t.lower()), None)
                if found:
                    found["have"] = False
                else:
                    extra["items"].append({"text": t, "have": False})
        save_state(library, state)


def ask(library, q, act=True):
    """Hands the search box text to Claude (Haiku, low effort, no tools) with the
    recipes, meal plan, and shopping list. Returns {"ids", "say", "added"} and
    makes any shopping list additions it asked for. With act=False (the page asks
    on its own while you pause typing) it only says what it would add, so a
    half-typed "add eg" never lands on the list; Enter then adds from the cache."""
    q = " ".join(q.split())[:200]
    rows = [r for _, rs in recipes(library) for r in rs]
    with LOCK:
        state = load_state(library)
    prompt = _prompt(q, state, rows)
    key = (q.lower(), hash(prompt))
    reply = ASK_CACHE.get(key)
    if reply is None:
        reply = _run_claude(prompt, library)
    known = {r["id"] for r in rows}
    ids, say, items, add_ids = [], [], [], []
    for line in reply.splitlines():
        line = line.strip().strip("-*`").strip()
        tag, _, rest = line.partition(":")
        tag, rest = tag.strip().upper(), rest.strip().strip("`'\"").split(" | ")[0].strip()
        if tag == "RECIPE" and rest in known and rest not in ids:
            ids.append(rest)
        elif tag == "SAY" and rest:
            say.append(line.partition(":")[2].strip())
        elif tag == "ADD" and rest and len(items) < 30:
            items.append(rest[:120])
        elif tag == "ADD_RECIPE" and rest in known and rest not in add_ids:
            add_ids.append(rest)
    titles = {r["id"]: r["title"] for r in rows}
    changes = items + [f"ingredients for {titles[i]}" for i in add_ids]
    if changes and act:
        ASK_CACHE.pop(key, None)  # a list change never replays from the cache
        _add_to_list(library, items, add_ids, rows)
    else:
        if len(ASK_CACHE) > 200:
            ASK_CACHE.clear()
        ASK_CACHE[key] = reply
    if changes and not act:
        return {"ids": ids, "say": "Press Search to add " + ", ".join(changes) + " to your list.",
                "added": [], "pending": True}
    return {"ids": ids, "say": " ".join(say), "added": changes if act else []}


# ---- stored data ---------------------------------------------------------

def _root(library):
    return os.path.dirname(library)


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def load_state(library):
    path = os.path.join(_root(library), "state", "cook-state.json")
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    state.setdefault("list", [])
    state.setdefault("plan", {})
    return state


def save_state(library, state):
    _write(os.path.join(_root(library), "state", "cook-state.json"), json.dumps(state, indent=1))


def favorites(library):
    try:
        lines = read_text(os.path.join(library, "favorites.txt")).splitlines()
    except OSError:
        return []
    return [l.strip() for l in lines if l.strip() and not l.startswith("#")]


def set_favorite(library, name, on):
    names = [n for n in favorites(library) if n != name]
    if on:
        names.append(name)
    _write(os.path.join(library, "favorites.txt"), "".join(n + "\n" for n in names))


def _to_trash(library, path, sub):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base, ext = os.path.splitext(os.path.basename(path))
    dest = os.path.join(_root(library), "trash", sub, f"{base}.{stamp}{ext}")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    os.replace(path, dest)


def save_recipe(library, path, markdown):
    markdown = markdown.replace("\r\n", "\n").strip() + "\n"
    first = next((l for l in markdown.splitlines() if l.strip()), "")
    if not first.startswith("# ") or len(first) < 3:
        raise ValueError("The first line has to be the title, starting with '# '.")
    backup = os.path.join(_root(library), "trash", "edits", os.path.basename(path))
    os.makedirs(os.path.dirname(backup), exist_ok=True)
    with open(backup[:-3] + time.strftime(".%Y%m%d-%H%M%S.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(read_text(path))
    _write(path, markdown)


def set_notes(library, path, notes):
    """Replaces (or removes) the recipe's final "## My notes" section."""
    lines = read_text(path).replace("\r\n", "\n").split("\n")
    out, skipping = [], False
    for line in lines:
        if line.strip().startswith("## "):
            skipping = line.strip()[3:].strip().lower() == NOTES.lower()
        if not skipping:
            out.append(line)
    text = "\n".join(out).rstrip() + "\n"
    body = [l.rstrip().lstrip("#").strip() if l.lstrip().startswith("#") else l.rstrip()
            for l in notes.replace("\r\n", "\n").split("\n")]
    body = "\n".join(body).strip()
    if body:
        text += f"\n## {NOTES}\n{body}\n"
    _write(path, text)


def delete_recipe(library, path):
    folder, name = os.path.split(path)
    cat, base = os.path.basename(folder), name[:-3]
    for extra in (base + ".jpg", base + ".thumb.jpg"):
        if os.path.isfile(os.path.join(folder, extra)):
            _to_trash(library, os.path.join(folder, extra), cat)
    _to_trash(library, path, cat)
    set_favorite(library, name, False)
    state = load_state(library)
    rel = f"{cat}/{name}"
    for day in state["plan"].values():
        day[:] = [e for e in day if e.get("id") != rel]
    save_state(library, state)


# ---- pages ---------------------------------------------------------------

STYLE = """
:root { --bg:#faf7f0; --card:#fff; --ink:#222; --muted:#6b6b6b; --line:#e6e0d6; --accent:#b4532a; --have:#9a9a9a; --star:#d99a00; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#161412; --card:#221f1c; --ink:#eee; --muted:#a59f97; --line:#38332d; --accent:#e58a5c; --have:#77716a; --star:#f2b632; }
}
* { box-sizing: border-box; }
body { margin:0 auto; background:var(--bg); color:var(--ink); font:17px/1.45 -apple-system, system-ui, sans-serif;
  padding: env(safe-area-inset-top) 16px calc(84px + env(safe-area-inset-bottom)); max-width: 680px; overflow-wrap:anywhere; }
header { display:flex; align-items:center; gap:12px; padding:14px 0 8px; position:sticky; top:0; background:var(--bg); z-index:2; }
header h1 { font-size:20px; margin:0; flex:1; }
a { color:var(--accent); text-decoration:none; }
nav.tabs { position:fixed; left:0; right:0; bottom:0; display:flex; background:var(--card); border-top:1px solid var(--line);
  padding-bottom: env(safe-area-inset-bottom); z-index:3; }
nav.tabs a { flex:1; text-align:center; padding:12px 0; color:var(--muted); font-weight:600; font-size:15px; }
nav.tabs a.on { color:var(--accent); }
.btn { display:inline-block; border:0; border-radius:10px; padding:10px 14px; background:var(--accent); color:#fff;
  font:inherit; font-weight:600; cursor:pointer; }
.btn.quiet { background:transparent; color:var(--accent); border:1px solid var(--line); }
.btn.danger { background:transparent; color:#c0392b; border:1px solid var(--line); }
.btn.small { padding:6px 10px; font-size:15px; }
input[type=search], select, textarea { width:100%; font:inherit; padding:10px 12px; border-radius:10px; border:1px solid var(--line);
  background:var(--card); color:var(--ink); }
input[type=search] { margin-bottom:8px; }
.answer { background:var(--card); border:1px solid var(--line); border-left:4px solid var(--accent); border-radius:10px; padding:10px 12px; margin:0 0 12px; }
textarea { font:15px/1.4 ui-monospace, Menlo, monospace; }
textarea.notes { font:inherit; min-height:100px; }
h2 { font-size:14px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin:20px 0 6px; }
ul.cards { list-style:none; margin:0; padding:0; }
ul.cards li a { display:flex; gap:12px; align-items:center; padding:10px; margin-bottom:8px; background:var(--card);
  border:1px solid var(--line); border-radius:12px; color:var(--ink); }
ul.cards img, ul.cards .noimg { width:64px; height:64px; border-radius:12px; object-fit:cover; flex:none; background:var(--line); }
.t { font-weight:600; } .s { color:var(--muted); font-size:14px; }
.fav { color:var(--star); }
.star { background:none; border:0; font-size:26px; line-height:1; color:var(--muted); cursor:pointer; padding:4px; }
.star.on { color:var(--star); }
.hero { width:100%; border-radius:12px; margin:4px 0 8px; }
.icon { width:96px; height:96px; border-radius:16px; display:block; margin:4px 0 8px; }
.sub { color:var(--muted); margin:0 0 12px; }
.recipe ul, .recipe ol { padding-left:22px; } .recipe li { margin:4px 0; }
.recipe h3 { font-size:15px; margin:12px 0 4px; }
.bar { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin:12px 0; }
.chips { display:flex; gap:6px; margin:6px 0; }
.chip { flex:1; border:1px solid var(--line); background:var(--card); color:var(--ink); border-radius:999px; padding:7px 0;
  font:inherit; font-size:15px; font-weight:600; cursor:pointer; }
.chip.on { background:var(--accent); border-color:var(--accent); color:#fff; }
.item { display:flex; gap:12px; align-items:flex-start; padding:11px 4px; border-bottom:1px solid var(--line); cursor:pointer;
  -webkit-tap-highlight-color: transparent; }
.box { width:24px; height:24px; border-radius:6px; border:2px solid var(--muted); flex:none; margin-top:1px;
  display:flex; align-items:center; justify-content:center; font-size:15px; color:#fff; }
.item.have { color:var(--have); text-decoration:line-through; }
.item.have .box { background:var(--have); border-color:var(--have); }
.from { color:var(--muted); font-size:13px; display:block; text-decoration:none; }
.empty { color:var(--muted); padding:24px 0; text-align:center; }
.hint { color:var(--muted); font-size:14px; }
.day { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:10px 12px; margin-bottom:10px; }
.day.today { border-color:var(--accent); }
.day .d { font-weight:700; }
.meal { display:flex; align-items:center; gap:8px; padding:6px 0; border-bottom:1px solid var(--line); }
.meal a { flex:1; color:var(--ink); }
.meal .x { background:none; border:0; color:var(--muted); font-size:20px; cursor:pointer; padding:0 4px; }
.meal select { width:auto; padding:4px 6px; font-size:14px; }
.day select.add { margin-top:8px; color:var(--muted); }
.toast { position:fixed; left:50%; bottom:90px; transform:translateX(-50%); background:var(--ink); color:var(--bg);
  padding:10px 16px; border-radius:10px; font-size:15px; z-index:5; }
"""


def shell(title, body, back=None, extra="", tab="", data=None):
    nav = f'<a href="{back}" aria-label="Back">&larr;</a>' if back else ""
    tabs = "".join(f'<a href="{href}" id="tab-{key}"{" class=on" if key == tab else ""}>{label}</a>'
                   for key, href, label in (("recipes", "/cook/", "Recipes"), ("plan", "/cook/plan", "Plan"),
                                            ("list", "/cook/list", "List")))
    payload = ""
    if data is not None:
        payload = '<script>var DATA = ' + json.dumps(data).replace("</", "<\\/") + ';</script>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Recipes">
<title>{esc(title)}</title><style>{STYLE}</style></head>
<body><header>{nav}<h1>{esc(title)}</h1>{extra}</header>
{body}
<nav class="tabs">{tabs}</nav>
{payload}<script src="/cook/app.js"></script>
</body></html>"""


def index_page(library):
    favs = set(favorites(library))
    groups = recipes(library)
    starred = [r for _, rows in groups for r in rows if r["id"].split("/")[1] in favs]
    starred.sort(key=lambda r: r["title"].lower())
    parts = ['<input type="search" id="q" placeholder="Search, or ask Claude (&quot;Indian&quot;, &quot;add eggs&quot;)" autocomplete="off" enterkeyhint="search">',
             '<p class="empty" id="asking" hidden></p><p class="answer" id="answer" hidden></p><section id="picks" hidden><h2>Claude\'s picks</h2><ul class="cards"></ul></section>']

    def section(name, rows):
        parts.append(f'<section><h2>{esc(name)}</h2><ul class="cards">')
        for r in rows:
            img = f'<img src="{esc(r["thumb"])}" alt="" loading="lazy">' if r["thumb"] else '<span class="noimg"></span>'
            star = ' <span class="fav">&#9733;</span>' if r["id"].split("/")[1] in favs else ""
            parts.append(f'<li data-id="{esc(r["id"])}" data-q="{esc((r["title"] + " " + r["subtitle"]).lower())}"><a href="/cook/r/{quote(r["id"])}">{img}'
                         f'<span><span class="t">{esc(r["title"])}</span>{star}<br><span class="s">{esc(r["subtitle"])}</span></span></a></li>')
        parts.append("</ul></section>")

    if starred:
        section("Favorites", starred)
    for cat, rows in groups:
        section(NAMES.get(cat, cat.title()), rows)
    if not groups:
        parts.append('<p class="empty">No recipes yet.</p>')
    return shell("Recipes", "\n".join(parts), tab="recipes", data={"page": "index"})


def recipe_page(library, rel):
    recipe = read(library, rel)
    if recipe is None:
        return None
    cat, name = rel.split("/")
    base = name[:-3]
    star = '<button class="star" id="star" aria-label="Favorite">&#9733;</button>'
    out = ['<div class="recipe">']
    if recipe["photo"] and not re.match(r"[a-z]+:", recipe["photo"]):
        out.append(f'<img class="hero" src="/{quote(cat)}/{quote(recipe["photo"])}" alt="">')
    elif os.path.isfile(os.path.join(library, cat, base + ".thumb.jpg")):
        out.append(f'<img class="icon" src="/{quote(cat)}/{quote(base + ".thumb.jpg")}" alt="">')
    if recipe["subtitle"]:
        out.append(f'<p class="sub">{esc(recipe["subtitle"])}</p>')
    for section in recipe["sections"]:
        is_ing = section["name"].lower().startswith("ingredient")
        if section["name"].lower() == NOTES.lower():
            continue
        if is_ing:
            out.append('<div class="bar"><button class="btn" id="add">Add to shopping list</button>'
                       '<select id="plan" style="width:auto"><option value="">Add to plan&hellip;</option></select></div>')
        out.append(f'<h2>{esc(section["name"])}</h2>')
        if is_ing:
            out.append('<div class="chips" id="scale">' + "".join(
                f'<button class="chip{" on" if s == 1 else ""}" data-s="{s}">{label}</button>'
                for s, label in ((1, "1&times;"), (0.5, "&frac12;&times;"), (2, "2&times;"), (3, "3&times;"), (5, "5&times;"))) + "</div>")
        open_tag = None
        for kind, text in section["items"]:
            tag = {"bullet": "ul", "step": "ol"}.get(kind)
            if tag != open_tag:
                if open_tag:
                    out.append(f"</{open_tag}>")
                if tag:
                    out.append(f"<{tag}>")
                open_tag = tag
            if kind == "head":
                out.append(f"<h3>{esc(text)}</h3>")
            elif is_ing and kind == "bullet":
                out.append(f'<li class="ing" data-raw="{esc(text)}">{esc(text)}</li>')
            elif tag:
                out.append(f"<li>{esc(text)}</li>")
            else:
                out.append(f"<p>{esc(text)}</p>")
        if open_tag:
            out.append(f"</{open_tag}>")
    notes = notes_of(recipe)
    out.append(f'<h2>{esc(NOTES)}</h2><div id="notes-view">'
               + ("".join(f"<p>{esc(l)}</p>" for l in notes.split("\n") if l.strip())
                  or '<p class="hint">No notes yet.</p>')
               + '</div><div id="notes-edit" hidden><textarea class="notes" id="notes-text"></textarea>'
               '<div class="bar"><button class="btn" id="notes-save">Save notes</button>'
               '<button class="btn quiet" id="notes-cancel">Cancel</button></div></div>'
               '<div class="bar"><button class="btn quiet" id="notes-open">Edit notes</button>'
               f'<a class="btn quiet" href="/cook/edit/{quote(rel)}">Edit recipe</a>'
               '<button class="btn danger" id="delete">Delete</button></div>')
    out.append("</div>")
    data = {"page": "recipe", "id": rel, "title": recipe["title"], "items": ingredients_of(recipe),
            "notes": notes, "fav": name in favorites(library)}
    return shell(recipe["title"] or "Recipe", "\n".join(out), back="/cook/", extra=star, tab="recipes", data=data)


def edit_page(library, rel):
    path = recipe_path(library, rel)
    if not path:
        return None
    body = (f'<p class="hint">The first line is the title (<code># Title</code>). Keep the photo line as it is. '
            f'The old version is kept in the trash folder on the PC.</p>'
            f'<textarea id="md" rows="22">{esc(read_text(path))}</textarea>'
            '<div class="bar"><button class="btn" id="save">Save</button>'
            f'<a class="btn quiet" href="/cook/r/{quote(rel)}">Cancel</a>'
            '<span style="flex:1"></span><button class="btn danger" id="delete">Delete recipe</button></div>')
    return shell("Edit recipe", body, back=f"/cook/r/{quote(rel)}", tab="recipes", data={"page": "edit", "id": rel})


def plan_page(library):
    all_recipes = [{"id": r["id"], "title": r["title"], "items": r["items"]} for _, rows in recipes(library) for r in rows]
    body = """<div class="bar"><button class="btn quiet small" id="prev">&larr; Earlier</button>
<span id="week" style="flex:1;text-align:center;font-weight:600"></span>
<button class="btn quiet small" id="next">Later &rarr;</button></div>
<div id="days"></div>
<div class="bar"><button class="btn" id="tolist">Add these 7 days to shopping list</button></div>
<p class="hint">Recipes planned more than once in the week are added once, with their amounts added together.</p>"""
    return shell("Meal plan", body, tab="plan", data={"page": "plan", "recipes": all_recipes})


def list_page():
    body = """<p class="hint">Tap anything you already have. What's left is your shopping list.</p>
<div class="bar"><button class="btn" id="copy">Copy list</button><button class="btn quiet" id="clear">Clear all</button></div>
<div id="list"></div>"""
    return shell("Shopping list", body, tab="list", data={"page": "list"})


APP_JS = r"""
(function () {
var FR = {'½':1/2,'¼':1/4,'¾':3/4,'⅓':1/3,'⅔':2/3,'⅛':1/8,'⅜':3/8,'⅝':5/8,'⅞':7/8};
var NUM = '(?:\\d+\\s+\\d+/\\d+|\\d+/\\d+|\\d+(?:\\.\\d+)?(?:\\s*[½¼¾⅓⅔⅛⅜⅝⅞])?|[½¼¾⅓⅔⅛⅜⅝⅞])';
var LEAD = new RegExp('^(' + NUM + ')(?:(\\s*[-–]\\s*)(' + NUM + '))?');
var PAREN = new RegExp('^(\\s+([A-Za-z.]+)\\s*\\()(' + NUM + ')(?:(\\s*[-–]\\s*)(' + NUM + '))?');
var NOSCALE = /^(cans?|packages?|pkgs?|jars?|box(es)?|bags?|sticks?)$/i;
function val(s) {
  s = s.trim(); var m;
  if ((m = s.match(/^(\d+)\s+(\d+)\/(\d+)$/))) return +m[1] + m[2] / m[3];
  if ((m = s.match(/^(\d+)\/(\d+)$/))) return m[1] / m[2];
  if ((m = s.match(/^(\d+(?:\.\d+)?)?\s*([½¼¾⅓⅔⅛⅜⅝⅞])?$/))) return (m[1] ? +m[1] : 0) + (m[2] ? FR[m[2]] : 0);
  return NaN;
}
var NICE = [[0,''],[1/8,'⅛'],[1/4,'¼'],[1/3,'⅓'],[3/8,'⅜'],[1/2,'½'],[5/8,'⅝'],[2/3,'⅔'],[3/4,'¾'],[7/8,'⅞'],[1,'']];
function fmt(v) {
  if (v >= 10) return String(Math.round(v));
  var w = Math.floor(v), f = v - w, best = NICE[0];
  NICE.forEach(function (n) { if (Math.abs(n[0] - f) < Math.abs(best[0] - f)) best = n; });
  if (best[0] === 1) { w += 1; best = NICE[0]; }
  if (!w && !best[1]) return '⅛';
  return (w ? String(w) : '') + best[1];
}
function scaleLine(text, s) {
  if (s === 1) return text;
  var m = text.match(LEAD); if (!m) return text;
  var out = fmt(val(m[1]) * s) + (m[3] ? m[2] + fmt(val(m[3]) * s) : ''), rest = text.slice(m[0].length);
  var p = rest.match(PAREN);
  if (p && !NOSCALE.test(p[2])) rest = p[1] + fmt(val(p[3]) * s) + (p[5] ? p[4] + fmt(val(p[5]) * s) : '') + rest.slice(p[0].length);
  return out + rest;
}
window.scaleLine = scaleLine;

function $(id) { return document.getElementById(id); }
function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
function toast(msg) { var t = el('div', 'toast', msg); document.body.appendChild(t); setTimeout(function () { t.remove(); }, 2200); }
function api(path, data) {
  return fetch('/cook/api/' + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) })
    .then(function (r) { return r.text().then(function (t) { if (!r.ok) throw new Error(t || r.statusText); return t ? JSON.parse(t) : {}; }); })
    .catch(function (e) { toast(e.message); throw e; });
}
function state() { return fetch('/cook/api/state', { cache: 'no-store' }).then(function (r) { return r.json(); }); }
function link(id) { return '/cook/r/' + id.split('/').map(encodeURIComponent).join('/'); }
function count(st) {
  var n = 0; st.list.forEach(function (r) { r.items.forEach(function (i) { if (!i.have) n++; }); });
  $('tab-list').textContent = n ? 'List (' + n + ')' : 'List';
}
// put one recipe on the list at scale s, keeping what was already ticked
function putOnList(list, r, s) {
  var old = list.filter(function (x) { return x.id === r.id; })[0];
  var rest = list.filter(function (x) { return x.id !== r.id; });
  rest.push({ id: r.id, title: r.title, scale: s, items: r.items.map(function (t, i) {
    return { text: scaleLine(t, s), have: !!(old && old.items[i] && old.items[i].have) }; }) });
  return rest;
}
function iso(d) { return d.getFullYear() + '-' + ('0' + (d.getMonth() + 1)).slice(-2) + '-' + ('0' + d.getDate()).slice(-2); }
function days(start, n) { var out = []; for (var i = 0; i < n; i++) { var d = new Date(start); d.setDate(d.getDate() + i); out.push(d); } return out; }
function dayName(d) { return d.toLocaleDateString(undefined, { weekday: 'long', month: 'short', day: 'numeric' }); }
var SCALES = [[0.5, '½×'], [1, '1×'], [2, '2×'], [3, '3×'], [5, '5×']];

var D = window.DATA || {};
state().then(function (st) {
  // move a list saved in this browser by the first version onto the PC
  try {
    var local = JSON.parse(localStorage.getItem('cook.list') || '[]');
    if (local.length && !st.list.length) { st.list = local; api('list', st.list); }
    localStorage.removeItem('cook.list');
  } catch (e) {}
  count(st);
  ({ index: index, recipe: recipe, edit: edit, list: list, plan: plan })[D.page](st);
});

function index() {
  // typing filters by title at once; a pause (or Enter) also asks Claude on the PC,
  // whose picks go on top so "Indian" finds palak paneer
  var box = $('q'), picks = $('picks'), asking = $('asking'), answer = $('answer'), timer = null, asked = '', seq = 0;
  function filter(q) {
    document.querySelectorAll('section:not(#picks)').forEach(function (s) {
      var shown = 0;
      s.querySelectorAll('li').forEach(function (li) {
        var ok = !q || li.dataset.q.indexOf(q) >= 0; li.style.display = ok ? '' : 'none'; if (ok) shown++;
      });
      s.style.display = shown ? '' : 'none';
    });
  }
  function ask(act) {
    clearTimeout(timer);
    var q = box.value.trim();
    if (q.length < 3 || (q === asked && !act)) return;
    asked = q; var mine = ++seq; asking.textContent = 'Asking Claude…'; asking.hidden = false;
    fetch('/cook/api/ask', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ q: q, act: !!act }) })
      .then(function (r) { return r.text().then(function (t) { if (!r.ok) throw new Error(t || r.statusText); return JSON.parse(t); }); })
      .then(function (res) {
        if (mine !== seq) return;  // a newer ask (or typing) replaced this one
        var ul = picks.querySelector('ul'), seen = {}; ul.innerHTML = '';
        res.ids.forEach(function (id) {
          var li = document.querySelector('section:not(#picks) li[data-id="' + CSS.escape(id) + '"]');
          if (li && !seen[id]) { seen[id] = 1; var c = li.cloneNode(true); c.style.display = ''; ul.appendChild(c); }
        });
        picks.hidden = !ul.children.length;
        // a question about the plan or list, or a list change, comes back as a sentence
        var said = res.say || (res.added.length ? 'Added ' + res.added.join(', ') + ' to your list.' : '');
        answer.textContent = said; answer.hidden = !said;
        if (res.added.length) state().then(count);
        asking.textContent = 'Claude found nothing for that.'; asking.hidden = !!(ul.children.length || said);
        // what Claude picked is on top, so drop the same rows from the title matches below
        document.querySelectorAll('section:not(#picks)').forEach(function (s) {
          var shown = 0;
          s.querySelectorAll('li').forEach(function (li) { if (seen[li.dataset.id]) li.style.display = 'none'; if (li.style.display !== 'none') shown++; });
          s.style.display = shown ? '' : 'none';
        });
      })
      .catch(function (e) { if (mine === seq) { asking.textContent = 'Claude search failed: ' + e.message; asked = ''; } });
  }
  box.addEventListener('input', function () {
    var q = box.value.trim().toLowerCase();
    picks.hidden = true; asking.hidden = true; answer.hidden = true; asked = ''; seq++;
    filter(q);
    clearTimeout(timer);
    if (q.length >= 3) timer = setTimeout(function () { ask(false); }, 900);
  });
  box.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); box.blur(); ask(true); } });
}

function recipe(st) {
  var scale = 1, star = $('star');
  star.classList.toggle('on', D.fav);
  star.addEventListener('click', function () {
    var on = !star.classList.contains('on');
    api('favorite', { id: D.id, on: on }).then(function () { star.classList.toggle('on', on); toast(on ? 'Added to favorites' : 'Removed from favorites'); });
  });
  var chips = $('scale');
  if (chips) chips.addEventListener('click', function (e) {
    var b = e.target.closest('.chip'); if (!b) return;
    scale = +b.dataset.s;
    chips.querySelectorAll('.chip').forEach(function (c) { c.classList.toggle('on', c === b); });
    document.querySelectorAll('li.ing').forEach(function (li) {
      var t = scaleLine(li.dataset.raw, scale);
      li.textContent = t; li.style.fontWeight = (scale !== 1 && t !== li.dataset.raw) ? '600' : '';
    });
  });
  if ($('add')) $('add').addEventListener('click', function () {
    st.list = putOnList(st.list, D, scale);
    api('list', st.list).then(function () { location.href = '/cook/list'; });
  });
  var sel = $('plan');
  if (sel) {
    days(new Date(), 14).forEach(function (d, i) {
      var o = el('option', null, i === 0 ? 'Today' : i === 1 ? 'Tomorrow' : dayName(d)); o.value = iso(d); sel.appendChild(o);
    });
    sel.addEventListener('change', function () {
      var day = sel.value; if (!day) return;
      (st.plan[day] = st.plan[day] || []).push({ id: D.id, scale: scale });
      api('plan', st.plan).then(function () { toast('Planned for ' + sel.options[sel.selectedIndex].text); sel.value = ''; });
    });
  }
  $('notes-open').addEventListener('click', function () {
    $('notes-text').value = D.notes; $('notes-edit').hidden = false; $('notes-view').hidden = true; $('notes-text').focus();
  });
  $('notes-cancel').addEventListener('click', function () { $('notes-edit').hidden = true; $('notes-view').hidden = false; });
  $('notes-save').addEventListener('click', function () {
    api('notes', { id: D.id, notes: $('notes-text').value }).then(function () { location.reload(); });
  });
  confirmDelete();
}

function confirmDelete() {
  $('delete').addEventListener('click', function () {
    if (!confirm('Delete this recipe? It moves to the trash folder on the PC.')) return;
    api('delete', { id: D.id }).then(function () { location.href = '/cook/'; });
  });
}

function edit() {
  $('save').addEventListener('click', function () {
    api('save', { id: D.id, markdown: $('md').value }).then(function () { location.href = link(D.id); });
  });
  confirmDelete();
}

function list(st) {
  function save() { count(st); return api('list', st.list); }
  function render() {
    var root = $('list'); root.innerHTML = '';
    if (!st.list.length) { root.appendChild(el('p', 'empty', 'Nothing here yet. Open a recipe and tap "Add to shopping list", or use the meal plan.')); return; }
    var need = el('div'), have = el('div'), nNeed = 0, nHave = 0;
    st.list.forEach(function (r) {
      r.items.forEach(function (it) {
        var row = el('div', 'item' + (it.have ? ' have' : ''));
        row.appendChild(el('span', 'box', it.have ? '✓' : ''));
        var t = el('span', null, it.text); if (st.list.length > 1) t.appendChild(el('span', 'from', r.title));
        row.appendChild(t);
        row.addEventListener('click', function () { it.have = !it.have; save(); render(); });
        if (it.have) { have.appendChild(row); nHave++; } else { need.appendChild(row); nNeed++; }
      });
    });
    root.appendChild(el('h2', null, 'Need to buy (' + nNeed + ')'));
    root.appendChild(nNeed ? need : el('p', 'empty', 'You have everything.'));
    if (nHave) { root.appendChild(el('h2', null, 'Already have (' + nHave + ')')); root.appendChild(have); }
    root.appendChild(el('h2', null, 'Recipes on this list'));
    st.list.forEach(function (r, ri) {
      var row = el('div', 'bar');
      var a = el('a', null, r.title + (r.scale && r.scale !== 1 ? ' (' + fmt(r.scale) + '×)' : '')); if (r.id !== 'extra') a.href = link(r.id); a.style.flex = '1';
      var x = el('button', 'btn quiet small', 'Remove');
      x.addEventListener('click', function () { st.list.splice(ri, 1); save(); render(); });
      row.appendChild(a); row.appendChild(x); root.appendChild(row);
    });
  }
  $('clear').addEventListener('click', function () { if (confirm('Clear the whole list?')) { st.list = []; save(); render(); } });
  $('copy').addEventListener('click', function () {
    var lines = []; st.list.forEach(function (r) { r.items.forEach(function (i) { if (!i.have) lines.push(i.text); }); });
    var text = lines.join('\n'), btn = this;
    function done() { btn.textContent = 'Copied'; setTimeout(function () { btn.textContent = 'Copy list'; }, 1500); }
    if (navigator.share) { navigator.share({ text: text }).catch(function () {}); return; }
    if (navigator.clipboard) { navigator.clipboard.writeText(text).then(done); return; }
    var ta = el('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); done();
  });
  render();
}

function plan(st) {
  var byId = {}; D.recipes.forEach(function (r) { byId[r.id] = r; });
  var offset = 0;
  function week() { var s = new Date(); s.setHours(12, 0, 0, 0); s.setDate(s.getDate() + offset * 7); return days(s, 7); }
  function save() { return api('plan', st.plan); }
  function render() {
    var root = $('days'), ds = week(), today = iso(new Date()); root.innerHTML = '';
    $('week').textContent = offset === 0 ? 'Next 7 days' : ds[0].toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) + ' – ' + ds[6].toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    ds.forEach(function (d) {
      var key = iso(d), box = el('div', 'day' + (key === today ? ' today' : ''));
      box.appendChild(el('div', 'd', key === today ? 'Today' : dayName(d)));
      (st.plan[key] || []).forEach(function (m, mi) {
        var r = byId[m.id]; if (!r) return;
        var row = el('div', 'meal'), a = el('a', null, r.title); a.href = link(r.id); row.appendChild(a);
        var s = el('select');
        SCALES.forEach(function (o) { var op = el('option', null, o[1]); op.value = o[0]; if (o[0] === (m.scale || 1)) op.selected = true; s.appendChild(op); });
        s.addEventListener('change', function () { m.scale = +s.value; save(); });
        var x = el('button', 'x', '×'); x.setAttribute('aria-label', 'Remove');
        x.addEventListener('click', function () { st.plan[key].splice(mi, 1); if (!st.plan[key].length) delete st.plan[key]; save(); render(); });
        row.appendChild(s); row.appendChild(x); box.appendChild(row);
      });
      var add = el('select', 'add'); add.appendChild(el('option', null, '+ Add a recipe'));
      D.recipes.forEach(function (r) { var o = el('option', null, r.title); o.value = r.id; add.appendChild(o); });
      add.addEventListener('change', function () {
        if (!add.value) return; (st.plan[key] = st.plan[key] || []).push({ id: add.value, scale: 1 }); save(); render();
      });
      box.appendChild(add); root.appendChild(box);
    });
  }
  $('prev').addEventListener('click', function () { offset--; render(); });
  $('next').addEventListener('click', function () { offset++; render(); });
  $('tolist').addEventListener('click', function () {
    var total = {}, order = [];
    week().forEach(function (d) { (st.plan[iso(d)] || []).forEach(function (m) {
      if (!byId[m.id]) return; if (!(m.id in total)) { total[m.id] = 0; order.push(m.id); } total[m.id] += m.scale || 1; }); });
    if (!order.length) { toast('Nothing planned for these days yet.'); return; }
    order.forEach(function (id) { st.list = putOnList(st.list, byId[id], total[id]); });
    api('list', st.list).then(function () { location.href = '/cook/list'; });
  });
  render();
}
})();
"""

HTML = "text/html; charset=utf-8"
JSON = "application/json; charset=utf-8"


def handle(path, library):
    """Returns (status, body, content type) for a GET under /cook/, or None."""
    library = os.path.abspath(library)
    if path in ("/", "/cook", "/cook/"):
        return 200, index_page(library), HTML
    if path == "/cook/app.js":
        return 200, APP_JS, "application/javascript; charset=utf-8"
    if path == "/cook/api/state":
        with LOCK:
            state = load_state(library)
        state["favorites"] = favorites(library)
        return 200, json.dumps(state), JSON
    if path in ("/cook/list", "/cook/list/"):
        return 200, list_page(), HTML
    if path in ("/cook/plan", "/cook/plan/"):
        return 200, plan_page(library), HTML
    for prefix, page in (("/cook/r/", recipe_page), ("/cook/edit/", edit_page)):
        if path.startswith(prefix):
            body = page(library, unquote(path[len(prefix):]))
            if body:
                return 200, body, HTML
            return 404, shell("Not found", '<p class="empty">That recipe is gone.</p>', back="/cook/"), HTML
    return None


def handle_post(path, body, trusted, library):
    """JSON writes from the pages. trusted means the request came through
    Tailscale (or this PC); only then may recipes themselves be changed."""
    if not path.startswith("/cook/api/"):
        return None
    library = os.path.abspath(library)
    action = path[len("/cook/api/"):]
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return 400, "That wasn't valid JSON.", "text/plain; charset=utf-8"
    if action == "ask":
        # not under LOCK: it takes a few seconds and only reads
        q = str(data.get("q", "")).strip() if isinstance(data, dict) else ""
        if not q:
            return 400, "Nothing to search for.", "text/plain; charset=utf-8"
        try:
            return 200, json.dumps(ask(library, q, data.get("act", True) is not False)), JSON
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            return 502, str(e), "text/plain; charset=utf-8"
    with LOCK:
        if action in ("list", "plan"):
            if not isinstance(data, (list, dict)):
                return 400, "Bad data.", "text/plain; charset=utf-8"
            state = load_state(library)
            state[action] = data
            save_state(library, state)
            return 200, "{}", JSON
        rel = str(data.get("id", "")) if isinstance(data, dict) else ""
        rpath = recipe_path(library, rel)
        if action not in ("favorite", "notes", "save", "delete"):
            return 404, "Unknown action.", "text/plain; charset=utf-8"
        if not rpath:
            return 404, "That recipe is gone.", "text/plain; charset=utf-8"
        if action == "favorite":
            set_favorite(library, os.path.basename(rpath), bool(data.get("on")))
            return 200, "{}", JSON
        if not trusted:
            return 403, "Editing only works over Tailscale. Open the ts.net address instead.", "text/plain; charset=utf-8"
        try:
            if action == "notes":
                set_notes(library, rpath, str(data.get("notes", "")))
            elif action == "save":
                save_recipe(library, rpath, str(data.get("markdown", "")))
            else:
                delete_recipe(library, rpath)
        except ValueError as e:
            return 400, str(e), "text/plain; charset=utf-8"
        return 200, "{}", JSON
