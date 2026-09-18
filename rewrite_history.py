#!/usr/bin/env python3
"""Rewrite every author/committer/tagger in a repo to one identity from .env.

Uses `git fast-export` piped through a filter into `git fast-import`, so it
needs nothing beyond git itself.
"""

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
IDENT_FIELDS = (b"author ", b"committer ", b"tagger ")
# GitHub reads these trailers and credits them as contributors, so an identity
# rewrite that ignores them leaves the old names on the repo's contributor list
CO_AUTHOR = re.compile(rb"(?im)^[ \t]*co-authored-by:[^\n]*\n?")


def load_env(path):
    """Parse a .env file into a dict. Handles quotes, comments and `export`."""
    env = {}
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"no .env at {path} (copy .env.example and fill it in)")
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        match = ENV_LINE.match(raw)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if value[:1] in ("\"", "'"):
            quote = value[0]
            end = value.find(quote, 1)
            # unterminated quote: fall through and take the line as-is
            value = value[1:end] if end != -1 else value
        else:
            value = value.split(" #", 1)[0].strip()
        env[key] = value
    return env


def identity_from_env(env):
    """Pull author/committer name and email out of the parsed .env."""

    def pick(*keys):
        for key in keys:
            if env.get(key):
                return env[key]
        return None

    name = pick("GIT_AUTHOR_NAME", "GIT_NAME", "NAME")
    email = pick("GIT_AUTHOR_EMAIL", "GIT_EMAIL", "EMAIL")
    if not name or not email:
        raise ValueError(
            "GIT_AUTHOR_NAME and GIT_AUTHOR_EMAIL (or NAME/EMAIL) must be set in the .env file"
        )

    committer_name = pick("GIT_COMMITTER_NAME") or name
    committer_email = pick("GIT_COMMITTER_EMAIL") or email

    for value in (name, email, committer_name, committer_email):
        if "<" in value or ">" in value or "\n" in value:
            raise ValueError(f"'<', '>' and newlines are not allowed in an identity: {value!r}")

    return (name, email), (committer_name, committer_email)


def _strip_coauthors(message):
    """Drop Co-authored-by trailers, tidying the blank lines they leave behind."""
    cleaned = CO_AUTHOR.sub(b"", message)
    if cleaned == message:
        return message, 0
    dropped = len(CO_AUTHOR.findall(message))
    tidied = cleaned.rstrip(b" \t\r\n")
    if tidied and message.endswith(b"\n"):
        tidied += b"\n"
    return tidied, dropped


def _ident(field, name, email):
    return field + f"{name} <{email}>".encode("utf-8")


def _filter_stream(src, dst, author, committer, control=None, on_commit=None, drop_coauthors=True):
    """Copy a fast-export stream, replacing identities but keeping timestamps.

    Returns the number of co-author trailers removed.
    """
    idents = {
        b"author ": _ident(b"author ", *author),
        b"committer ": _ident(b"committer ", *committer),
        b"tagger ": _ident(b"tagger ", *committer),
    }

    # a data block right after committer/tagger is a message; every other one
    # is file content and must pass through byte for byte
    expect_message = False
    dropped = 0

    while True:
        line = src.readline()
        if not line:
            return dropped
        if control is not None:
            control.checkpoint()
        if on_commit is not None and line.startswith(b"commit "):
            on_commit()

        # `data <n>` is followed by n raw bytes (commit message or file
        # content). Copy them verbatim so their text is never mistaken
        # for a command.
        if line.startswith(b"data ") and line[5:].strip().isdigit():
            remaining = int(line[5:].strip())
            if expect_message and drop_coauthors:
                message = b""
                while len(message) < remaining:
                    chunk = src.read(remaining - len(message))
                    if not chunk:
                        raise EOFError("fast-export stream ended inside a commit message")
                    message += chunk
                message, removed = _strip_coauthors(message)
                dropped += removed
                dst.write(b"data %d\n" % len(message))
                dst.write(message)
            else:
                dst.write(line)
                while remaining > 0:
                    chunk = src.read(min(1 << 16, remaining))
                    if not chunk:
                        raise EOFError("fast-export stream ended inside a data block")
                    dst.write(chunk)
                    remaining -= len(chunk)
            expect_message = False
            continue

        for field, replacement in idents.items():
            if line.startswith(field):
                if field in (b"committer ", b"tagger "):
                    expect_message = True
                # ... <email> 1700000000 +0900 -- keep everything after the email
                cut = line.rfind(b"> ")
                if cut != -1:
                    line = replacement + b" " + line[cut + 2:]
                break

        dst.write(line)


def rewrite(repo, env_path=None, prune=True, identity=None, control=None, on_progress=None,
            drop_coauthors=True):
    """Rewrite all refs in `repo` to one identity.

    The identity comes from `identity` as (name, email), or from the .env at
    `env_path`. `control` makes the rewrite pausable; `on_progress` is called
    with a 0..1 fraction as commits stream past.
    """
    repo = Path(repo).resolve()
    if identity is not None:
        author = committer = tuple(identity)
    else:
        author, committer = identity_from_env(load_env(env_path))

    total = 0
    if on_progress is not None:
        counted = subprocess.run(["git", "-C", str(repo), "rev-list", "--all", "--count"],
                                 capture_output=True, text=True)
        total = int(counted.stdout.strip() or 0) if counted.returncode == 0 else 0

    seen = 0

    def on_commit():
        nonlocal seen
        seen += 1
        if on_progress is not None and total:
            on_progress(min(1.0, seen / total))

    spawn = control.popen if control is not None else subprocess.Popen
    export = spawn(
        ["git", "-C", str(repo), "fast-export", "--all", "--reencode=yes",
         "--signed-tags=strip", "--tag-of-filtered-object=rewrite"],
        stdout=subprocess.PIPE,
    )
    errors = tempfile.TemporaryFile()
    importer = spawn(
        ["git", "-C", str(repo), "fast-import", "--force", "--quiet",
         "--date-format=raw-permissive"],
        stdin=subprocess.PIPE, stderr=errors,
    )

    try:
        dropped = _filter_stream(export.stdout, importer.stdin, author, committer,
                                 control, on_commit, drop_coauthors) or 0
    except BrokenPipeError:
        dropped = 0  # fast-import died; its own stderr says why, reported below
    finally:
        export.stdout.close()
        try:
            importer.stdin.close()
        except BrokenPipeError:
            pass
        if control is not None:
            control.release(export)
            control.release(importer)

    if importer.wait() != 0:
        errors.seek(0)
        detail = errors.read().decode("utf-8", "replace").strip().splitlines()
        errors.close()
        raise RuntimeError(f"git fast-import failed: {detail[0] if detail else 'no output'}")
    errors.close()
    if export.wait() != 0:
        raise subprocess.CalledProcessError(export.returncode, "git fast-export")

    # The working tree still points at the pre-rewrite commits.
    subprocess.run(["git", "-C", str(repo), "reset", "--hard"], check=True,
                   stdout=subprocess.DEVNULL)

    if prune:
        subprocess.run(["git", "-C", str(repo), "reflog", "expire", "--expire=now", "--all"],
                       check=True)
        subprocess.run(["git", "-C", str(repo), "gc", "--prune=now", "--quiet"], check=True)

    return author, committer, dropped


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", help="path to the repository to rewrite")
    parser.add_argument("-e", "--env", default=str(Path(__file__).resolve().parent / ".env"),
                        help="path to the .env file (default: .env next to this script)")
    parser.add_argument("--keep-coauthors", action="store_true",
                        help="keep Co-authored-by trailers (GitHub credits them as contributors)")
    parser.add_argument("--no-prune", action="store_true",
                        help="keep the pre-rewrite objects reachable through the reflog")
    args = parser.parse_args()

    try:
        author, committer, dropped = rewrite(args.repo, args.env, prune=not args.no_prune,
                                             drop_coauthors=not args.keep_coauthors)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"error: {exc.cmd} failed with {exc.returncode}", file=sys.stderr)
        return exc.returncode

    print(f"rewrote history: author {author[0]} <{author[1]}>, committer {committer[0]} <{committer[1]}>")
    if dropped:
        print(f"removed {dropped} co-author trailer{'s' if dropped != 1 else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
