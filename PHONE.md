# Recipes on your phone

The recipe server (`tools/serve-recipes.py`, port 8080) also serves phone pages from `tools/recipe-web.py`:

- `/cook/` (and `/`): all recipes by category, with search and photos
- `/cook/r/<category>/<file>.md`: one recipe, with an "Add to shopping list" button
- `/cook/list`: the shopping list. Tap what you already have and it moves to "Already have". "Copy list" shares or copies what's left.

The shopping list is kept in the phone's browser, so nothing is written on the PC. Adding the same recipe again keeps what you already marked. Changing `recipe-web.py` needs a server restart, but new recipes show up right away.

## Reaching it away from home with Tailscale

This keeps the server private to your own devices (your tailnet). Nothing is opened to the internet, and no router or firewall changes are needed.

1. Install Tailscale on the PC (https://tailscale.com/download/windows) and on the iPhone (App Store), and sign in to the same account on both.
2. In the Tailscale admin console (https://login.tailscale.com/admin/dns), make sure MagicDNS is on and turn on HTTPS Certificates.
3. On the PC, in a normal PowerShell window:

   ```
   tailscale serve --bg 8080
   ```

   It prints an address like `https://your-pc.<your-tailnet>.ts.net/`. This setting survives reboots. Check it with `tailscale serve status`, and undo it with `tailscale serve --https=443 off`.
4. On the iPhone, with the Tailscale app connected, open `https://your-pc.<your-tailnet>.ts.net/cook/`. In Safari, Share > Add to Home Screen makes it an app icon.

Use `tailscale serve`, not `tailscale funnel`: Funnel would put the server on the public internet.

The inbox is reachable through the same address, still guarded by `X-Inbox-Token`. To use the "Add to Nook" Shortcut away from home, change its URL to `https://your-pc.<your-tailnet>.ts.net/inbox`.

The PC has to be on and signed in, since the server starts at sign-in.
