#!/usr/bin/env python3
"""Record of the repositories this program published, with a retention sweep.

Every published repo is written to history.json. Entries older than the
retention window (7 days by default) are deleted from GitHub and their local
clone removed. Only repos listed in this file are ever touched.
"""

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STORE = BASE_DIR / "history.json"
REPOS_DIR = BASE_DIR / "repos"
DEFAULT_RETENTION_DAYS = 7


def now():
    return datetime.now(timezone.utc)


def _stamp(moment=None):
    return (moment or now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(stamp):
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def load(path=STORE):
    """Read the store, tolerating a missing or damaged file."""
    path = Path(path)
    if not path.is_file():
        return {"version": 1, "settings": {"retention_days": DEFAULT_RETENTION_DAYS}, "repos": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "settings": {"retention_days": DEFAULT_RETENTION_DAYS}, "repos": []}
    data.setdefault("settings", {}).setdefault("retention_days", DEFAULT_RETENTION_DAYS)
    data.setdefault("repos", [])
    return data


def save(data, path=STORE):
    path = Path(path)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)  # atomic, so a crash cannot leave a half-written store


def retention_days(path=STORE):
    return int(load(path)["settings"]["retention_days"])


def set_retention_days(days, path=STORE):
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise ValueError("retention must be a whole number of days")
    if days < 0:
        raise ValueError("retention must be 0 days or more")
    if days > 3650:
        raise ValueError("retention must be 3650 days or fewer")
    data = load(path)
    data["settings"]["retention_days"] = days
    save(data, path)
    return days


def record(login, name, url, source, clone_path, private, path=STORE):
    """Add a published repo to the store."""
    data = load(path)
    data["repos"].append({
        "login": login,
        "name": name,
        "url": url,
        "source": source,
        "path": str(clone_path) if clone_path else None,
        "private": bool(private),
        "created_at": _stamp(),
        "deleted_at": None,
        "keep": False,
    })
    save(data, path)
    return data["repos"][-1]


def expiry(entry, days):
    created = _parse(entry.get("created_at"))
    return created + timedelta(days=days) if created else None


def annotate(entry, days, moment=None):
    """Add the derived fields the UI shows, without storing them."""
    moment = moment or now()
    due = expiry(entry, days)
    view = dict(entry)
    view["expires_at"] = _stamp(due) if due else None
    if entry.get("deleted_at"):
        view["status"] = "deleted"
        view["days_left"] = None
    elif entry.get("keep"):
        view["status"] = "kept"
        view["days_left"] = None
    elif due is None:
        view["status"] = "live"
        view["days_left"] = None
    else:
        remaining = (due - moment).total_seconds() / 86400
        view["status"] = "expired" if remaining <= 0 else "live"
        view["days_left"] = round(remaining, 2)
    return view


def listing(path=STORE, include_deleted=True, moment=None):
    data = load(path)
    days = int(data["settings"]["retention_days"])
    rows = [annotate(entry, days, moment) for entry in data["repos"]
            if include_deleted or not entry.get("deleted_at")]
    rows.sort(key=lambda row: row.get("created_at") or "", reverse=True)
    return {"retention_days": days, "repos": rows}


def find(name, path=STORE):
    for entry in load(path)["repos"]:
        if entry["name"] == name and not entry.get("deleted_at"):
            return entry
    return None


def set_keep(name, keep, path=STORE):
    data = load(path)
    for entry in data["repos"]:
        if entry["name"] == name and not entry.get("deleted_at"):
            entry["keep"] = bool(keep)
            save(data, path)
            return entry
    raise LookupError(f"no live entry named '{name}'")


def _remove_clone(clone_path):
    """Delete the local clone, but only from inside the repos directory."""
    if not clone_path:
        return False
    target = Path(clone_path).resolve()
    try:
        target.relative_to(REPOS_DIR.resolve())
    except ValueError:
        return False  # refuse to touch anything outside repos/
    if not target.is_dir():
        return False
    shutil.rmtree(target)
    return True


def delete(name, path=STORE, drop_clone=True):
    """Delete one recorded repo from GitHub and remove its local clone."""
    entry = find(name, path)
    if entry is None:
        raise LookupError(f"no live entry named '{name}'")

    slug = f"{entry['login']}/{entry['name']}"
    probe = subprocess.run(["gh", "repo", "delete", slug, "--yes"],
                           capture_output=True, text=True)
    detail = (probe.stderr or probe.stdout or "").strip()

    if probe.returncode != 0:
        if "Could not resolve" in detail or "Not Found" in detail:
            pass  # already gone on GitHub; fall through and close the entry
        elif "delete_repo" in detail:
            raise PermissionError(
                "deleting needs the delete_repo scope: "
                "run `gh auth refresh -h github.com -s delete_repo`"
            )
        else:
            raise RuntimeError(detail.splitlines()[0] if detail else "gh repo delete failed")

    removed = _remove_clone(entry.get("path")) if drop_clone else False

    data = load(path)
    for stored in data["repos"]:
        if stored["name"] == name and not stored.get("deleted_at"):
            stored["deleted_at"] = _stamp()
            stored["clone_removed"] = removed
            break
    save(data, path)
    return {"repo": slug, "clone_removed": removed}


def sweep(path=STORE, dry_run=False, moment=None):
    """Delete every live entry past its retention window."""
    state = listing(path, moment=moment)
    due = [row for row in state["repos"] if row["status"] == "expired"]
    results = []
    for row in due:
        if dry_run:
            results.append({"repo": f"{row['login']}/{row['name']}", "action": "would delete"})
            continue
        try:
            delete(row["name"], path)
            results.append({"repo": f"{row['login']}/{row['name']}", "action": "deleted"})
        except (LookupError, PermissionError, RuntimeError, OSError) as exc:
            results.append({"repo": f"{row['login']}/{row['name']}", "action": f"failed: {exc}"})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show the recorded repositories")
    sweep_cmd = sub.add_parser("sweep", help="delete everything past the retention window")
    sweep_cmd.add_argument("--dry-run", action="store_true", help="show what would go")
    delete_cmd = sub.add_parser("delete", help="delete one recorded repository now")
    delete_cmd.add_argument("name")
    keep_cmd = sub.add_parser("keep", help="exempt a repository from the sweep")
    keep_cmd.add_argument("name")
    keep_cmd.add_argument("--off", action="store_true", help="stop exempting it")
    retention_cmd = sub.add_parser("retention", help="show or set the retention window")
    retention_cmd.add_argument("days", nargs="?", type=int)
    args = parser.parse_args()

    try:
        if args.command == "list":
            state = listing()
            print(f"retention: {state['retention_days']} days")
            if not state["repos"]:
                print("(nothing published yet)")
            for row in state["repos"]:
                left = "" if row["days_left"] is None else f"  {row['days_left']:.1f}d left"
                print(f"  [{row['status']:7}] {row['login']}/{row['name']:<28} "
                      f"{row['created_at']}{left}")
        elif args.command == "sweep":
            results = sweep(dry_run=args.dry_run)
            print("\n".join(f"{r['action']}: {r['repo']}" for r in results) or "nothing to sweep")
        elif args.command == "delete":
            print(delete(args.name))
        elif args.command == "keep":
            entry = set_keep(args.name, not args.off)
            print(f"{entry['name']}: keep={entry['keep']}")
        elif args.command == "retention":
            print(set_retention_days(args.days) if args.days is not None else retention_days(),
                  "days")
    except (LookupError, PermissionError, RuntimeError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
