#!/usr/bin/env python3
"""Publish rewritten history to the GitHub account this device is signed in to.

The only gate is that `gh auth status` succeeds. The name and email are taken
as given and are not checked against the account: whatever you type is what the
commits carry.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import name_repo
import rewrite_history

NOREPLY = "users.noreply.github.com"


class AuthError(Exception):
    """This device is not connected to GitHub."""


def gh_signed_in():
    """Return (ok, detail) from `gh auth status` for github.com."""
    try:
        probe = subprocess.run(["gh", "auth", "status", "--hostname", "github.com"],
                               capture_output=True, text=True)
    except FileNotFoundError:
        return False, "gh is not installed"
    lines = (probe.stderr or probe.stdout or "").strip().splitlines()
    return probe.returncode == 0, (lines[0].strip() if lines else "")


def token_scopes():
    """The OAuth scopes on the current gh token, as a list."""
    probe = subprocess.run(["gh", "auth", "status", "--hostname", "github.com"],
                           capture_output=True, text=True)
    text = (probe.stderr or "") + (probe.stdout or "")
    for line in text.splitlines():
        if "Token scopes:" in line:
            return re.findall(r"'([^']+)'", line)
    return []


def gh_identity():
    """The account this device is signed in as. Raises AuthError if it is not."""
    ok, detail = gh_signed_in()
    if not ok:
        raise AuthError(detail or "not signed in to github.com; run `gh auth login`")

    probe = subprocess.run(["gh", "api", "user"], capture_output=True, text=True)
    if probe.returncode != 0:
        lines = (probe.stderr or "").strip().splitlines()
        raise AuthError(f"signed in, but `gh api user` failed: {lines[0] if lines else 'no output'}")

    user = json.loads(probe.stdout)
    return {
        "login": user.get("login"),
        "name": user.get("name"),
        "id": user.get("id"),
        "public_email": user.get("email"),
    }


def check(repo, env_path=None, identity=None):
    """Confirm GitHub is connected, and resolve the identity to write.

    Returns (name, email, account). The name and email are NOT compared against
    the account - they are whatever was given, and the commits will carry them.
    """
    if identity is not None:
        name, email = identity
    else:
        (name, email), _ = rewrite_history.identity_from_env(rewrite_history.load_env(env_path))
    return name, email, gh_identity()


def remote_url(repo):
    result = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("this repository has no 'origin' remote to push to")
    return result.stdout.strip()


def materialize_remote_branches(repo):
    """Give every origin/* branch a local branch.

    `git push --all` only pushes local branches, and a fresh clone has just
    one, so without this the other branches would keep their old history.
    """
    def refs(pattern):
        out = subprocess.run(["git", "-C", str(repo), "for-each-ref", "--format=%(refname)", pattern],
                             check=True, capture_output=True, text=True).stdout
        return [line for line in out.splitlines() if line]

    prefix = "refs/remotes/origin/"
    existing = {ref[len("refs/heads/"):] for ref in refs("refs/heads")}
    created = []
    for ref in refs("refs/remotes/origin"):
        name = ref[len(prefix):]
        # origin/HEAD is a symbolic pointer, not a branch
        if not name or name == "HEAD" or name in existing:
            continue
        subprocess.run(["git", "-C", str(repo), "branch", name, ref], check=True,
                       stdout=subprocess.DEVNULL)
        created.append(name)
    return created


def force_push(repo, env_path, assume_yes=False):
    """Check the identity, confirm, then force-push all branches and tags."""
    repo = Path(repo).resolve()
    name, email, account = check(repo, env_path)
    url = remote_url(repo)

    print(f"identity verified: {name} <{email}> is github.com/{account['login']}")

    created = materialize_remote_branches(repo)
    if created:
        print(f"local branches created so --all covers them: {', '.join(created)}")

    if not assume_yes:
        print(f"\nAbout to FORCE-PUSH rewritten history to {url}")
        print("This overwrites all branches and tags on the remote and cannot be undone.")
        if input("Type the repository's name to continue: ").strip() != repo.name:
            print("aborted")
            return False

    subprocess.run(["git", "-C", str(repo), "push", "--force", "--all", "origin"], check=True)
    subprocess.run(["git", "-C", str(repo), "push", "--force", "--tags", "origin"], check=True)
    print(f"force-pushed {repo.name} to {url}")
    return True


def name_taken(login, name):
    """True if the account already has a repo by that name."""
    probe = subprocess.run(["gh", "repo", "view", f"{login}/{name}", "--json", "name"],
                           capture_output=True, text=True)
    return probe.returncode == 0


def pick_name(login, names):
    """First candidate the account does not already use."""
    for name in names:
        if not name_taken(login, name):
            return name
    raise RuntimeError(f"every candidate name is already taken on {login}: {', '.join(names)}")


def publish_as_new(repo, names, env_path=None, identity=None, private=True,
                   assume_yes=False, dry_run=False, control=None, on_progress=None):
    """Create a fresh repo on the verified account and push the history there.

    Nothing is force-pushed over an existing project: the new repo starts empty,
    so the repo that was cloned from is left exactly as it was.
    """
    repo = Path(repo).resolve()
    run = control.run if control is not None else None
    name, email, account = check(repo, env_path, identity)
    login = account["login"]
    print(f"signed in as github.com/{login}; commits will carry {name} <{email}>")

    if isinstance(names, str):
        names = [names]
    chosen = pick_name(login, names)
    url = f"https://github.com/{login}/{chosen}.git"
    visibility = "--private" if private else "--public"

    created = materialize_remote_branches(repo)
    if created:
        print(f"local branches created so --all covers them: {', '.join(created)}")

    if dry_run:
        print(f"[dry-run] gh repo create {login}/{chosen} {visibility}")
        print(f"[dry-run] git remote set-url origin {url}")
        print(f"[dry-run] git push --force --all origin")
        print(f"[dry-run] git push --force --tags origin")
        return chosen

    if not assume_yes:
        print(f"\nAbout to create {visibility[2:]} repo {login}/{chosen} and push {repo.name} into it.")
        if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted")
            return None

    create = ["gh", "repo", "create", f"{login}/{chosen}", visibility]
    set_url = ["git", "-C", str(repo), "remote", "set-url", "origin", url]
    push_all = ["git", "-C", str(repo), "push", "--force", "--all", "--progress", "origin"]
    push_tags = ["git", "-C", str(repo), "push", "--force", "--tags", "--progress", "origin"]

    if control is not None:
        control.run(create)
        control.run(set_url)
        control.stream(push_all, on_progress)
        control.stream(push_tags, on_progress)
    else:
        for cmd in (create, set_url, push_all, push_tags):
            subprocess.run(cmd, check=True)
    print(f"published: https://github.com/{login}/{chosen}")
    return chosen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", help="repository whose rewritten history to push")
    parser.add_argument("-e", "--env", default=str(Path(__file__).resolve().parent / ".env"),
                        help="path to the .env file (default: .env next to this script)")
    parser.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument("--check-only", action="store_true",
                        help="check the GitHub connection and exit without pushing")
    parser.add_argument("--new", nargs="?", const="", metavar="NAME",
                        help="publish to a NEW repo on your account instead of force-pushing "
                             "origin; with no NAME, one is generated from the repo's contents")
    parser.add_argument("--public", action="store_true", help="make the new repo public (default: private)")
    parser.add_argument("--dry-run", action="store_true", help="print what --new would do, change nothing")
    args = parser.parse_args()

    try:
        if args.check_only:
            name, email, account = check(args.repo, args.env)
            print(f"signed in as github.com/{account['login']}; "
                  f"commits would carry {name} <{email}>")
            return 0
        if args.new is not None:
            names = [args.new] if args.new else name_repo.suggest(args.repo)
            chosen = publish_as_new(args.repo, names, env_path=args.env, private=not args.public,
                                    assume_yes=args.yes, dry_run=args.dry_run)
            return 0 if chosen else 1
        return 0 if force_push(args.repo, args.env, args.yes) else 1
    except (AuthError, RuntimeError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"error: git push failed with {exc.returncode}", file=sys.stderr)
        return exc.returncode


if __name__ == "__main__":
    sys.exit(main())
