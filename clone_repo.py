#!/usr/bin/env python3
"""Clone a GitHub repo, rewrite its history to the .env identity, republish it under
a name derived from what the repo contains."""

import argparse
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import name_repo
import push_history
import rewrite_history

BASE_DIR = Path(__file__).resolve().parent
DEST_DIR = BASE_DIR / "repos"
ENV_FILE = BASE_DIR / ".env"


def parse_github_url(url):
    """Return (owner, repo) from an https, ssh or owner/repo style GitHub URL."""
    url = url.strip().rstrip("/")

    # git@github.com:owner/repo.git
    match = re.match(r"^git@([^:]+):(?P<path>.+)$", url)
    if match:
        path = match.group("path")
    elif "://" in url:
        parsed = urlparse(url)
        if parsed.hostname != "github.com":
            raise ValueError(f"not a github.com URL: {url}")
        path = parsed.path.lstrip("/")
    else:
        path = url  # bare "owner/repo"

    if path.endswith(".git"):
        path = path[: -len(".git")]

    # keeps the first two segments, so a deep link like
    # github.com/owner/repo/tree/main/src still works
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"cannot read owner/repo from: {url}")

    owner, repo = parts[0], parts[1]
    if not re.fullmatch(r"[A-Za-z0-9._-]+", owner) or not re.fullmatch(r"[A-Za-z0-9._-]+", repo):
        raise ValueError(f"invalid owner or repo name in: {url}")
    return owner, repo


def clone(url, dest_dir=DEST_DIR, depth=None, force=False):
    owner, repo = parse_github_url(url)
    clone_url = f"https://github.com/{owner}/{repo}.git"
    target = dest_dir / owner / repo

    if target.exists():
        if not force:
            raise FileExistsError(f"already exists: {target}")
        print(f"removing existing {target}")
        subprocess.run(["rm", "-rf", str(target)], check=True)

    target.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["git", "clone"]
    if depth:
        cmd += ["--depth", str(depth)]
    cmd += [clone_url, str(target)]

    print(f"cloning {clone_url} -> {target}")
    subprocess.run(cmd, check=True)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", help="GitHub URL or owner/repo")
    parser.add_argument("-d", "--dest", default=str(DEST_DIR), help="destination folder (default: ./repos)")
    parser.add_argument("--depth", type=int, help="shallow clone depth")
    parser.add_argument("-f", "--force", action="store_true", help="re-clone over an existing copy")
    parser.add_argument("-e", "--env", default=str(ENV_FILE),
                        help="path to the .env holding the replacement identity (default: ./.env)")
    parser.add_argument("--no-rewrite", action="store_true",
                        help="just clone; leave the original contributor info alone")
    parser.add_argument("--no-push", action="store_true",
                        help="stop after rewriting; publish nothing")
    parser.add_argument("--name", help="use this repo name instead of generating one")
    parser.add_argument("--public", action="store_true",
                        help="make the published repo public (default: private)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show the name and the publish commands without running them")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="skip the confirmation prompt before the force-push")
    args = parser.parse_args()

    url = args.url or input("GitHub URL: ")

    try:
        target = clone(url, Path(args.dest).resolve(), args.depth, args.force)
    except (ValueError, FileExistsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"error: git exited with {exc.returncode}", file=sys.stderr)
        return exc.returncode

    if args.no_rewrite:
        print(f"done: {target}")
        return 0

    try:
        author, committer = rewrite_history.rewrite(target, args.env)
    except FileNotFoundError:
        print(f"error: no .env at {args.env} (copy .env.example and fill it in)", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"error: {exc.cmd} failed with {exc.returncode}", file=sys.stderr)
        return exc.returncode

    print(f"rewrote history to {author[0]} <{author[1]}>")

    if args.no_push:
        print(f"done: {target}")
        return 0

    names = [args.name] if args.name else name_repo.suggest(target)
    if not args.name:
        print(f"name candidates: {', '.join(names)}")

    try:
        chosen = push_history.publish_as_new(target, names, env_path=args.env,
                                             private=not args.public, assume_yes=args.yes,
                                             dry_run=args.dry_run)
        if chosen is None:
            return 1
    except (push_history.AuthError, RuntimeError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(f"history was rewritten but not published: {target}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"error: git push failed with {exc.returncode}", file=sys.stderr)
        return exc.returncode

    print(f"done: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
