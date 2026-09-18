#!/usr/bin/env python3
"""Resolve a GitHub username to the identity its commits should carry.

GitHub does not expose account emails - the profile field is almost always
null. The dependable address is the account's own
`<id>+<login>@users.noreply.github.com`, which GitHub links back to that
account, so commits written with it are credited to them.
"""

import argparse
import json
import re
import subprocess
import sys

LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
NOREPLY = "users.noreply.github.com"


class UserNotFound(Exception):
    """No GitHub account by that name."""


def gh_json(path):
    """GET a GitHub API path. Returns (ok, parsed-or-error-string)."""
    try:
        probe = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if probe.returncode != 0:
        lines = (probe.stderr or "").strip().splitlines()
        return False, (lines[0] if lines else "request failed")
    try:
        return True, json.loads(probe.stdout)
    except json.JSONDecodeError:
        return False, "unreadable response"


def resolve(username):
    """Return {login, id, name, email} for a real account, else raise UserNotFound."""
    username = (username or "").strip()
    if not username:
        raise UserNotFound("enter a GitHub username")
    if not LOGIN.match(username):
        raise UserNotFound(
            f"'{username}' is not a GitHub username "
            f"(letters, digits and single hyphens, up to 39 characters)"
        )

    ok, data = gh_json(f"users/{username}")
    if not ok:
        if "Not Found" in str(data):
            raise UserNotFound(f"no GitHub user named '{username}'")
        raise UserNotFound(f"could not check '{username}': {data}")

    if data.get("type") == "Organization":
        raise UserNotFound(f"'{username}' is an organization, not a user account")

    login, uid = data["login"], data["id"]
    return {
        "login": login,
        "id": uid,
        "name": data.get("name") or login,
        # the profile email when the account publishes one, otherwise the
        # address GitHub itself issues for that account
        "email": data.get("email") or f"{uid}+{login}@{NOREPLY}",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username", help="GitHub username to look up")
    parser.add_argument("--json", action="store_true", help="print the raw result")
    args = parser.parse_args()

    try:
        found = resolve(args.username)
    except UserNotFound as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(found, indent=2))
    else:
        print(f"github.com/{found['login']} exists (id {found['id']})")
        print(f"commits will carry {found['name']} <{found['email']}>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
