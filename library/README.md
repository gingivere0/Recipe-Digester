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
- Use plain text only. No links, tables, bold, or italics.
- One optional photo line, `![Photo of Recipe Title](<same name as the recipe>.jpg)`, goes under the subtitle, with the .jpg next to the .md (544 px wide works best). The phone inbox adds it itself when the source has a photo of the finished dish.
- The Nook's list shows `<same name>.thumb.jpg` (80x80) next to each recipe: cut from the photo, or an icon Claude draws when there's no photo. `py -3 tools\recipe-inbox.py --thumbs` makes any that are missing.
- Keep quantities with both units where the source gives them.
- The phone inbox adds a `## Nutrition` section before Notes: one line with Claude's rough calories and protein per serving. `py -3 tools\recipe-inbox.py --nutrition` adds it to recipes that have none.
