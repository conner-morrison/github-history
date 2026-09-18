#!/usr/bin/env python3
"""Connection to a Relay workspace: enrol, wait for approval, then talk.

The handshake is the relay's own worker protocol, not an invented one:

    POST {base}/{workspace}/enrol   {worker_id, token, label}  -> 202 accepted

202 means pending, not connected: an operator approves the worker in the relay
console. Until then every other call is refused, so approval is detected by
polling `GET {base}/{workspace}/messages`, which starts answering once approved.

After approval the worker authenticates with the token it chose:

    POST {base}/{workspace}/publish   {channel, body, id?}
    GET  {base}/{workspace}/messages  ?wait=&limit=
    POST {base}/{workspace}/ack       {channel, seq}

What actually travels over those is not decided here.
"""

import argparse
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse

BASE_DIR = Path(__file__).resolve().parent
STORE = BASE_DIR / "server.json"
TIMEOUT = 20
CLIENT = "github-history"
DEFAULT_WORKER = "github-history"


class ServerError(Exception):
    """The relay could not be reached, or refused."""


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _blank():
    return {"version": 2, "url": None, "base": None, "workspace": None,
            "worker_id": None, "token": None, "label": None, "status": "idle",
            "enrolled_at": None, "approved_at": None, "detail": None,
            "checked_at": None, "client_id": None}


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
    return merged


def save(data, path=STORE):
    path = Path(path)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)  # the worker token is a credential
    tmp.replace(path)
    os.chmod(path, 0o600)


def client_id(path=STORE):
    data = load(path)
    if not data.get("client_id"):
        data["client_id"] = uuid.uuid4().hex
        save(data, path)
    return data["client_id"]


def parse_target(url):
    """Split a workspace URL into (base, workspace).

    `https://host/upwork` -> (`https://host`, `upwork`)
    """
    url = (url or "").strip()
    if not url:
        raise ServerError("enter the workspace URL")
    if "://" not in url:
        url = "https://" + url
    parts = urlparse(url)
    if parts.scheme not in ("http", "https"):
        raise ServerError(f"unsupported scheme '{parts.scheme}' - use http or https")
    if not parts.hostname:
        raise ServerError("that URL has no host")

    segments = [s for s in parts.path.split("/") if s]
    if not segments:
        raise ServerError("include the workspace in the URL, for example https://host/upwork")
    workspace = segments[-1]
    base_path = "/".join(segments[:-1])
    base = urlunparse((parts.scheme, parts.netloc, ("/" + base_path) if base_path else "", "", "", ""))
    return base.rstrip("/"), workspace


def request(method, url, payload=None, token=None, timeout=TIMEOUT):
    """One HTTP round trip. Returns {status, json, text}."""
    body = None
    headers = {"Accept": "application/json", "User-Agent": CLIENT}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read(1 << 20).decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read(1 << 20).decode("utf-8", "replace") if exc.fp else ""
        status = exc.code
    except urllib.error.URLError as exc:
        raise ServerError(f"could not reach {url}: {exc.reason}")
    except (TimeoutError, OSError) as exc:
        raise ServerError(f"could not reach {url}: {exc}")

    try:
        parsed = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError:
        parsed = None
    return {"status": status, "json": parsed, "text": raw[:2000]}


def _detail(result, fallback=""):
    data = result.get("json")
    if isinstance(data, dict):
        for key in ("detail", "message", "error"):
            if data.get(key):
                return str(data[key])[:300]
    text = (result.get("text") or "").strip()
    if text.startswith("<"):
        return fallback or "the server answered with a web page, not the relay API"
    return text[:300] or fallback


def check_workspace(base, workspace):
    """Confirm the workspace exists before enrolling into it."""
    result = request("GET", f"{base}/api/workspaces/{quote(workspace)}")
    if result["status"] == 404:
        raise ServerError(f"no workspace '{workspace}' on {base}")
    if not 200 <= result["status"] < 300 or not isinstance(result["json"], dict):
        raise ServerError(
            f"{base} does not look like a relay "
            f"(HTTP {result['status']}: {_detail(result)})"
        )
    if result["json"].get("exists") is False:
        raise ServerError(f"no workspace '{workspace}' on {base}")
    return result["json"]


def enrol(url, worker_id=None, label=None, token=None, path=STORE):
    """Ask to join a workspace. Returns state with status 'pending' on 202.

    `token` is the secret the worker claims. Given one, it is used as is;
    otherwise a fresh random one is minted.
    """
    base, workspace = parse_target(url)
    info = check_workspace(base, workspace)

    data = load(path)
    worker_id = (worker_id or "").strip() or data.get("worker_id") or DEFAULT_WORKER
    token = (token or "").strip()
    if not token:
        token = data.get("token") if data.get("worker_id") == worker_id else None
        token = token or secrets.token_urlsafe(24)
    label = (label or "").strip() or f"{CLIENT} on this machine"

    result = request("POST", f"{base}/{quote(workspace)}/enrol",
                     {"worker_id": worker_id, "token": token, "label": label})

    if result["status"] in (401, 403):
        raise ServerError(f"the workspace refused the enrolment: {_detail(result)}")
    if result["status"] == 409:
        raise ServerError(f"'{worker_id}' is already taken on this workspace: {_detail(result)}")
    if result["status"] == 422:
        raise ServerError(f"the relay rejected the enrolment fields: {_detail(result)}")
    if not 200 <= result["status"] < 300:
        raise ServerError(f"enrolment failed (HTTP {result['status']}: {_detail(result)})")

    data.update({
        "url": f"{base}/{workspace}", "base": base, "workspace": workspace,
        "worker_id": worker_id, "token": token, "label": label,
        "status": "pending", "enrolled_at": _stamp(), "approved_at": None,
        "detail": f"waiting for {workspace} to approve '{worker_id}'",
        "checked_at": None,
        "client_id": data.get("client_id") or uuid.uuid4().hex,
        "workspace_name": info.get("name"),
    })
    save(data, path)
    return state(path)


def refresh(path=STORE):
    """Ask the relay whether the worker has been approved yet."""
    data = load(path)
    if not data.get("base") or not data.get("token"):
        return state(path)

    result = request("GET",
                     f"{data['base']}/{quote(data['workspace'])}/messages?wait=0&limit=1",
                     token=data["token"])
    data["checked_at"] = _stamp()

    body = result["json"] if isinstance(result["json"], dict) else {}
    reported = body.get("worker_id")
    mine = str(data.get("worker_id"))

    if 200 <= result["status"] < 300 and (reported is None or str(reported) == mine):
        # approved, and approved as the worker we enrolled
        data["status"] = "connected"
        data["approved_at"] = _stamp()
        data["detail"] = "approved"
    elif 200 <= result["status"] < 300:
        # the token works, but the relay knows it as a different worker - this
        # token was approved under another id, so *this* id is not approved
        data["status"] = "pending"
        data["detail"] = (f"this token is approved as '{reported}', not '{mine}'. "
                          f"Set the profile id to '{reported}', or use a token that "
                          f"has not been approved under another id.")
    elif result["status"] in (401, 403, 404):
        # still sitting in the workspace's pending list
        data["status"] = "pending"
        data["detail"] = _detail(result, "waiting for approval")
    else:
        data["status"] = "pending"
        data["detail"] = f"HTTP {result['status']}: {_detail(result)}"
    save(data, path)
    return state(path)


def state(path=STORE):
    client_id(path)
    data = load(path)
    return {
        "status": data.get("status") or "idle",
        "connected": data.get("status") == "connected",
        "pending": data.get("status") == "pending",
        "url": data.get("url"),
        "workspace": data.get("workspace"),
        "workspace_name": data.get("workspace_name"),
        "worker_id": data.get("worker_id"),
        "label": data.get("label"),
        "enrolled_at": data.get("enrolled_at"),
        "approved_at": data.get("approved_at"),
        "checked_at": data.get("checked_at"),
        "detail": data.get("detail"),
        "client_id": data.get("client_id"),
    }


def disconnect(path=STORE):
    """Forget the workspace and the worker token. The relay keeps its own record."""
    data = load(path)
    keep = data.get("client_id")
    data = _blank()
    data["client_id"] = keep
    save(data, path)
    return state(path)


def _ready(path=STORE):
    data = load(path)
    if data.get("status") != "connected":
        raise ServerError("not connected yet - the workspace has not approved this worker")
    return data


def publish(channel, body, message_id=None, path=STORE):
    """POST one message to a channel."""
    data = _ready(path)
    payload = {"channel": channel, "body": body}
    if message_id:
        payload["id"] = message_id
    return request("POST", f"{data['base']}/{quote(data['workspace'])}/publish",
                   payload, token=data["token"])


def messages(wait=0, limit=50, path=STORE):
    """GET everything above this worker's cursors."""
    data = _ready(path)
    url = (f"{data['base']}/{quote(data['workspace'])}/messages"
           f"?wait={int(wait)}&limit={int(limit)}")
    return request("GET", url, token=data["token"],
                   timeout=max(TIMEOUT, int(wait) + 5))


def ack(channel, seq, path=STORE):
    """Move this worker's cursor on a channel."""
    data = _ready(path)
    return request("POST", f"{data['base']}/{quote(data['workspace'])}/ack",
                   {"channel": channel, "seq": int(seq)}, token=data["token"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    connect_cmd = sub.add_parser("connect", help="enrol into a workspace and wait for approval")
    connect_cmd.add_argument("url", help="workspace URL, e.g. https://host/upwork")
    connect_cmd.add_argument("--worker-id")
    connect_cmd.add_argument("--label")
    connect_cmd.add_argument("--token", help="claim this token instead of a generated one")
    sub.add_parser("status", help="show the connection, without asking the relay")
    sub.add_parser("refresh", help="ask the relay whether approval has come through")
    sub.add_parser("disconnect", help="forget the workspace and token")
    pub = sub.add_parser("publish", help="send one message")
    pub.add_argument("channel")
    pub.add_argument("json", nargs="?", default="{}")
    recv = sub.add_parser("messages", help="read waiting messages")
    recv.add_argument("--wait", type=int, default=0)
    recv.add_argument("--limit", type=int, default=50)
    ack_cmd = sub.add_parser("ack", help="acknowledge up to a sequence number")
    ack_cmd.add_argument("channel")
    ack_cmd.add_argument("seq", type=int)
    args = parser.parse_args()

    try:
        if args.command == "connect":
            print(json.dumps(enrol(args.url, args.worker_id, args.label, args.token), indent=2))
        elif args.command == "status":
            print(json.dumps(state(), indent=2))
        elif args.command == "refresh":
            print(json.dumps(refresh(), indent=2))
        elif args.command == "disconnect":
            print(json.dumps(disconnect(), indent=2))
        elif args.command == "publish":
            print(json.dumps(publish(args.channel, json.loads(args.json)), indent=2))
        elif args.command == "messages":
            print(json.dumps(messages(args.wait, args.limit), indent=2))
        elif args.command == "ack":
            print(json.dumps(ack(args.channel, args.seq), indent=2))
    except ServerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"error: payload is not valid JSON: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
