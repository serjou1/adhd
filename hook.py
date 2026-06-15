#!/usr/bin/env python3
"""Claude Code multi-instance monitor: hook handler.

Invoked by Claude Code on lifecycle events (one process per event). Reads the
event JSON on stdin and writes/updates a per-session state file that the
dashboard (monitor.py) reads. Must be fast and must never fail the host
session, so everything is wrapped and we always exit 0.
"""
import json
import os
import subprocess
import sys
import time
import tempfile

from history import record_closed

STATE_DIR = os.environ.get("ADHD_STATE_DIR") or os.path.join(
    os.path.expanduser("~"), ".adhd", "state")


def detect_tty():
    """Best-effort controlling terminal device, e.g. /dev/ttys011.

    The hook's stdin/stdout are piped by Claude Code, so os.ttyname usually
    fails; fall back to asking ps for the tty of our parent (the claude proc).
    """
    for fd in (2, 1, 0):
        try:
            return os.ttyname(fd)
        except OSError:
            pass
    try:
        out = subprocess.check_output(
            ["ps", "-o", "tty=", "-p", str(os.getppid())],
            stderr=subprocess.DEVNULL, text=True).strip()
        if out and out not in ("?", "??"):
            return out if out.startswith("/dev/") else "/dev/" + out
    except Exception:
        pass
    return ""


def detect_root(cwd):
    """The project root, i.e. the folder VS Code most likely has open.

    The session cwd may be a deep subdir (e.g. crates/foo/src); the git
    top-level is a far better focus target and display name. Falls back to cwd.
    """
    try:
        out = subprocess.check_output(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL, text=True).strip()
        if out:
            return out
    except Exception:
        pass
    return cwd


def detect_terminal(cwd):
    """Identifiers the dashboard uses to focus this session's window later."""
    return {
        "term_program": os.environ.get("TERM_PROGRAM", ""),
        "tmux_pane": os.environ.get("TMUX_PANE", ""),
        "iterm_session_id": os.environ.get("ITERM_SESSION_ID", ""),
        "tty": detect_tty(),
        "root": detect_root(cwd),
    }


def classify(data):
    """Return (state, detail) for the incoming event.

    states: working | waiting | idle | done
      working -> actively processing (prompt/tool in flight)
      waiting -> blocked on a permission/approval prompt (needs YOU)
      idle    -> finished a turn, sitting at the prompt for next input
      done    -> session ended (file is removed instead, see main)
    """
    event = data.get("hook_event_name", "")

    if event == "UserPromptSubmit":
        return "working", "thinking"
    if event == "PreToolUse":
        return "working", data.get("tool_name", "tool")
    if event == "PostToolUse":
        return "working", "ran " + data.get("tool_name", "tool")
    if event == "SubagentStop":
        return "working", "subagent done"
    if event == "SessionStart":
        return "idle", data.get("source", "started")
    if event == "Stop":
        return "idle", "done"
    if event == "Notification":
        ntype = str(data.get("notification_type", ""))
        msg = str(data.get("message", ""))
        low = (ntype + " " + msg).lower()
        if "permission" in low or "approve" in low or "allow" in low or "needs your" in low:
            return "waiting", msg or "needs approval"
        if "waiting for your input" in low or "idle" in low:
            return "idle", "waiting for input"
        return "waiting", msg or "needs attention"
    # Unknown event: don't change semantics, just note it.
    return "working", event or "active"


def main():
    raw = sys.stdin.read()
    data = json.loads(raw) if raw.strip() else {}

    sid = data.get("session_id") or "unknown"
    event = data.get("hook_event_name", "")
    path = os.path.join(STATE_DIR, sid + ".json")

    # Session ended cleanly: record it to history, then remove its state file so
    # it drops off the dashboard. (Crash/power-off skips SessionEnd, so the
    # dashboard's reaper records those closures instead — see monitor.load_sessions.)
    if event == "SessionEnd":
        try:
            with open(path) as f:
                record_closed(json.load(f))
        except Exception:
            pass
        try:
            os.remove(path)
        except OSError:
            pass
        return

    state, detail = classify(data)
    cwd = data.get("cwd") or os.getcwd()

    # Reuse the terminal block captured on a prior event so we only pay the
    # tty/ps lookup once per session, not on every hook fire.
    term = None
    try:
        with open(path) as f:
            term = json.load(f).get("term")
    except Exception:
        pass
    if not term:
        term = detect_terminal(cwd)
    elif not term.get("root"):
        term["root"] = detect_root(cwd)  # backfill for pre-existing sessions

    root = term.get("root") or cwd
    record = {
        "session_id": sid,
        "cwd": cwd,
        "project": os.path.basename(root.rstrip("/")) or root,
        "state": state,
        "detail": detail,
        "event": event,
        "model": data.get("model", ""),
        "term": term,
        "updated": time.time(),
    }

    os.makedirs(STATE_DIR, exist_ok=True)
    # Atomic write so the dashboard never reads a half-written file.
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(record, f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # never break the host Claude Code session
    sys.exit(0)
