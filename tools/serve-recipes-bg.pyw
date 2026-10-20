"""Runs serve-recipes.py with no console window and restarts it if it exits.

Started at sign-in by the "Nook recipe server" scheduled task
(see tools/install-autostart.ps1). Output goes to logs/serve-recipes.log.
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.abspath(os.path.join(HERE, "..", "logs"))
LOG = os.path.join(LOGS, "serve-recipes.log")
MAX_LOG = 5_000_000
# python.exe, not pythonw.exe, so the child has real stdout/stderr for the log
PYTHON = os.path.join(os.path.dirname(sys.executable), "python.exe")

os.makedirs(LOGS, exist_ok=True)
while True:
    if os.path.exists(LOG) and os.path.getsize(LOG) > MAX_LOG:
        os.replace(LOG, LOG + ".old")
    with open(LOG, "a", encoding="utf-8") as log:
        log.write(time.strftime("\n=== %Y-%m-%d %H:%M:%S starting server\n"))
        log.flush()
        code = subprocess.call(
            [PYTHON, "-u", os.path.join(HERE, "serve-recipes.py")],
            stdout=log, stderr=subprocess.STDOUT, cwd=HERE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        log.write(time.strftime(f"=== %Y-%m-%d %H:%M:%S server exited with {code}; restarting in 10 s\n"))
    time.sleep(10)
