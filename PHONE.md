# Recipes on your phone

Open https://your-pc.your-tailnet.ts.net/cook/ on the iPhone with the Tailscale app connected (anywhere), or http://192.168.1.20:8080/cook/ on home Wi-Fi. In Safari, Share > Add to Home Screen makes it an app icon.

The recipe server (`tools/serve-recipes.py`, port 8080) serves these pages from `tools/recipe-web.py`:

- **Recipes** (`/cook/`): every recipe by category, with search. Starred recipes are listed first under Favorites.
- **A recipe** (`/cook/r/<category>/<file>.md`):
  - The star at the top marks it as a favorite.
  - The ½×, 2×, 3×, and 5× buttons scale the ingredient amounts. Scaled amounts show in bold.
  - "Add to shopping list" adds the ingredients at the chosen scale. "Add to plan" puts the recipe on a day in the next two weeks.
  - "Edit notes" changes your own notes. "Edit recipe" opens the whole recipe for editing, with a Delete button.
- **Plan** (`/cook/plan`): seven days at a time. Add recipes to each day and pick a scale per meal. "Add these 7 days to shopping list" puts all of them on the list. A recipe planned twice gets its amounts added together.
- **List** (`/cook/list`): tap what you already have and it moves to "Already have". "Copy list" shares or copies what's left.

## Where things are saved

| What | Where | Seen by the Nook |
|---|---|---|
| Favorites | `library/favorites.txt`, one recipe file name per line | Yes |
| Your notes | A final `## My notes` section in the recipe's .md | Yes |
| Recipe edits | The recipe's .md. The old version goes to `trash/edits/` | Yes |
| Deleted recipes | Moved, with their photo and icon, to `trash/<category>/` | Removed on next sync |
| Shopping list and meal plan | `state/cook-state.json` | No |

The shopping list and plan are kept on the PC, so they're the same on every phone or computer that opens the pages. To bring back a deleted recipe, move its files from `trash/<category>/` back to `library/<category>/` and remove the date from the file names.

Editing, deleting, and notes only work through the Tailscale address. They're refused from other devices on the home Wi-Fi. Favorites, the plan, and the shopping list work from either address.

Changing `recipe-web.py` needs a server restart. In PowerShell: `Get-NetTCPConnection -LocalPort 8080 -State Listen | % { Stop-Process -Id $_.OwningProcess -Force }`. The "Nook recipe server" task starts it again within about 10 seconds. New or edited recipes show up without a restart.

## Tailscale setup (done 2026-10-20)

Tailscale is installed on the PC and the iPhone, on the same account. `tailscale serve --bg 8080` gives the PC the private HTTPS address above, which survives reboots. Check it with `tailscale serve status`, and turn it off with `tailscale serve --https=443 off`. Don't use `tailscale funnel`, which would put the server on the public internet.

The "Add to Nook" Shortcut works away from home when its URL is `https://your-pc.your-tailnet.ts.net/inbox`. The inbox still requires its `X-Inbox-Token`.
