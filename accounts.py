#!/usr/bin/env python3
"""Saved GitHub accounts, so switching is one click.

Each account holds a display name, the GitHub numeric id, a token, a username
and an email. The token is a real credential: the store is written 0600, is
gitignored, and is never handed to the browser - only a masked hint is.
"""

import argparse
import getpass
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STORE = BASE_DIR / "accounts.json"


class AccountError(Exception):
    """The account could not be saved, switched to, or found."""


class RateLimited(AccountError):
    """GitHub would not answer right now. It says nothing about the token."""


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


def check_token(token):
    """Ask GitHub who a token belongs to. Returns {login, id, name, scopes}."""
    if not (token or "").strip():
        raise AccountError("no token given")

    env = dict(os.environ, GH_TOKEN=token.strip())
    env.pop("GITHUB_TOKEN", None)
    probe = subprocess.run(["gh", "api", "user"], capture_output=True, text=True, env=env)
    if probe.returncode != 0:
        detail = (probe.stderr or "").strip().splitlines()
        first = detail[0] if detail else "request failed"
        if "401" in first or "Bad credentials" in first:
            raise AccountError("GitHub rejected that token")
        if "rate limit" in first.lower():
            raise RateLimited("GitHub is rate limiting this token - try again shortly")
        raise AccountError(f"could not check the token: {first}")

    user = json.loads(probe.stdout)
    scopes = subprocess.run(["gh", "auth", "status", "--hostname", "github.com"],
                            capture_output=True, text=True, env=env)
    text = (scopes.stderr or "") + (scopes.stdout or "")
    found = []
    for line in text.splitlines():
        if "Token scopes:" in line:
            found = [part.strip().strip("'") for part in line.split(":", 1)[1].split(",")]
    return {"login": user.get("login"), "id": user.get("id"),
            "name": user.get("name"), "scopes": [s for s in found if s]}


def add(token, name=None, username=None, email=None, account_id=None, path=STORE):
    """Save an account after confirming the token really belongs to it."""
    who = check_token(token)

    username = (username or "").strip() or who["login"]
    if username.lower() != (who["login"] or "").lower():
        raise AccountError(f"that token belongs to '{who['login']}', not '{username}'")

    account_id = account_id or who["id"]
    name = (name or "").strip() or who["name"] or who["login"]
    email = (email or "").strip() or f"{account_id}+{who['login']}@users.noreply.github.com"

    entry = {
        "name": name,
        "id": account_id,
        "username": who["login"],
        "email": email,
        "token": token.strip(),
        "scopes": who["scopes"],
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
    who = check_token(token)

    username = (username or "").strip() or existing["username"]
    if username.lower() != (who["login"] or "").lower():
        raise AccountError(f"that token belongs to '{who['login']}', not '{username}'")

    updated = dict(existing)
    updated.update({
        "name": (name or "").strip() or existing["name"],
        "username": who["login"],
        "email": (email or "").strip() or existing["email"],
        "id": account_id or existing["id"] or who["id"],
        "token": token,
        "scopes": who["scopes"],
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
    """Make a saved account the one gh uses, after re-checking its token."""
    entry = find(username, path)
    if entry is None:
        raise AccountError(f"no saved account named '{username}'")

    # re-check before replacing a working login, but a rate limit is not a
    # verdict on the token: it was verified when it was saved
    try:
        who = check_token(entry["token"])
        if (who["login"] or "").lower() != entry["username"].lower():
            raise AccountError(f"the saved token now belongs to '{who['login']}'")
    except RateLimited:
        who = {"login": entry["username"], "scopes": entry.get("scopes", [])}

    handoff = subprocess.run(["gh", "auth", "login", "--hostname", "github.com",
                              "--with-token"], input=entry["token"],
                             capture_output=True, text=True)
    if handoff.returncode != 0:
        detail = (handoff.stderr or "").strip().splitlines()
        first = detail[0] if detail else "unknown error"
        if "rate limit" in first.lower():
            raise RateLimited("GitHub is rate limiting right now - try switching again shortly")
        raise AccountError(f"gh refused the token: {first}")

    data = load(path)
    for stored in data["accounts"]:
        if stored["username"].lower() == entry["username"].lower():
            stored["scopes"] = who["scopes"]
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
    add_cmd.add_argument("--id", type=int)
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
