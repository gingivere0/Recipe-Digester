# Recipe library

The Nook syncs from `http://<this PC's IP>:8080/index.txt`, served by `tools\serve-recipes.cmd`.

## Adding a recipe

1. Save it as Markdown in the right category folder (breakfast, mains, sides, soups, baking, desserts, drinks, or misc). A short name like `pancakes.md` is fine.
2. That's it. The server renames it to `breakfast--pancakes.md` and adds it to `index.txt` the next time the Nook syncs. To do it by hand, run `py -3 tools\build-index.py`.

## Format the Nook understands

```
# Recipe Title
One plain line: serves, time, or a short description (shown under the title in the list).

## Ingredients
- 2 cups flour
- 1 tsp salt

## Steps
1. Heat the oven to 375°F (190°C).
2. Mix everything together.

## Notes
Plain text.
```

- `# ` is the title (one per file). `## ` starts a section.
- Lines starting with `- ` or `1. ` can be checked off on the Nook.
- Use plain text only. No images, links, tables, bold, or italics.
- Keep quantities with both units where the source gives them.
