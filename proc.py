#!/usr/bin/env python3
"""Subprocess control: pause, resume and stop long-running git commands.

Pausing is real, not cooperative. Children are started in their own process
group and receive SIGSTOP, so a clone that is halfway through receiving objects
genuinely freezes and picks up where it left off on SIGCONT.
"""

import os
import re
import signal
import subprocess
import threading

PROGRESS = re.compile(r"^(?P<label>[A-Za-z][A-Za-z ]+):\s+(?P<percent>\d{1,3})%")


class Cancelled(Exception):
    """Raised inside a step when the user presses Stop."""


class Control:
    """Shared pause/resume/stop state for everything one run spawns."""

    def __init__(self):
        self._lock = threading.Lock()
        self._procs = []
        self._stopped = threading.Event()
        self._running = threading.Event()
        self._running.set()  # not paused

    @property
    def paused(self):
        return not self._running.is_set()

    @property
    def stopped(self):
        return self._stopped.is_set()

    def _signal_all(self, sig):
        for proc in list(self._procs):
            if proc.poll() is not None:
                continue
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except (ProcessLookupError, PermissionError):
                pass

    def pause(self):
        with self._lock:
            if self._stopped.is_set() or self.paused:
                return False
            self._running.clear()
            self._signal_all(signal.SIGSTOP)
            return True

    def resume(self):
        with self._lock:
            if self._stopped.is_set() or not self.paused:
                return False
            self._signal_all(signal.SIGCONT)
            self._running.set()
            return True

    def stop(self):
        with self._lock:
            self._stopped.set()
            self._signal_all(signal.SIGCONT)  # a stopped process cannot act on SIGTERM
            self._signal_all(signal.SIGTERM)
            self._running.set()  # release anything blocked in checkpoint()
            return True

    def checkpoint(self):
        """Block while paused; raise Cancelled if stopped."""
        if self._stopped.is_set():
            raise Cancelled("stopped")
        if not self._running.wait(timeout=3600):
            raise Cancelled("paused for too long")
        if self._stopped.is_set():
            raise Cancelled("stopped")

    def popen(self, cmd, **kwargs):
        self.checkpoint()
        kwargs.setdefault("start_new_session", True)  # its own process group, for killpg
        proc = subprocess.Popen(cmd, **kwargs)
        with self._lock:
            self._procs.append(proc)
            if self.paused:
                # paused between the checkpoint and the spawn
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGSTOP)
                except (ProcessLookupError, PermissionError):
                    pass
        return proc

    def release(self, proc):
        with self._lock:
            if proc in self._procs:
                self._procs.remove(proc)

    def run(self, cmd, check=True, capture=False, **kwargs):
        """Run to completion, staying responsive to pause and stop."""
        if capture:
            kwargs.setdefault("stdout", subprocess.PIPE)
            kwargs.setdefault("stderr", subprocess.PIPE)
            kwargs.setdefault("text", True)
        proc = self.popen(cmd, **kwargs)
        try:
            out, err = proc.communicate()
        finally:
            self.release(proc)
        if self._stopped.is_set():
            raise Cancelled("stopped")
        if check and proc.returncode != 0:
            detail = (err or "").strip().splitlines()
            raise subprocess.CalledProcessError(proc.returncode, cmd,
                                                output=out, stderr=detail[-1] if detail else "")
        return proc.returncode, out, err

    def stream(self, cmd, on_line=None, **kwargs):
        """Run a command, feeding each stderr line to on_line as it appears.

        git writes its progress meter to stderr with \\r, not \\n, so the
        stream is split on both.
        """
        proc = self.popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **kwargs)
        buffer = b""
        try:
            while True:
                chunk = proc.stderr.read(1)
                if not chunk:
                    break
                if chunk in (b"\r", b"\n"):
                    line = buffer.decode("utf-8", "replace").strip()
                    buffer = b""
                    if line and on_line:
                        on_line(line)
                    self.checkpoint()
                else:
                    buffer += chunk
            if buffer and on_line:
                on_line(buffer.decode("utf-8", "replace").strip())
        finally:
            proc.stderr.close()
            proc.wait()
            self.release(proc)
        if self._stopped.is_set():
            raise Cancelled("stopped")
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd)
        return proc.returncode


def parse_percent(line):
    """Pull ('Receiving objects', 42) out of a git progress line."""
    match = PROGRESS.match(line)
    if not match:
        return None, None
    return match.group("label"), min(100, int(match.group("percent")))
