#!/usr/bin/env python3
"""Live Codex-session poller for adhd.

Codex (the `codex` CLI) has no lifecycle-hook system like Claude Code, so it
can't *push* its state into ~/.adhd/state the way hook.py does for Claude.
Instead we *poll*: every running session is fully observable from the outside.

  - The process: a live `codex` shows up in `ps` (comm basename `codex`). Its
    controlling tty, working directory (via lsof), start time, and terminal env
    (TERM_PROGRAM / ITERM_SESSION_ID / TMUX_PANE, via `ps -E`) are all readable
    without privileges — the same identifiers focus_session() needs to raise the
    window later.
  - The transcript: each session continuously appends a rollout JSONL under
    ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<session_id>.jsonl. Its first line
    (session_meta) carries the session id + cwd; the event_msg lines after it
    say what the agent is doing (task_started, agent_message, task_complete, …).

codex_sessions() ties the two together and returns records in the *same shape*
hook.py writes for Claude (plus a "tool": "codex" tag), so monitor.py and
menubar.py render, focus, and notify them through their existing, tool-agnostic
machinery — no special-casing downstream.

posix_spawn safety: codex_sessions() runs inside menubar.py's AppKit run loop.
A fork() there intermittently breaks the status item's WindowServer link and the
icon silently vanishes (see monitor.live_ttys and commit d609fc3). So every
subprocess here passes an absolute exe path + close_fds=False, which makes
CPython take the fork-free posix_spawn branch.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import time

from history import record_closed

SESSIONS_DIR = os.environ.get("CODEX_SESSIONS_DIR") or os.path.join(
    os.path.expanduser("~"), ".codex", "sessions")

# Absolute exe paths so subprocess uses posix_spawn, not fork()+exec() — see the
# module docstring. shutil.which result is cached at import (PATH won't move).
_PS = shutil.which("ps") or "/bin/ps"
_LSOF = shutil.which("lsof") or "/usr/sbin/lsof"
_GIT = shutil.which("git") or "/usr/bin/git"
_OSASCRIPT = shutil.which("osascript") or "/usr/bin/osascript"

# Codex signals "needs you" — an approval / attention prompt — by setting its
# terminal tab TITLE (e.g. "[ ! ] Action Required | <cwd>"), NOT by writing
# anything to the rollout. So the rollout alone can't tell a waiting session from
# a working one; we read the tab's live OSC title from the host terminal and look
# for these markers to promote it to "waiting". Lower-cased substring match.
_ATTENTION_MARKERS = ("action required", "[ ! ]", "needs your", "needs approval",
                      "waiting for approval")

# Read every tab's tty + its live (OSC-set) custom title in one shot. `custom
# title` is the title the running program set; absent tabs come back blank. tty
# and title are joined by US (\x1f) so a title can contain anything printable.
_TERMINAL_TITLES_OSA = r'''tell application "Terminal"
	set out to ""
	repeat with w in windows
		repeat with t in tabs of w
			set ct to ""
			try
				set ct to custom title of t
			end try
			set out to out & (tty of t) & (character id 31) & ct & linefeed
		end repeat
	end repeat
	return out
end tell'''

# iTerm equivalent: a session carries its own tty + (title-reflecting) name.
_ITERM_TITLES_OSA = r'''tell application "iTerm"
	set out to ""
	repeat with w in windows
		repeat with t in tabs of w
			repeat with s in sessions of t
				set out to out & (tty of s) & (character id 31) & (name of s) & linefeed
			end repeat
		end repeat
	end repeat
	return out
end tell'''

TAIL_BYTES = 65536  # how much of a rollout's end we read to classify its state

# event_msg payload `type`s that carry no liveness signal: usage accounting that
# can trail a completed turn. Ignoring them keeps a finished session reading idle
# instead of flipping back to "working" on a stray post-completion count.
_NEUTRAL = {"token_count"}


def _run(cmd):
    """Run `cmd` fork-free, return stdout (str), or '' on any failure."""
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True,
                              close_fds=False).stdout
    except Exception:
        return ""


def _live_codex_pids():
    """{pid: tty} for every running `codex` process (empty if ps fails).

    tty is bare (e.g. 'ttys000'); '??' when the process has no controlling
    terminal. Matches comm by basename so a full path like
    /Users/me/.local/bin/codex still counts.
    """
    out = _run([_PS, "-axo", "pid=,tty=,comm="])
    pids = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and os.path.basename(parts[2].strip()) == "codex":
            try:
                pids[int(parts[0])] = parts[1]
            except ValueError:
                pass
    return pids


def _codex_cwds(pids):
    """{pid: cwd} for the given codex pids, via one lsof call.

    `lsof -a -c codex -d cwd -Fpn` emits machine-readable records: a `p<pid>`
    line then an `n<path>` line per process. Only pids we asked about are kept.
    """
    if not pids:
        return {}
    out = _run([_LSOF, "-a", "-c", "codex", "-d", "cwd", "-Fpn"])
    cwds = {}
    cur = None
    for line in out.splitlines():
        if line.startswith("p"):
            try:
                cur = int(line[1:])
            except ValueError:
                cur = None
        elif line.startswith("n") and cur in pids:
            cwds[cur] = line[1:]
    return cwds


_ENV_KEYS = ("TERM_PROGRAM", "ITERM_SESSION_ID", "TMUX", "TMUX_PANE")


def _proc_env(pid):
    """The terminal-identifying env vars of `pid` (own processes only).

    `ps -E` appends the environment after the command; we pluck just the keys
    focus_session() cares about. Values for these keys never contain spaces, so
    a simple token scan is enough. Missing keys come back absent.
    """
    out = _run([_PS, "-E", "-ww", "-o", "command=", "-p", str(pid)])
    env = {}
    for key in _ENV_KEYS:
        m = re.search(r"(?:^|\s)" + key + r"=(\S*)", out)
        if m:
            env[key] = m.group(1)
    return env


def _proc_start(pid):
    """Unix start time of `pid` (local wall clock), or 0.0 if unknown.

    Used only to disambiguate two codex sessions sharing one cwd: we pair each
    process with the rollout whose start is closest. `ps -o lstart` prints the
    fixed 'Tue Jun 23 22:34:11 2026' form.
    """
    out = _run([_PS, "-o", "lstart=", "-p", str(pid)]).strip()
    if not out:
        return 0.0
    try:
        return time.mktime(time.strptime(out, "%a %b %d %H:%M:%S %Y"))
    except (ValueError, OverflowError):
        return 0.0


_ROLLOUT_RE = re.compile(
    r"rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-")


def _rollout_start(path):
    """Session start time encoded in a rollout filename, as local epoch (or 0)."""
    m = _ROLLOUT_RE.search(os.path.basename(path))
    if not m:
        return 0.0
    try:
        return time.mktime(time.strptime(m.group(1), "%Y-%m-%dT%H-%M-%S"))
    except (ValueError, OverflowError):
        return 0.0


_meta_cache = {}  # path -> session_meta payload dict (immutable first line)


def _session_meta(path):
    """The rollout's first-line session_meta payload ({} on failure). Cached.

    The first line never changes once written, so we cache it by path and never
    re-read it — only the tail (which grows) is re-read each tick.
    """
    if path in _meta_cache:
        return _meta_cache[path]
    meta = {}
    try:
        with open(path) as f:
            obj = json.loads(f.readline())
        if obj.get("type") == "session_meta":
            meta = obj.get("payload") or {}
    except Exception:
        meta = {}
    _meta_cache[path] = meta
    return meta


def _tail_events(path):
    """event_msg payloads from the tail of a rollout, in file order.

    Reads only the last TAIL_BYTES so cost stays flat as a session grows. A
    partial first line (we may seek into the middle of one) just fails to parse
    and is skipped.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    events = []
    for line in tail.splitlines():
        if '"event_msg"' not in line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") == "event_msg" and isinstance(obj.get("payload"), dict):
            events.append(obj["payload"])
    return events


def _classify(events):
    """(state, detail) from a rollout's tail events — last meaningful one wins.

    We walk the events in order and let each meaningful one overwrite the verdict,
    so the final answer reflects the newest activity:
      task_complete / turn_aborted -> idle      (turn over, at the prompt)
      *approval_request*           -> waiting    (blocked on YOUR yes/no)
      a usage/quota-limit error    -> limit      (blocked on the clock)
      anything else active         -> working    (a turn is in flight)
    token_count is skipped (see _NEUTRAL). No meaningful events -> idle/"started".
    """
    state, detail = "idle", "started"
    for p in events:
        t = p.get("type", "")
        if t in _NEUTRAL:
            continue
        if "approval" in t and "request" in t:
            state, detail = "waiting", "needs approval"
        elif t == "task_complete":
            state, detail = "idle", "done"
        elif t == "turn_aborted":
            state, detail = "idle", "aborted"
        elif t == "task_started":
            state, detail = "working", "thinking"
        elif t == "agent_reasoning":
            state, detail = "working", "thinking"
        elif t == "agent_message":
            state, detail = "working", "responding"
        elif t.startswith("web_search"):
            state, detail = "working", "searching web"
        elif t.startswith("exec_command") or "tool_call" in t or t.startswith("mcp"):
            state, detail = "working", "running tool"
        elif "error" in t:
            msg = str(p.get("message", "")).lower()
            if "limit" in msg and ("usage" in msg or "rate" in msg or "quota" in msg):
                state, detail = "limit", "usage limit reached"
            # other errors are transient; leave the prior verdict in place
    return state, detail


def _title(events):
    """A human label for the session: its most recent user prompt, trimmed.

    The latest user_message says what the session is *currently* about — the
    closest Codex analogue to Claude's rolling chat title. '' when the tail holds
    no user turn (callers fall back to the project name).
    """
    title = ""
    for p in events:
        if p.get("type") == "user_message":
            msg = (p.get("message") or "").strip()
            if msg:
                title = msg
    return title.replace("\n", " ")


_root_cache = {}  # cwd -> git toplevel (or cwd)


def _git_root(cwd):
    """Project root for `cwd` (git toplevel, else cwd). Cached per cwd."""
    if not cwd:
        return cwd
    if cwd in _root_cache:
        return _root_cache[cwd]
    out = _run([_GIT, "-C", cwd, "rev-parse", "--show-toplevel"]).strip()
    root = out or cwd
    _root_cache[cwd] = root
    return root


def _recent_rollouts(since):
    """Rollout paths whose encoded start time is >= `since` (cheap, no reads).

    Bounds how many session_meta first-lines we read each tick: only sessions
    that *started* around or after the oldest live process can belong to one, and
    on a personal machine that's a handful. We don't filter by mtime — an idle
    session waiting at its prompt may not have been written to for a while.
    """
    paths = []
    for p in glob.glob(os.path.join(SESSIONS_DIR, "*", "*", "*", "rollout-*.jsonl")):
        if _rollout_start(p) >= since:
            paths.append(p)
    return paths


def _match_rollouts(procs):
    """Pair each live codex proc with its rollout file.

    `procs` is {pid: {tty, cwd, start, env}}. A session's rollout is created at
    session start in the session's cwd, so we match on cwd and break ties by
    start-time proximity, claiming each rollout once so two concurrent sessions
    in the same directory don't collide. Returns {pid: rollout_path}.
    """
    starts = [p["start"] for p in procs.values() if p["start"]]
    floor = (min(starts) if starts else time.time()) - 300  # 5 min slack
    candidates = _recent_rollouts(floor)
    by_cwd = {}
    for path in candidates:
        cwd = _session_meta(path).get("cwd")
        if cwd:
            by_cwd.setdefault(cwd, []).append(path)

    claimed = set()
    matched = {}
    # Oldest process first: it gets first pick of the oldest matching rollout.
    for pid in sorted(procs, key=lambda p: procs[p]["start"]):
        cwd = procs[pid]["cwd"]
        pool = [p for p in by_cwd.get(cwd, []) if p not in claimed]
        if not pool:
            continue
        start = procs[pid]["start"]
        best = min(pool, key=lambda p: (abs(_rollout_start(p) - start)
                                        if start else 0, -os.path.getmtime(p)))
        claimed.add(best)
        matched[pid] = best
    return matched


def _needs_attention(title):
    """True if a terminal title says Codex is blocked waiting on you."""
    low = title.lower()
    return any(m in low for m in _ATTENTION_MARKERS)


def _terminal_titles(programs):
    """{tty: live tab title} for the terminal apps hosting codex sessions.

    `programs` is the set of TERM_PROGRAM values our codex records carry; we only
    ask the apps actually in use, and only when there's a codex session to check.
    The osascript is read-only (no `activate`), so it never steals focus, and any
    failure (app not running, automation not granted) just yields no titles — we
    then fall back to the rollout-derived state. One call per app per tick.
    """
    titles = {}
    scripts = []
    if "Apple_Terminal" in programs:
        scripts.append(_TERMINAL_TITLES_OSA)
    if "iTerm.app" in programs:
        scripts.append(_ITERM_TITLES_OSA)
    for script in scripts:
        out = _run([_OSASCRIPT, "-e", script])
        for line in out.splitlines():
            tty, sep, ct = line.partition("\x1f")
            if sep and tty:
                titles[tty] = ct
    return titles


_last_records = {}  # session_id -> last record we emitted, for close detection


def codex_sessions():
    """Live Codex sessions as hook.py-shaped records (each tagged tool='codex').

    Derived fresh every call from the live process list + rollout files, so a
    session that exits simply stops appearing — no state files to reap. As a
    side effect we record vanished sessions to the shared closed-project history
    (history.record_closed), the same store Claude closes land in, so a finished
    Codex session shows up under "recently closed" and can be `codex resume`d.
    """
    pids = _live_codex_pids()
    cwds = _codex_cwds(pids)
    procs = {}
    for pid, tty in pids.items():
        procs[pid] = {
            "tty": tty,
            "cwd": cwds.get(pid, ""),
            "start": _proc_start(pid),
            "env": _proc_env(pid),
        }
    matched = _match_rollouts(procs)

    out = []
    current = {}
    for pid, proc in procs.items():
        path = matched.get(pid)
        meta = _session_meta(path) if path else {}
        events = _tail_events(path) if path else []
        sid = meta.get("session_id") or ("pid-%d" % pid)
        cwd = proc["cwd"] or meta.get("cwd") or ""
        root = _git_root(cwd)
        state, detail = _classify(events)
        env = proc["env"]
        rec = {
            "session_id": sid,
            "cwd": cwd,
            "project": os.path.basename(root.rstrip("/")) or root or "?",
            "title": _title(events),
            "state": state,
            "detail": detail,
            "event": "codex",
            "model": meta.get("model") or meta.get("model_provider") or "codex",
            "term": {
                "term_program": env.get("TERM_PROGRAM", ""),
                "tmux_pane": env.get("TMUX_PANE", ""),
                "iterm_session_id": env.get("ITERM_SESSION_ID", ""),
                "tty": ("/dev/" + proc["tty"]) if proc["tty"] and proc["tty"] != "??" else "",
                "root": root,
            },
            "updated": (os.path.getmtime(path) if path and os.path.exists(path)
                        else time.time()),
            "tool": "codex",
        }
        out.append(rec)
        current[sid] = rec

    # Promote sessions whose terminal title shows an approval / attention prompt
    # to "waiting" — the one state Codex exposes only through its tab title, never
    # the rollout (so without this an approval-blocked session reads as working).
    # rec is shared between `out` and `current`, so this also fixes the record we
    # remember for close detection.
    programs = set()
    for r in out:
        if r["term"].get("tty"):
            programs.add(r["term"].get("term_program"))
    if programs:
        titles = _terminal_titles(programs)
        for r in out:
            if _needs_attention(titles.get(r["term"].get("tty", ""), "")):
                r["state"], r["detail"] = "waiting", "needs approval"

    # A session present last tick but gone now has closed: record it, once, to
    # the same history Claude uses. record_closed dedupes by root, so the menu
    # bar and the dashboard both diffing independently is harmless.
    for sid, rec in _last_records.items():
        if sid not in current:
            try:
                record_closed(rec)
            except Exception:
                pass
    _last_records.clear()
    _last_records.update(current)
    return out


if __name__ == "__main__":
    print(json.dumps(codex_sessions(), indent=2))
