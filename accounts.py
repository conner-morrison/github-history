#!/usr/bin/env python3
"""Saved profiles, so switching worker identity is one click.

Each profile holds a display name, an id, a token, a GitHub username and email.
The token is the relay worker password (worker id + token is what the relay
approves), not a GitHub credential - it is never used to authenticate to GitHub.
It is still a secret, so the store is written 0600, is gitignored, and is never
handed to the browser: only a masked hint is.
"""

import argparse
import getpass
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STORE = BASE_DIR / "accounts.json"


class AccountError(Exception):
    """The account could not be saved, switched to, or found."""


def _empty():
    return {"version": 1, "active": None, "accounts": []}


def load(path=STORE):
    path = Path(path)
    if not path.is_file():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty()
    data.setdefault("accounts", [])
    data.setdefault("active", None)
    return data


def save(data, path=STORE):
    path = Path(path)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)  # a token lives in here
    tmp.replace(path)
    os.chmod(path, 0o600)


def mask(token):
    """Show only enough of a token to tell two apart."""
    if not token:
        return ""
    return f"...{token[-4:]}" if len(token) > 8 else "..."


def public(entry):
    """An account record with the token replaced by a hint."""
    shown = {key: value for key, value in entry.items() if key != "token"}
    shown["token_hint"] = mask(entry.get("token"))
    return shown


def add(token, name=None, username=None, email=None, account_id=None, path=STORE):
    """Save an account. The token is stored as given, not checked against GitHub.

    Because nothing is derived from the token, the username is required.
    """
    username = (username or "").strip()
    if not username:
        raise AccountError("a GitHub username is required")
    if not (token or "").strip():
        raise AccountError("a token is required")

    noreply = (f"{account_id}+{username}@users.noreply.github.com" if account_id
               else f"{username}@users.noreply.github.com")
    entry = {
        "name": (name or "").strip() or username,
        "id": account_id,
        "username": username,
        "email": (email or "").strip() or noreply,
        "token": token.strip(),
        "scopes": [],  # unknown without asking GitHub
        "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    data = load(path)
    data["accounts"] = [a for a in data["accounts"] if a["username"].lower() != entry["username"].lower()]
    data["accounts"].append(entry)
    data["accounts"].sort(key=lambda a: a["username"].lower())
    save(data, path)
    return public(entry)


def update(original, name=None, username=None, email=None, account_id=None,
           token=None, path=STORE):
    """Edit a saved account. A blank token keeps the stored one."""
    existing = find(original, path)
    if existing is None:
        raise AccountError(f"no saved account named '{original}'")

    token = (token or "").strip() or existing["token"]
    username = (username or "").strip() or existing["username"]

    updated = dict(existing)
    updated.update({
        "name": (name or "").strip() or existing["name"],
        "username": username,
        "email": (email or "").strip() or existing["email"],
        "id": account_id or existing["id"],
        "token": token,
        "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })

    data = load(path)
    data["accounts"] = [a for a in data["accounts"]
                        if a["username"].lower() not in (original.lower(), updated["username"].lower())]
    data["accounts"].append(updated)
    data["accounts"].sort(key=lambda a: a["username"].lower())
    if (data.get("active") or "").lower() == original.lower():
        data["active"] = updated["username"]
    save(data, path)
    return public(updated)


def remove(username, path=STORE):
    data = load(path)
    before = len(data["accounts"])
    data["accounts"] = [a for a in data["accounts"] if a["username"].lower() != username.lower()]
    if len(data["accounts"]) == before:
        raise AccountError(f"no saved account named '{username}'")
    if (data.get("active") or "").lower() == username.lower():
        data["active"] = None
    save(data, path)
    return True


def find(username, path=STORE):
    for entry in load(path)["accounts"]:
        if entry["username"].lower() == (username or "").lower():
            return entry
    return None


def listing(path=STORE):
    data = load(path)
    return {"active": data.get("active"),
            "accounts": [public(entry) for entry in data["accounts"]]}


def switch(username, path=STORE):
    """Make a saved profile the active one.

    This only selects the profile. Its token is the relay worker password, not a
    GitHub credential, so nothing is handed to `gh` here: the account that
    actually pushes is whoever is signed in through the browser. The active
    profile supplies the relay worker id and token, and the name/email that
    commits are rewritten to.
    """
    entry = find(username, path)
    if entry is None:
        raise AccountError(f"no saved profile named '{username}'")

    data = load(path)
    data["active"] = entry["username"]
    save(data, path)
    return public(entry)


def active(path=STORE):
    return find(load(path).get("active") or "", path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show saved accounts")
    add_cmd = sub.add_parser("add", help="save an account (the token is prompted for)")
    add_cmd.add_argument("--name")
    add_cmd.add_argument("--username")
    add_cmd.add_argument("--email")
    add_cmd.add_argument("--id", help="relay worker id (any string)")
    switch_cmd = sub.add_parser("switch", help="use a saved account")
    switch_cmd.add_argument("username")
    remove_cmd = sub.add_parser("remove", help="forget a saved account")
    remove_cmd.add_argument("username")
    args = parser.parse_args()

    try:
        if args.command == "list":
            state = listing()
            if not state["accounts"]:
                print("(no saved accounts)")
            for entry in state["accounts"]:
                mark = "*" if entry["username"] == state["active"] else " "
                print(f" {mark} {entry['username']:<24} {entry['name']:<24} "
                      f"{entry['email']:<40} token {entry['token_hint']}")
        elif args.command == "add":
            token = getpass.getpass("GitHub token (hidden): ")
            entry = add(token, args.name, args.username, args.email, args.id)
            print(f"saved {entry['username']} ({entry['name']}) <{entry['email']}>")
        elif args.command == "switch":
            entry = switch(args.username)
            print(f"now using {entry['username']}")
        elif args.command == "remove":
            remove(args.username)
            print(f"removed {args.username}")
    except AccountError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
