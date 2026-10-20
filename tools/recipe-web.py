"""Phone-friendly recipe pages, served by serve-recipes.py under /cook/.

  /cook/                    all recipes, grouped by category, with search
  /cook/r/<cat>/<file>.md   one recipe, with an "Add to shopping list" button
  /cook/list                shopping list: tap what you already have

The shopping list lives in the phone's browser (localStorage), so nothing is
written on this PC. Meant to be opened over Tailscale (see README.md).
"""
import html
import json
import os
import re
from urllib.parse import quote, unquote

CATEGORIES = ["breakfast", "mains", "sides", "soups", "baking", "desserts", "drinks", "misc"]
NAMES = {"misc": "Other"}


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


def read(library, rel):
    path = os.path.abspath(os.path.join(library, rel))
    if not path.startswith(library + os.sep) or not path.endswith(".md") or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return parse(f.read())


def photo_url(library, cat, base):
    """The dish photo, else the 80x80 stand-in icon (<name>.thumb.jpg), else ""."""
    for candidate in (base + ".jpg", base + ".thumb.jpg"):
        if os.path.isfile(os.path.join(library, cat, candidate)):
            return f"/{quote(cat)}/{quote(candidate)}"
    return ""


def recipes(library):
    """[(category, [(rel, title, subtitle, thumb)])] in display order."""
    found = {}
    for cat in os.listdir(library):
        folder = os.path.join(library, cat)
        if not os.path.isdir(folder) or cat.startswith("."):
            continue
        for name in os.listdir(folder):
            if not name.endswith(".md"):
                continue
            recipe = read(library, os.path.join(cat, name))
            title = recipe["title"] or name[:-3].split("--")[-1].replace("-", " ")
            base = name[:-3]
            thumb = ""
            thumb = photo_url(library, cat, base)
            found.setdefault(cat, []).append((f"{cat}/{name}", title, recipe["subtitle"], thumb))
    order = sorted(found, key=lambda c: (CATEGORIES.index(c) if c in CATEGORIES else 99, c))
    return [(c, sorted(found[c], key=lambda r: r[1].lower())) for c in order]


STYLE = """
:root { --bg:#faf7f0; --card:#fff; --ink:#222; --muted:#6b6b6b; --line:#e6e0d6; --accent:#b4532a; --have:#9a9a9a; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#161412; --card:#221f1c; --ink:#eee; --muted:#a59f97; --line:#38332d; --accent:#e58a5c; --have:#77716a; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:17px/1.45 -apple-system, system-ui, sans-serif;
  padding: env(safe-area-inset-top) 16px calc(24px + env(safe-area-inset-bottom)); max-width: 680px; margin: 0 auto; }
header { display:flex; align-items:center; gap:12px; padding:14px 0 8px; position:sticky; top:0; background:var(--bg); z-index:2; }
header h1 { font-size:20px; margin:0; flex:1; }
a { color:var(--accent); text-decoration:none; }
.btn { display:inline-block; border:0; border-radius:10px; padding:10px 14px; background:var(--accent); color:#fff;
  font:inherit; font-weight:600; cursor:pointer; }
.btn.quiet { background:transparent; color:var(--accent); border:1px solid var(--line); }
input[type=search] { width:100%; font:inherit; padding:10px 12px; border-radius:10px; border:1px solid var(--line);
  background:var(--card); color:var(--ink); margin-bottom:8px; }
h2 { font-size:14px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin:20px 0 6px; }
ul.cards { list-style:none; margin:0; padding:0; }
ul.cards li a { display:flex; gap:12px; align-items:center; padding:10px; margin-bottom:8px; background:var(--card);
  border:1px solid var(--line); border-radius:12px; color:var(--ink); }
ul.cards img, ul.cards .noimg { width:64px; height:64px; border-radius:12px; object-fit:cover; flex:none; background:var(--line); }
.t { font-weight:600; } .s { color:var(--muted); font-size:14px; }
.hero { width:100%; border-radius:12px; margin:4px 0 8px; }
.icon { width:96px; height:96px; border-radius:16px; display:block; margin:4px 0 8px; }
.sub { color:var(--muted); margin:0 0 12px; }
.recipe ul, .recipe ol { padding-left:22px; } .recipe li { margin:4px 0; }
.recipe h3 { font-size:15px; margin:12px 0 4px; }
.bar { display:flex; gap:8px; flex-wrap:wrap; margin:12px 0; }
.item { display:flex; gap:12px; align-items:flex-start; padding:11px 4px; border-bottom:1px solid var(--line); cursor:pointer;
  -webkit-tap-highlight-color: transparent; }
.box { width:24px; height:24px; border-radius:6px; border:2px solid var(--muted); flex:none; margin-top:1px;
  display:flex; align-items:center; justify-content:center; font-size:15px; color:#fff; }
.item.have { color:var(--have); text-decoration:line-through; }
.item.have .box { background:var(--have); border-color:var(--have); }
.from { color:var(--muted); font-size:13px; display:block; text-decoration:none; }
.empty { color:var(--muted); padding:24px 0; text-align:center; }
.hint { color:var(--muted); font-size:14px; }
"""


def shell(title, body, back=None, extra=""):
    nav = f'<a href="{back}" aria-label="Back">&larr;</a>' if back else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Recipes">
<title>{esc(title)}</title><style>{STYLE}</style></head>
<body><header>{nav}<h1>{esc(title)}</h1>{extra}</header>
{body}
</body></html>"""


LIST_LINK = '<a class="btn quiet" href="/cook/list" id="listlink">List</a>'
COUNT_JS = """<script>
try { var n = 0, l = JSON.parse(localStorage.getItem('cook.list') || '[]');
  l.forEach(function (r) { r.items.forEach(function (i) { if (!i.have) n++; }); });
  if (n) document.getElementById('listlink').textContent = 'List (' + n + ')'; } catch (e) {}
</script>"""


def index_page(library):
    parts = ['<input type="search" id="q" placeholder="Search recipes" autocomplete="off">']
    groups = recipes(library)
    for cat, rows in groups:
        parts.append(f'<section><h2>{esc(NAMES.get(cat, cat.title()))}</h2><ul class="cards">')
        for rel, title, subtitle, thumb in rows:
            img = f'<img src="{esc(thumb)}" alt="" loading="lazy">' if thumb else '<span class="noimg"></span>'
            parts.append(f'<li data-q="{esc((title + " " + subtitle).lower())}"><a href="/cook/r/{quote(rel)}">{img}'
                         f'<span><span class="t">{esc(title)}</span><br><span class="s">{esc(subtitle)}</span></span></a></li>')
        parts.append("</ul></section>")
    if not groups:
        parts.append('<p class="empty">No recipes yet.</p>')
    parts.append("""<script>
document.getElementById('q').addEventListener('input', function () {
  var q = this.value.trim().toLowerCase();
  document.querySelectorAll('section').forEach(function (s) {
    var shown = 0;
    s.querySelectorAll('li').forEach(function (li) {
      var ok = !q || li.dataset.q.indexOf(q) >= 0; li.style.display = ok ? '' : 'none'; if (ok) shown++;
    });
    s.style.display = shown ? '' : 'none';
  });
});
</script>""" + COUNT_JS)
    return shell("Recipes", "\n".join(parts), extra=LIST_LINK)


def recipe_page(library, rel):
    recipe = read(library, rel)
    if recipe is None:
        return None
    cat = rel.split("/")[0]
    out = ['<div class="recipe">']
    base = os.path.basename(rel)[:-3]
    if recipe["photo"] and not re.match(r"[a-z]+:", recipe["photo"]):
        out.append(f'<img class="hero" src="/{quote(cat)}/{quote(recipe["photo"])}" alt="">')
    elif os.path.isfile(os.path.join(library, cat, base + ".thumb.jpg")):
        out.append(f'<img class="icon" src="/{quote(cat)}/{quote(base + ".thumb.jpg")}" alt="">')
    if recipe["subtitle"]:
        out.append(f'<p class="sub">{esc(recipe["subtitle"])}</p>')
    ingredients = []
    for section in recipe["sections"]:
        is_ing = section["name"].lower().startswith("ingredient")
        if is_ing:
            out.append('<div class="bar"><button class="btn" id="add">Add to shopping list</button></div>')
        out.append(f'<h2>{esc(section["name"])}</h2>')
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
            elif tag:
                out.append(f"<li>{esc(text)}</li>")
            else:
                out.append(f"<p>{esc(text)}</p>")
            if is_ing and kind == "bullet":
                ingredients.append(text)
        if open_tag:
            out.append(f"</{open_tag}>")
    out.append("</div>")
    data = json.dumps({"id": rel, "title": recipe["title"], "items": ingredients}).replace("</", "<\\/")
    out.append(f"""<script>
var R = {data};
var add = document.getElementById('add');
if (add) add.addEventListener('click', function () {{
  var l = []; try {{ l = JSON.parse(localStorage.getItem('cook.list') || '[]'); }} catch (e) {{}}
  var old = l.filter(function (r) {{ return r.id === R.id; }})[0];
  var had = {{}}; if (old) old.items.forEach(function (i) {{ had[i.text] = i.have; }});
  l = l.filter(function (r) {{ return r.id !== R.id; }});
  l.push({{id: R.id, title: R.title, items: R.items.map(function (t) {{ return {{text: t, have: !!had[t]}}; }})}});
  localStorage.setItem('cook.list', JSON.stringify(l));
  location.href = '/cook/list';
}});
</script>{COUNT_JS}""")
    return shell(recipe["title"] or "Recipe", "\n".join(out), back="/cook/", extra=LIST_LINK)


def list_page():
    body = """<p class="hint">Tap anything you already have. What's left is your shopping list.</p>
<div class="bar"><button class="btn" id="copy">Copy list</button><button class="btn quiet" id="clear">Clear all</button></div>
<div id="list"></div>
<script>
function load() { try { return JSON.parse(localStorage.getItem('cook.list') || '[]'); } catch (e) { return []; } }
function save(l) { localStorage.setItem('cook.list', JSON.stringify(l)); }
function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
function render() {
  var l = load(), root = document.getElementById('list'); root.innerHTML = '';
  if (!l.length) { root.appendChild(el('p', 'empty', 'Nothing here yet. Open a recipe and tap "Add to shopping list".')); return; }
  var need = el('div'), have = el('div'), nNeed = 0, nHave = 0;
  l.forEach(function (r, ri) {
    r.items.forEach(function (it, ii) {
      var row = el('div', 'item' + (it.have ? ' have' : ''));
      var box = el('span', 'box', it.have ? '\\u2713' : ''); row.appendChild(box);
      var t = el('span', null, it.text); if (l.length > 1) t.appendChild(el('span', 'from', r.title));
      row.appendChild(t);
      row.addEventListener('click', function () { var m = load(); m[ri].items[ii].have = !m[ri].items[ii].have; save(m); render(); });
      if (it.have) { have.appendChild(row); nHave++; } else { need.appendChild(row); nNeed++; }
    });
  });
  root.appendChild(el('h2', null, 'Need to buy (' + nNeed + ')'));
  root.appendChild(nNeed ? need : el('p', 'empty', 'You have everything.'));
  if (nHave) { root.appendChild(el('h2', null, 'Already have (' + nHave + ')')); root.appendChild(have); }
  root.appendChild(el('h2', null, 'Recipes on this list'));
  l.forEach(function (r, ri) {
    var row = el('div', 'bar');
    var a = el('a', null, r.title); a.href = '/cook/r/' + r.id.split('/').map(encodeURIComponent).join('/'); a.style.flex = '1';
    var x = el('button', 'btn quiet', 'Remove');
    x.addEventListener('click', function () { var m = load(); m.splice(ri, 1); save(m); render(); });
    row.appendChild(a); row.appendChild(x); root.appendChild(row);
  });
}
document.getElementById('clear').addEventListener('click', function () { if (confirm('Clear the whole list?')) { save([]); render(); } });
document.getElementById('copy').addEventListener('click', function () {
  var lines = []; load().forEach(function (r) { r.items.forEach(function (i) { if (!i.have) lines.push(i.text); }); });
  var text = lines.join('\\n'), btn = this;
  function done() { btn.textContent = 'Copied'; setTimeout(function () { btn.textContent = 'Copy list'; }, 1500); }
  if (navigator.share) { navigator.share({ text: text }).catch(function () {}); return; }
  if (navigator.clipboard) { navigator.clipboard.writeText(text).then(done); return; }
  var ta = el('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); done();
});
render();
</script>"""
    return shell("Shopping list", body, back="/cook/")


def handle(path, library):
    """Returns (status, html) for a /cook/ path, or None if it isn't one."""
    library = os.path.abspath(library)
    if path in ("/", "/cook", "/cook/"):
        return 200, index_page(library)
    if path in ("/cook/list", "/cook/list/"):
        return 200, list_page()
    if path.startswith("/cook/r/"):
        page = recipe_page(library, unquote(path[len("/cook/r/"):]).replace("\\", "/"))
        return (200, page) if page else (404, shell("Not found", '<p class="empty">That recipe is gone.</p>', back="/cook/"))
    return None
