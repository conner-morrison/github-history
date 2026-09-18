#!/usr/bin/env python3
"""Run the clone -> rewrite -> name -> publish sequence as one observable job.

Holds the state the UI renders: phase, percent, log and result. Every step is
pausable and stoppable through a shared proc.Control.
"""

import threading
import traceback
from pathlib import Path

import clone_repo
import history
import name_repo
import proc
import push_history
import rewrite_history

# (phase, start percent, end percent) - the spans are rough shares of a typical run
STEPS = [
    ("verify", 0, 5),
    ("clone", 5, 45),
    ("rewrite", 45, 75),
    ("name", 75, 80),
    ("publish", 80, 100),
]
SPAN = {phase: (start, end) for phase, start, end in STEPS}


class Pipeline:
    """One run. Create it, start it, poll snapshot()."""

    def __init__(self, url, username, email, public=False, dest=None, name=None, dry_run=False):
        self.url = url
        self.identity = (username, email)
        self.public = public
        self.dest = Path(dest) if dest else clone_repo.DEST_DIR
        self.forced_name = name or None
        self.dry_run = dry_run

        self.control = proc.Control()
        self._lock = threading.Lock()
        self._thread = None
        self.state = "idle"       # idle | running | paused | done | stopped | error
        self.phase = ""
        self.percent = 0.0
        self.log = []
        self.result = None
        self.error = None

    # ---------- state ----------

    def say(self, message):
        with self._lock:
            self.log.append(message)
            del self.log[:-400]

    def _set(self, phase=None, percent=None, state=None):
        with self._lock:
            if phase is not None:
                self.phase = phase
            if percent is not None:
                self.percent = max(self.percent, min(100.0, percent))
            if state is not None:
                self.state = state

    def _advance(self, phase, fraction):
        start, end = SPAN[phase]
        self._set(phase=phase, percent=start + (end - start) * max(0.0, min(1.0, fraction)))

    def snapshot(self):
        with self._lock:
            return {
                "state": self.state,
                "phase": self.phase,
                "percent": round(self.percent, 1),
                "log": list(self.log[-200:]),
                "result": self.result,
                "error": self.error,
                "paused": self.control.paused,
            }

    # ---------- controls ----------

    def start(self):
        if self._thread and self._thread.is_alive():
            return False
        self._set(state="running", phase="verify", percent=0)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def pause(self):
        if self.state != "running" or not self.control.pause():
            return False
        self._set(state="paused")
        self.say("-- paused --")
        return True

    def resume(self):
        if self.state != "paused" or not self.control.resume():
            return False
        self._set(state="running")
        self.say("-- resumed --")
        return True

    def stop(self):
        if self.state not in ("running", "paused"):
            return False
        self.control.stop()
        self.say("-- stopping --")
        return True

    # ---------- the run ----------

    def _git_progress(self, phase, weights):
        """Turn git's progress lines into a fraction inside one phase.

        `weights` maps a git label ("Receiving objects") to the slice of the
        phase it accounts for, as (start, end) fractions.
        """
        def handle(line):
            label, percent = proc.parse_percent(line)
            if label and label in weights:
                start, end = weights[label]
                self._advance(phase, start + (end - start) * percent / 100)
            elif line:
                self.say(line)
        return handle

    def _run(self):
        try:
            username, email = self.identity

            # 1. confirm GitHub is connected before spending time on a clone
            self._advance("verify", 0.2)
            self.say("checking GitHub connection ...")
            _, _, account = push_history.check(None, identity=self.identity)
            self.say(f"connected as github.com/{account['login']}")
            self.say(f"commits will carry {username} <{email}>")
            self._advance("verify", 1.0)

            # 2. clone
            owner, repo_name = clone_repo.parse_github_url(self.url)
            target = self.dest / owner / repo_name
            source = f"https://github.com/{owner}/{repo_name}.git"
            self.say(f"cloning {source}")
            if target.exists():
                self.say(f"removing previous copy at {target}")
                self.control.run(["rm", "-rf", str(target)])
            target.parent.mkdir(parents=True, exist_ok=True)
            self.control.stream(
                ["git", "clone", "--progress", source, str(target)],
                self._git_progress("clone", {"Receiving objects": (0.0, 0.85),
                                             "Resolving deltas": (0.85, 1.0)}),
            )
            self._advance("clone", 1.0)
            self.say(f"cloned into {target}")

            # 3. rewrite every author and committer
            self.say("rewriting history ...")
            _, _, dropped = rewrite_history.rewrite(
                target, identity=self.identity, control=self.control,
                on_progress=lambda fraction: self._advance("rewrite", fraction),
            )
            self._advance("rewrite", 1.0)
            self.say(f"history rewritten to {username} <{email}>")
            if dropped:
                self.say(f"removed {dropped} co-author trailer(s) so GitHub credits nobody else")

            # 4. name it
            self._advance("name", 0.3)
            names = [self.forced_name] if self.forced_name else name_repo.suggest(target)
            self.say(f"name candidates: {', '.join(names)}")
            self._advance("name", 1.0)

            # 5. publish to a new repo on the verified account
            self.control.checkpoint()
            self.say("publishing ...")
            chosen = push_history.publish_as_new(
                target, names, identity=self.identity, private=not self.public,
                assume_yes=True, dry_run=self.dry_run, control=self.control,
                on_progress=self._git_progress("publish", {"Writing objects": (0.0, 0.9),
                                                           "Resolving deltas": (0.9, 1.0)}),
            )
            if chosen is None:
                raise RuntimeError("publish was refused")

            published = f"https://github.com/{account['login']}/{chosen}"
            if not self.dry_run:
                history.record(account["login"], chosen, published, self.url,
                               target, not self.public)
                self.say(f"recorded in history (kept {history.retention_days()} days)")

            self._set(phase="done", percent=100, state="done")
            with self._lock:
                self.result = {
                    "name": chosen,
                    "url": published,
                    "path": str(target),
                    "private": not self.public,
                    "dry_run": self.dry_run,
                }
            self.say(f"finished: {self.result['url']}")

        except proc.Cancelled:
            self._set(state="stopped", phase="stopped")
            self.say("-- stopped --")
        except Exception as exc:  # surfaced in the UI rather than a traceback on stderr
            with self._lock:
                self.error = f"{type(exc).__name__}: {exc}"
                self.state = "error"
                self.phase = "error"
            self.say(f"error: {self.error}")
            for line in traceback.format_exc().splitlines()[-4:]:
                self.say(line)
