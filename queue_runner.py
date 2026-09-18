#!/usr/bin/env python3
"""Take GitHub URLs off a relay channel, run them one at a time, report done.

Two threads. The listener long-polls the relay for messages on one channel,
pulls GitHub URLs out of whatever shape they arrive in, and queues them. The
worker runs the queue strictly one at a time using the account currently in
use, and once the queue drains it publishes a `done` message back to the
channel with what happened.

The queue is on disk, so a restart resumes rather than losing work.
"""

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

import accounts
import clone_repo
import pipeline as pipeline_module
import server_link

BASE_DIR = Path(__file__).resolve().parent
STORE = BASE_DIR / "queue.json"
DEFAULT_CHANNEL = "github"
POLL_WAIT = 25          # seconds the relay holds a long poll open
IDLE_SLEEP = 3.0
URL_PATTERN = re.compile(
    r"(?:https?://(?:www\.)?github\.com/|git@github\.com:)[A-Za-z0-9._-]+/[A-Za-z0-9._-]+",
    re.I,
)
URL_KEYS = ("url", "repo", "repository", "github", "link", "clone_url", "html_url")


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- the queue

def _blank():
    return {"version": 1,
            # queued work always runs for real and publishes public
            "settings": {"channel": DEFAULT_CHANNEL, "dry_run": False, "public": True,
                         "enabled": True},
            "items": [], "cursors": {}}


def load(path=STORE):
    path = Path(path)
    if not path.is_file():
        return _blank()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _blank()
    merged = _blank()
    merged.update({k: v for k, v in data.items() if k in merged})
    merged["settings"] = {**_blank()["settings"], **(data.get("settings") or {})}
    return merged


def save(data, path=STORE):
    path = Path(path)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def settings(path=STORE):
    return load(path)["settings"]


def set_settings(updates, path=STORE):
    data = load(path)
    for key in ("channel", "dry_run", "public", "enabled"):
        if key in updates and updates[key] is not None:
            value = updates[key]
            data["settings"][key] = value.strip() if isinstance(value, str) else bool(value)
    save(data, path)
    return data["settings"]


def extract_urls(body):
    """Pull GitHub URLs out of a message body, whatever shape it has."""
    found = []

    def take(value):
        if isinstance(value, str):
            found.extend(URL_PATTERN.findall(value))
        elif isinstance(value, list):
            for item in value:
                take(item)
        elif isinstance(value, dict):
            for key in URL_KEYS:
                if isinstance(value.get(key), str):
                    # an explicit field may hold the short owner/repo form
                    text = value[key].strip()
                    found.extend(URL_PATTERN.findall(text) or
                                 ([text] if re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", text)
                                  else []))
            for key, item in value.items():
                if key not in URL_KEYS:
                    take(item)

    take(body)

    clean, seen = [], set()
    for candidate in found:
        try:
            owner, repo = clone_repo.parse_github_url(candidate)
        except ValueError:
            continue
        url = f"https://github.com/{owner}/{repo}"
        if url.lower() not in seen:
            seen.add(url.lower())
            clean.append(url)
    return clean


def add(url, source=None, path=STORE):
    """Queue one URL. Returns the item, or None if it is already waiting."""
    try:
        owner, repo = clone_repo.parse_github_url(url)
    except ValueError as exc:
        raise ValueError(str(exc))
    url = f"https://github.com/{owner}/{repo}"

    data = load(path)
    for item in data["items"]:
        if item["url"].lower() == url.lower() and item["status"] in ("queued", "running"):
            return None
    item = {"url": url, "status": "queued", "added_at": _stamp(), "source": source,
            "started_at": None, "finished_at": None, "result": None, "error": None}
    data["items"].append(item)
    save(data, path)
    return item


def _update(url, path=STORE, **fields):
    data = load(path)
    for item in data["items"]:
        if item["url"] == url and item["status"] in ("queued", "running"):
            item.update(fields)
            break
    save(data, path)


def next_queued(path=STORE):
    for item in load(path)["items"]:
        if item["status"] == "queued":
            return item
    return None


def listing(path=STORE):
    data = load(path)
    return {"settings": data["settings"], "items": data["items"][-200:]}


def clear_finished(path=STORE):
    data = load(path)
    data["items"] = [i for i in data["items"] if i["status"] in ("queued", "running")]
    save(data, path)
    return listing(path)


def remove(url, path=STORE):
    data = load(path)
    before = len(data["items"])
    data["items"] = [i for i in data["items"]
                     if not (i["url"] == url and i["status"] == "queued")]
    save(data, path)
    return len(data["items"]) < before


# ---------------------------------------------------------------- the runner

class QueueRunner:
    """Listens on a relay channel and runs what arrives, one at a time."""

    def __init__(self, path=STORE, on_run=None):
        self.path = Path(path)
        self.on_run = on_run              # hands the live Pipeline to the UI
        self._stop = threading.Event()
        self._threads = []
        self._lock = threading.Lock()
        self.current = None               # url being run
        self.last_error = None
        self.last_done = None
        self.listening = False
        self._ran_since_done = 0
        self._failed_since_done = 0
        self._done_repos = []

    # ---- lifecycle

    def start(self):
        if self._threads:
            return False
        self._stop.clear()
        for target in (self._listen, self._work):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)
        return True

    def stop(self):
        self._stop.set()
        self._threads = []

    def state(self):
        data = listing(self.path)
        counts = {}
        for item in data["items"]:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        return {"listening": self.listening, "current": self.current,
                "last_error": self.last_error, "last_done": self.last_done,
                "counts": counts, **data}

    # ---- listening

    def _listen(self):
        while not self._stop.is_set():
            if not settings(self.path).get("enabled", True):
                self.listening = False
                self._stop.wait(IDLE_SLEEP)
                continue
            link = server_link.state()
            if link["status"] != "connected":
                self.listening = False
                self._stop.wait(IDLE_SLEEP)
                continue

            self.listening = True
            try:
                result = server_link.messages(wait=POLL_WAIT, limit=50)
            except server_link.ServerError as exc:
                self.last_error = str(exc)
                self._stop.wait(IDLE_SLEEP)
                continue

            if result["status"] != 200:
                self.last_error = f"relay answered HTTP {result['status']}"
                self._stop.wait(IDLE_SLEEP)
                continue

            self.last_error = None
            self._absorb(result["json"])

    def _absorb(self, payload):
        """Queue URLs from a batch of relay messages, then move the cursor."""
        messages = payload
        if isinstance(payload, dict):
            for key in ("messages", "items", "data", "results"):
                if isinstance(payload.get(key), list):
                    messages = payload[key]
                    break
        if not isinstance(messages, list):
            return

        channel = settings(self.path).get("channel") or DEFAULT_CHANNEL
        highest = {}
        for message in messages:
            if not isinstance(message, dict):
                continue
            where = message.get("channel") or channel
            if where != channel:
                continue
            body = message.get("body", message.get("text", message))
            for url in extract_urls(body):
                if add(url, source=f"{where}#{message.get('seq')}", path=self.path):
                    pass
            seq = message.get("seq")
            if isinstance(seq, int):
                highest[where] = max(highest.get(where, 0), seq)

        for where, seq in highest.items():
            try:
                server_link.ack(where, seq)
            except server_link.ServerError as exc:
                self.last_error = f"could not acknowledge {where}: {exc}"

    # ---- running

    def _work(self):
        while not self._stop.is_set():
            if not settings(self.path).get("enabled", True):
                self._stop.wait(IDLE_SLEEP)
                continue

            item = next_queued(self.path)
            if item is None:
                if self._ran_since_done or self._failed_since_done:
                    self._report_done()
                self._stop.wait(IDLE_SLEEP)
                continue

            self._run_one(item)

    def _run_one(self, item):
        url = item["url"]
        account = accounts.active()
        if not account:
            self.last_error = "worker is not selected - choose an account with Use first"
            self._stop.wait(IDLE_SLEEP)
            return

        config = settings(self.path)
        self.current = url
        _update(url, self.path, status="running", started_at=_stamp())

        run = pipeline_module.Pipeline(
            url=url, username=account["name"], email=account["email"],
            public=bool(config.get("public")), dry_run=bool(config.get("dry_run")),
        )
        if self.on_run:
            self.on_run(run)
        run.start()

        while not self._stop.is_set():
            snapshot = run.snapshot()
            if snapshot["state"] in ("done", "error", "stopped"):
                break
            self._stop.wait(0.4)

        snapshot = run.snapshot()
        self.current = None
        if snapshot["state"] == "done":
            _update(url, self.path, status="done", finished_at=_stamp(),
                    result=snapshot["result"])
            self._ran_since_done += 1
            if snapshot["result"]:
                self._done_repos.append({"source": url,
                                         "published": snapshot["result"].get("url"),
                                         "name": snapshot["result"].get("name")})
        else:
            _update(url, self.path, status="failed", finished_at=_stamp(),
                    error=snapshot["error"] or snapshot["state"])
            self._failed_since_done += 1

    def _report_done(self):
        """Tell the relay the queue is drained."""
        config = settings(self.path)
        channel = config.get("channel") or DEFAULT_CHANNEL
        link = server_link.state()
        payload = {
            "event": "done",
            "worker": link.get("worker_id"),
            "completed": self._ran_since_done,
            "failed": self._failed_since_done,
            "repos": self._done_repos[-50:],
            "dry_run": bool(config.get("dry_run")),
            "at": _stamp(),
        }
        try:
            result = server_link.publish(channel, payload)
        except server_link.ServerError as exc:
            self.last_error = f"could not report done: {exc}"
            return
        if result["status"] >= 300:
            self.last_error = f"done report refused: HTTP {result['status']}"
            return

        self.last_done = payload
        self._ran_since_done = 0
        self._failed_since_done = 0
        self._done_repos = []
