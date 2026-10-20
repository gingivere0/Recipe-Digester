"""Builds library/index.txt, the recipe list the Nook's cook-sync downloads.

Recipes live in library/<category>/. The Nook saves each recipe under its
bare file name, so files are renamed to <category>--<name>.md to keep names
unique across folders. The index groups links by category (blank line
between groups), sorted by title within each group. It must not contain
heading lines: cook-sync treats a file whose first line starts with "# " as
a single recipe, not a list.
"""
import os
import sys
from urllib.parse import quote

LIBRARY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "library")


def title_of(path):
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            if line.startswith("# "):
                return line[2:].strip()
    return os.path.splitext(os.path.basename(path))[0].replace("-", " ").replace("_", " ")


def build(library=LIBRARY):
    library = os.path.abspath(library)
    groups = []
    seen = {}
    for cat in sorted(os.listdir(library), key=str.lower):
        folder = os.path.join(library, cat)
        if not os.path.isdir(folder) or cat.startswith("."):
            continue
        entries = []
        for name in os.listdir(folder):
            if not name.lower().endswith((".md", ".txt")):
                continue
            prefix = cat + "--"
            if not name.startswith(prefix):
                new = prefix + name
                os.rename(os.path.join(folder, name), os.path.join(folder, new))
                print(f"renamed {cat}/{name} -> {cat}/{new}")
                name = new
            if name.lower() in seen:
                print(f"warning: {cat}/{name} has the same name as {seen[name.lower()]}", file=sys.stderr)
            seen[name.lower()] = f"{cat}/{name}"
            entries.append((title_of(os.path.join(folder, name)), f"{quote(cat)}/{quote(name)}"))
        if entries:
            entries.sort(key=lambda e: e[0].lower())
            groups.append((cat, entries))

    lines = []
    for cat, entries in groups:
        if lines:
            lines.append("")
        lines.extend(link for _, link in entries)
    with open(os.path.join(library, "index.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    count = sum(len(e) for _, e in groups)
    print(f"index.txt: {count} recipes in {len(groups)} categories")
    return count


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else LIBRARY)
