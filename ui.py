#!/usr/bin/env python3
"""Local web UI for the clone -> rewrite -> name -> publish pipeline.

Serves on 127.0.0.1 only, guarded by a token minted at startup, because these
endpoints create repositories and push to GitHub. Standard library only.
"""

import argparse
import errno
import json
import os
import re
import secrets
import signal
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import accounts
import history
import pipeline as pipeline_module
import queue_runner
import push_history
import server_link
import verify_user

TOKEN = secrets.token_urlsafe(24)
PAGE = (Path(__file__).resolve().parent / "ui.html").read_text(encoding="utf-8")

DEFAULT_PORT = 8765
SWEEP_STOP = threading.Event()
STATE = {"run": None, "auth": {"state": "idle", "code": None, "url": None, "message": ""},
         "auth_proc": None, "auth_gen": 0, "runner": None}
LOCK = threading.Lock()


# ---------------------------------------------------------------- gh auth

ACCOUNT_CACHE = {"at": 0.0, "value": None}
ACCOUNT_TTL = 30.0


def invalidate_account_cache():
    ACCOUNT_CACHE["at"] = 0.0


def auth_status():
    """Cached view of the gh login.

    The page polls status twice a second; asking GitHub every time burns
    through the API rate limit in well under an hour.
    """
    now = time.monotonic()
    if ACCOUNT_CACHE["value"] is not None and now - ACCOUNT_CACHE["at"] < ACCOUNT_TTL:
        return ACCOUNT_CACHE["value"]
    value = _auth_status()
    ACCOUNT_CACHE.update({"at": now, "value": value})
    return value


def _auth_status():
    """Who gh is signed in as on this device, plus sensible field defaults."""
    try:
        account = push_history.gh_identity()
    except push_history.AuthError as exc:
        return {"logged_in": False, "detail": str(exc)}
    login = account["login"]
    scopes = push_history.token_scopes()
    return {"logged_in": True, "login": login, "name": account["name"] or login,
            "scopes": scopes, "can_delete": "delete_repo" in scopes}


LOGIN_CMD = ["gh", "auth", "login", "--hostname", "github.com", "--web", "--git-protocol", "https"]
GRANT_DELETE_CMD = ["gh", "auth", "refresh", "--hostname", "github.com", "-s", "delete_repo"]

# gh has worded this line differently across versions -- "First copy your one-time
# code: A1B2-C3D4" in older builds, "One-time code (A1B2-C3D4) copied to clipboard"
# in 2.101. Anchor on the phrase, then take the code whatever punctuation separates
# them, so a future rewording of the separator does not blank the page again.
CODE_RE = re.compile(r"one-time code\D{0,4}([A-Z0-9]{4}-[A-Z0-9]{4})", re.I)


def start_gh_flow(cmd, opening):
    """Run a gh browser flow and surface its one-time code to the page."""
    # a re-click restarts: stop any flow still waiting rather than no-op, so a
    # stuck "...." can always be retried. Each flow gets a generation number so
    # a superseded worker cannot clobber the new one's state.
    with LOCK:
        if STATE["auth"]["state"] == "waiting" and STATE["auth_proc"] is not None:
            proc = STATE["auth_proc"]
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        STATE["auth_gen"] += 1
        gen = STATE["auth_gen"]
        STATE["auth"] = {"state": "waiting", "code": None, "url": None, "message": opening}
        STATE["auth_proc"] = None

    def fail(message):
        """Surface a flow that died before gh could report anything itself."""
        with LOCK:
            if gen != STATE["auth_gen"]:
                return                        # superseded: do not touch the new flow
            if STATE["auth"]["state"] == "cancelled":
                return                        # the user backed out
            STATE["auth_proc"] = None
            STATE["auth"] = {"state": "error", "code": None, "url": None,
                             "message": message}

    def worker():
        env = dict(os.environ, BROWSER="true")  # the page shows the link instead
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, env=env, start_new_session=True,  # own group, so cancel can signal it
                bufsize=1,  # line-buffered
            )
        except OSError as exc:
            # gh missing from PATH raised FileNotFoundError straight out of this
            # daemon thread, which died silently and stranded the page on "...."
            # with nothing to explain it. Report it instead.
            fail(f"could not run {cmd[0]}: {exc.strerror or exc}. "
                 f"Is {cmd[0]} installed and on this server's PATH?")
            return
        with LOCK:
            if gen != STATE["auth_gen"]:      # already superseded before we spawned
                pass
            else:
                STATE["auth_proc"] = proc
        try:
            # readline yields each line as gh prints it; `for line in proc.stdout`
            # can withhold lines in a read-ahead buffer while gh blocks on the
            # browser step, which would hide the one-time code
            for line in iter(proc.stdout.readline, ""):
                line = line.strip()
                if not line:
                    continue
                code = CODE_RE.search(line)
                url = re.search(r"(https://\S+)", line)
                with LOCK:
                    if gen != STATE["auth_gen"]:
                        break                 # a newer flow owns the state now
                    if code:
                        STATE["auth"]["code"] = code.group(1)
                    if url:
                        STATE["auth"]["url"] = url.group(1)
                    STATE["auth"]["message"] = line
            proc.wait()
        except Exception as exc:              # never leave the page waiting forever
            fail(f"{cmd[0]} flow failed: {exc}")
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            return
        with LOCK:
            if gen != STATE["auth_gen"]:
                return                        # superseded: do not touch the new flow
            STATE["auth_proc"] = None
            if STATE["auth"]["state"] == "cancelled":
                return                        # the user backed out
            STATE["auth"]["state"] = "done" if proc.returncode == 0 else "error"
            if proc.returncode != 0:
                STATE["auth"]["message"] = f"gh exited with {proc.returncode}"

    threading.Thread(target=worker, daemon=True).start()


def cancel_gh_flow():
    """Stop a browser sign-in that is still waiting for its code."""
    with LOCK:
        proc = STATE["auth_proc"]
        waiting = STATE["auth"]["state"] == "waiting"
        if not waiting or proc is None or proc.poll() is not None:
            return False
        STATE["auth"] = {"state": "cancelled", "code": None, "url": None,
                         "message": "sign-in cancelled"}

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    return True


LAST_SERVER_CHECK = [0.0]
SERVER_CHECK_EVERY = 3.0


def active_worker():
    """The active profile, as the relay worker identity.

    The relay registers by the profile's id, not its username.
    """
    entry = accounts.active()
    if not entry:
        return None
    worker_id = entry.get("id")
    return {"worker_id": "" if worker_id is None else str(worker_id),
            "username": entry["username"], "name": entry.get("name"),
            "token_hint": accounts.mask(entry.get("token"))}


def server_state():
    """Current link state, re-asking the relay while approval is outstanding."""
    current = server_link.state()
    if not current["pending"]:
        return current
    if time.monotonic() - LAST_SERVER_CHECK[0] < SERVER_CHECK_EVERY:
        return current
    LAST_SERVER_CHECK[0] = time.monotonic()
    try:
        return server_link.refresh()
    except server_link.ServerError as exc:
        current["detail"] = str(exc)
        return current


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "github-history-ui"

    def log_message(self, fmt, *args):
        pass  # the UI is the log

    def _send(self, code, body, content_type="application/json"):
        payload = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, code, data):
        self._send(code, json.dumps(data))

    def _authorized(self):
        if self.headers.get("X-Token") == TOKEN:
            return True
        self._json(403, {"error": "bad token"})
        return False

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            return self._send(200, PAGE.replace("__TOKEN__", TOKEN), "text/html; charset=utf-8")
        if path == "/api/queue":
            if not self._authorized():
                return None
            runner = STATE["runner"]
            return self._json(200, runner.state() if runner else queue_runner.listing())

        if path == "/api/server":
            if not self._authorized():
                return None
            return self._json(200, {**server_state(), "worker": active_worker()})

        if path == "/api/accounts":
            if not self._authorized():
                return None
            return self._json(200, accounts.listing())

        if path == "/api/history":
            if not self._authorized():
                return None
            return self._json(200, history.listing())

        if path == "/api/status":
            if not self._authorized():
                return None
            run = STATE["run"]
            with LOCK:
                auth = dict(STATE["auth"])
            return self._json(200, {
                "run": run.snapshot() if run else {"state": "idle", "phase": "", "percent": 0,
                                                   "log": [], "result": None, "error": None},
                "auth": auth,
                "account": auth_status(),
            })
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return None
        path = self.path.split("?")[0]

        if path == "/api/auth/login":
            start_gh_flow(LOGIN_CMD, "starting sign-in ...")
            invalidate_account_cache()
            return self._json(200, {"ok": True})

        if path == "/api/auth/cancel":
            if cancel_gh_flow():
                return self._json(200, {"ok": True})
            return self._json(200, {"ok": False, "error": "no sign-in is waiting"})

        if path == "/api/auth/grant-delete":
            start_gh_flow(GRANT_DELETE_CMD, "requesting permission to delete repositories ...")
            invalidate_account_cache()
            return self._json(200, {"ok": True})

        if path == "/api/queue/add":
            try:
                item = queue_runner.add((self._body().get("url") or "").strip(), source="manual")
            except ValueError as exc:
                return self._json(200, {"ok": False, "error": str(exc)})
            return self._json(200, {"ok": True, "added": bool(item)})

        if path == "/api/queue/remove":
            removed = queue_runner.remove((self._body().get("url") or "").strip())
            return self._json(200, {"ok": removed})

        if path == "/api/queue/clear":
            queue_runner.clear_finished()
            return self._json(200, {"ok": True})

        if path == "/api/queue/settings":
            return self._json(200, {"ok": True,
                                    "settings": queue_runner.set_settings(self._body())})

        if path == "/api/server/connect":
            body = self._body()
            # the identity is taken from the active account here, never from the
            # page - the token must not travel through the browser
            entry = accounts.active()
            if not entry:
                return self._json(200, {"ok": False,
                                        "error": "worker is not selected - choose an account with Use first"})
            worker_id = entry.get("id")
            if worker_id in (None, ""):
                return self._json(200, {"ok": False,
                                        "error": "this profile has no id - the relay registers by id, "
                                                 "so edit the profile and set one"})
            try:
                link = server_link.enrol(body.get("url", ""), str(worker_id),
                                         entry.get("name") or entry["username"],
                                         entry["token"])
            except server_link.ServerError as exc:
                return self._json(200, {"ok": False, "error": str(exc)})
            LAST_SERVER_CHECK[0] = 0.0
            return self._json(200, {"ok": True, **link})

        if path == "/api/server/refresh":
            try:
                LAST_SERVER_CHECK[0] = 0.0
                return self._json(200, {"ok": True, **server_link.refresh()})
            except server_link.ServerError as exc:
                return self._json(200, {"ok": False, "error": str(exc)})

        if path == "/api/server/disconnect":
            return self._json(200, {"ok": True, **server_link.disconnect()})

        # the relay's own worker calls; payload shapes are the caller's business
        if path == "/api/server/publish":
            body = self._body()
            try:
                result = server_link.publish(body.get("channel", ""), body.get("body"),
                                             body.get("id"))
            except server_link.ServerError as exc:
                return self._json(200, {"ok": False, "error": str(exc)})
            return self._json(200, {"ok": True, **result})

        if path == "/api/server/messages":
            body = self._body()
            try:
                result = server_link.messages(body.get("wait") or 0, body.get("limit") or 50)
            except server_link.ServerError as exc:
                return self._json(200, {"ok": False, "error": str(exc)})
            return self._json(200, {"ok": True, **result})

        if path == "/api/accounts/save":
            body = self._body()
            try:
                entry = accounts.add(body.get("token", ""), body.get("name"),
                                     body.get("username"), body.get("email"),
                                     body.get("id") or None)
            except accounts.AccountError as exc:
                return self._json(200, {"ok": False, "error": str(exc)})
            return self._json(200, {"ok": True, "account": entry})

        if path == "/api/accounts/update":
            body = self._body()
            try:
                entry = accounts.update(body.get("original", ""), body.get("name"),
                                        body.get("username"), body.get("email"),
                                        body.get("id") or None, body.get("token"))
            except accounts.AccountError as exc:
                return self._json(200, {"ok": False, "error": str(exc)})
            return self._json(200, {"ok": True, "account": entry})

        if path == "/api/accounts/switch":
            try:
                entry = accounts.switch((self._body().get("username") or "").strip())
            except accounts.AccountError as exc:
                return self._json(200, {"ok": False, "error": str(exc)})
            invalidate_account_cache()
            return self._json(200, {"ok": True, "account": entry})

        if path == "/api/accounts/remove":
            try:
                accounts.remove((self._body().get("username") or "").strip())
            except accounts.AccountError as exc:
                return self._json(200, {"ok": False, "error": str(exc)})
            return self._json(200, {"ok": True})

        if path == "/api/history/delete":
            name = (self._body().get("name") or "").strip()
            try:
                return self._json(200, {"ok": True, **history.delete(name)})
            except (LookupError, PermissionError, RuntimeError, OSError) as exc:
                return self._json(200, {"ok": False, "error": str(exc)})

        if path == "/api/history/keep":
            body = self._body()
            try:
                entry = history.set_keep((body.get("name") or "").strip(), bool(body.get("keep")))
                return self._json(200, {"ok": True, "keep": entry["keep"]})
            except LookupError as exc:
                return self._json(200, {"ok": False, "error": str(exc)})

        if path == "/api/history/retention":
            try:
                days = history.set_retention_days(self._body().get("days"))
            except (TypeError, ValueError) as exc:
                return self._json(200, {"ok": False, "error": str(exc)})
            return self._json(200, {"ok": True, "retention_days": days})

        if path == "/api/verify":
            try:
                found = verify_user.resolve(self._body().get("username", ""))
            except verify_user.UserNotFound as exc:
                return self._json(200, {"ok": False, "error": str(exc)})
            return self._json(200, {"ok": True, **found})

        if path == "/api/start":
            body = self._body()
            missing = [k for k in ("url", "username") if not (body.get(k) or "").strip()]
            if missing:
                return self._json(400, {"error": f"missing: {', '.join(missing)}"})
            run = STATE["run"]
            if run and run.state in ("running", "paused"):
                return self._json(409, {"error": "a run is already in progress"})

            # a saved account carries its own name and email; anything else is
            # resolved from GitHub, which does not publish addresses
            saved = accounts.find(body["username"].strip())
            if saved:
                found = {"name": saved["name"], "email": saved["email"]}
            else:
                try:
                    found = verify_user.resolve(body["username"])
                except verify_user.UserNotFound as exc:
                    return self._json(400, {"error": str(exc)})

            run = pipeline_module.Pipeline(
                url=body["url"].strip(),
                username=found["name"],
                email=found["email"],
                public=bool(body.get("public")),
                name=(body.get("name") or "").strip() or None,
                dry_run=bool(body.get("dry_run")),
            )
            STATE["run"] = run
            run.start()
            return self._json(200, {"ok": True})

        run = STATE["run"]
        if path in ("/api/pause", "/api/resume", "/api/stop"):
            if not run:
                return self._json(409, {"error": "nothing is running"})
            action = {"/api/pause": run.pause, "/api/resume": run.resume, "/api/stop": run.stop}[path]
            return self._json(200, {"ok": action()})

        return self._json(404, {"error": "not found"})


def sweeper(interval=3600):
    """Delete expired repositories, now and once an hour while the UI runs."""
    def worker():
        while True:
            try:
                for result in history.sweep():
                    print(f"retention sweep: {result['action']}: {result['repo']}", flush=True)
            except Exception as exc:  # a sweep failure must never kill the server
                print(f"retention sweep failed: {exc}", flush=True)
            if not SWEEP_STOP.wait(interval):
                continue
            return

    threading.Thread(target=worker, daemon=True).start()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-p", "--port", type=int, default=None,
                        help="port (default: 8765, or the next free one)")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument("--no-sweep", action="store_true",
                        help="do not delete expired repositories while running")
    parser.add_argument("--no-queue", action="store_true",
                        help="do not listen to the relay channel or run queued URLs")
    args = parser.parse_args()

    # 8765 is a popular port; when it is taken and no port was asked for,
    # walk forward and fall back to whatever the OS hands out.
    wanted = args.port if args.port is not None else DEFAULT_PORT
    tries = [wanted] if args.port is not None else [wanted + n for n in range(10)] + [0]

    server = None
    for candidate in tries:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            break
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
    if server is None:
        print(f"port {wanted} is already in use; try `python3 ui.py --port 9000`")
        return 1
    if server.server_port != wanted:
        print(f"port {wanted} was busy, using {server.server_port} instead", flush=True)

    if not args.no_sweep:
        sweeper()

    if not args.no_queue:
        runner = queue_runner.QueueRunner(on_run=lambda run: STATE.__setitem__("run", run))
        STATE["runner"] = runner
        runner.start()
        print(f"queue: listening on the '{queue_runner.settings()['channel']}' channel "
              f"once a relay is connected", flush=True)

    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"UI at {url}", flush=True)
    print("(local only; the page carries a one-time token)", flush=True)
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
