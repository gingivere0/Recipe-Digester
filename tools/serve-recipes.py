"""Rebuilds library/index.txt, then serves library/ to the Nook over the LAN.

Sync URL on the Nook: http://<this PC's IP>:8080/index.txt
Phone inbox (see recipe-inbox.py): POST /inbox, status page at /inbox
Phone recipe browser, meal plan, and shopping list (see recipe-web.py): /cook/
"""
import functools
import http.server
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIBRARY = os.path.abspath(os.path.join(HERE, "..", "library"))
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
MAX_UPLOAD = 25_000_000


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, file))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_index = load("build_index", "build-index.py")
inbox = load("recipe_inbox", "recipe-inbox.py")
web = load("recipe_web", "recipe-web.py")


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".md": "text/plain; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
    }

    def reply(self, code, text, content_type="text/plain; charset=utf-8"):
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/inbox", "/inbox/"):
            return self.reply(200, inbox.page(PORT), "text/html; charset=utf-8")
        page = web.handle(path, LIBRARY)
        if page:
            return self.reply(*page)
        # rebuild on each list request so new recipes show up without a restart
        if path == "/index.txt":
            build_index.build(LIBRARY)
        super().do_GET()

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        if path.startswith("/cook/api/"):
            if not 0 < length <= 2_000_000:
                return self.reply(400, "Nothing was sent, or it is too big.")
            # `tailscale serve` proxies from this PC, so only Tailscale (or the PC itself) looks local
            trusted = self.client_address[0] in ("127.0.0.1", "::1")
            return self.reply(*web.handle_post(path, self.rfile.read(length), trusted, LIBRARY))
        if path not in ("/inbox", "/inbox/"):
            return self.reply(404, "not found")
        if self.headers.get("X-Inbox-Token", "").strip() != inbox.token():
            return self.reply(403, "Wrong or missing X-Inbox-Token (see tools\\inbox-token.txt).")
        if not 0 < length <= MAX_UPLOAD:
            return self.reply(400, "Nothing was sent, or it is over 25 MB.")
        body = self.rfile.read(length)
        try:
            job = inbox.submit(body, self.headers.get("Content-Type", ""), self.headers.get("X-Filename", ""))
        except ValueError as e:
            return self.reply(400, f"Could not use that: {e}")
        what = "link" if job["kind"] == "url" else job["kind"]
        self.reply(200, f"Got the {what}. The recipe will be in the library in a minute or two.")


if __name__ == "__main__":
    build_index.build(LIBRARY)
    inbox.start(on_recipe=lambda: build_index.build(LIBRARY))
    handler = functools.partial(Handler, directory=LIBRARY)
    with http.server.ThreadingHTTPServer(("0.0.0.0", PORT), handler) as httpd:
        print(f"Serving {LIBRARY} on port {PORT}. Sync URL: http://<this PC's IP>:{PORT}/index.txt")
        print(f"Phone inbox: http://<this PC's IP>:{PORT}/inbox  token: {inbox.token()}")
        print(f"Recipe browser: http://<this PC's IP>:{PORT}/cook/")
        httpd.serve_forever()
