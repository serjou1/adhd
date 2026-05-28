#!/usr/bin/env python3
"""Claude Code multi-instance monitor: live terminal dashboard.

Reads the per-session state files written by hook.py and renders a live,
grouped view: instances WAITING for your input float to the top, then the
ones actively WORKING, then the idle/done ones. Run it in a spare terminal
or tmux pane.

Keys:  ↑/↓ (or j/k) select   ⏎ focus that session's window
       c clear stale   q quit
"""
import curses
import glob
import json
import os
import shutil
import subprocess
import time

STATE_DIR = os.environ.get("ADHD_STATE_DIR") or os.path.join(
    os.path.expanduser("~"), ".adhd", "state")
REFRESH = 1.0          # seconds between redraws
STALE_AFTER = 6 * 3600  # entries older than this are pruneable with 'c'

# TERM_PROGRAM value -> macOS application name to `activate`.
APP_NAME = {
    "Apple_Terminal": "Terminal",
    "iTerm.app": "iTerm",
    "vscode": "Visual Studio Code",
    "WezTerm": "WezTerm",
    "Hyper": "Hyper",
    "ghostty": "Ghostty",
}

# Ordering / styling per state.
ORDER = {"waiting": 0, "working": 1, "idle": 2, "done": 2}
LABEL = {"waiting": "WAITING", "working": "WORKING", "idle": "IDLE", "done": "IDLE"}


def live_ttys():
    """TTYs that currently host a live `claude` process.

    Returns a set like {"ttys000", ...}, or None if we couldn't tell (in which
    case callers must not prune — better a stale row than dropping a live one).
    """
    try:
        out = subprocess.run(["ps", "-axo", "tty=,comm="],
                             stdout=subprocess.PIPE, text=True).stdout
    except Exception:
        return None
    ttys = set()
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and os.path.basename(parts[1].strip()) == "claude":
            ttys.add(parts[0])
    return ttys


def load_sessions():
    # Crashed/force-closed sessions never send SessionEnd, so they'd linger
    # forever showing their last state (often a red WAITING). Drop any whose
    # terminal no longer has a live claude. Skip pruning if we can't read the
    # process list, or a record has no captured tty.
    live = live_ttys()
    out = []
    for fp in glob.glob(os.path.join(STATE_DIR, "*.json")):
        try:
            with open(fp) as f:
                r = json.load(f)
        except Exception:
            continue  # mid-write or garbage; skip this tick
        tty = (r.get("term", {}).get("tty") or "").replace("/dev/", "")
        if live and tty and tty not in live:
            try:
                os.remove(fp)  # its terminal is gone; reap it
            except OSError:
                pass
            continue
        out.append(r)
    out.sort(key=lambda r: (ORDER.get(r.get("state"), 3), -r.get("updated", 0)))
    return out


def fmt_age(secs):
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60:02d}s"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


def clear_stale():
    now = time.time()
    for fp in glob.glob(os.path.join(STATE_DIR, "*.json")):
        try:
            with open(fp) as f:
                r = json.load(f)
            if now - r.get("updated", 0) > STALE_AFTER:
                os.remove(fp)
        except Exception:
            pass


def _run(cmd):
    """Run a command silently; return True on exit 0."""
    try:
        return subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except Exception:
        return False


def _osascript(script):
    return _run(["osascript", "-e", script])


def focus_vscode_window(root, project):
    """Focus the VS Code window that has `root` open, via the `code` CLI.

    `code <folder>` brings an already-open window for that folder to the front
    (and opens one if none exists) — no Accessibility permission needed. We pass
    the project root rather than the raw cwd so a session started in a deep
    subdir still resolves to the window the user actually has open.
    """
    if not root:
        return "no folder recorded for this session (restart it)"
    code = shutil.which("code") or "/usr/local/bin/code"
    if not os.path.exists(code):
        return "`code` CLI not found (install it from VS Code: Shell Command)"
    if _run([code, root]):
        return "focused VS Code: %s" % (project or root)
    return "`code` CLI failed to run"


def focus_session(s):
    """Bring the terminal window/pane running session `s` to the front.

    Returns a short status string for the footer. Picks the best mechanism
    available from what the hook captured: tmux pane > iTerm session id >
    Terminal.app tty > VS Code window (`code` CLI) > plain app activation.
    """
    term = s.get("term") or {}
    prog = term.get("term_program", "")
    pane = term.get("tmux_pane", "")
    iterm = term.get("iterm_session_id", "")
    tty = term.get("tty", "")
    app = APP_NAME.get(prog)

    # 1) tmux: select the window+pane, then raise whatever app hosts tmux.
    if pane:
        ok = _run(["tmux", "select-window", "-t", pane])
        _run(["tmux", "select-pane", "-t", pane])
        if app:
            _osascript('tell application "%s" to activate' % app)
        return "focused tmux pane %s" % pane if ok else "tmux server not reachable"

    # 2) iTerm2: match the session by its unique id (suffix of ITERM_SESSION_ID).
    if prog == "iTerm.app" and iterm:
        guid = iterm.split(":", 1)[-1]
        script = (
            'tell application "iTerm"\n'
            '  activate\n'
            '  repeat with w in windows\n'
            '    repeat with t in tabs of w\n'
            '      repeat with se in sessions of t\n'
            '        if id of se is "%s" then\n'
            '          select w\n'
            '          tell t to select\n'
            '          select se\n'
            '          return\n'
            '        end if\n'
            '      end repeat\n'
            '    end repeat\n'
            '  end repeat\n'
            'end tell' % guid)
        return "focused iTerm session" if _osascript(script) else "iTerm session not found"

    # 3) Terminal.app: match the tab by its tty device.
    if prog == "Apple_Terminal" and tty:
        script = (
            'tell application "Terminal"\n'
            '  activate\n'
            '  repeat with w in windows\n'
            '    repeat with t in tabs of w\n'
            '      if tty of t is "%s" then\n'
            '        set selected of t to true\n'
            '        set frontmost of w to true\n'
            '        return\n'
            '      end if\n'
            '    end repeat\n'
            '  end repeat\n'
            'end tell' % tty)
        return "focused Terminal tab (%s)" % tty if _osascript(script) else "Terminal tab not found"

    # 4) VS Code: one window per session — focus it via the `code` CLI.
    if prog == "vscode":
        root = term.get("root") or s.get("cwd") or ""
        return focus_vscode_window(root, s.get("project") or "")

    # 5) Fallback: raise the app; we can't pinpoint the exact pane.
    if app:
        _osascript('tell application "%s" to activate' % app)
        return "raised %s; can't target exact pane%s" % (
            app, (" (tty %s)" % tty) if tty else "")

    return "no window info for this session (restart it to capture)"


def draw(stdscr, colors, sessions, selected_sid, status_msg):
    now = time.time()
    h, w = stdscr.getmaxyx()
    stdscr.erase()

    counts = {"waiting": 0, "working": 0, "idle": 0}
    for s in sessions:
        st = s.get("state", "idle")
        counts["idle" if st == "done" else st] = counts.get("idle" if st == "done" else st, 0) + 1

    title = "  Claude Code Monitor"
    summary = (f"{len(sessions)} running   "
               f"{counts['waiting']} waiting   "
               f"{counts['working']} working   "
               f"{counts['idle']} idle")
    stdscr.addnstr(0, 0, title.ljust(w), w - 1, curses.A_BOLD)
    stdscr.addnstr(1, 0, "  " + summary, w - 1, curses.A_DIM)

    # Column header
    hdr = f"  {'STATE':<8} {'PROJECT':<22} {'DETAIL':<26} {'AGE':>7}  CWD"
    stdscr.addnstr(3, 0, hdr.ljust(w), w - 1, curses.A_UNDERLINE)

    row = 4
    for s in sessions:
        if row >= h - 1:
            break
        st = s.get("state", "idle")
        label = LABEL.get(st, st.upper())
        attr = colors.get(st, 0)
        if st == "waiting":
            attr |= curses.A_BOLD
        selected = s.get("session_id") == selected_sid
        if selected:
            attr |= curses.A_REVERSE
        marker = "▶ " if selected else "  "
        proj = (s.get("project") or "?")[:22]
        detail = (s.get("detail") or "")[:26]
        age = fmt_age(now - s.get("updated", now))
        cwd = s.get("cwd") or ""
        line = f"{marker}{label:<8} {proj:<22} {detail:<26} {age:>7}  {cwd}"
        try:
            stdscr.addnstr(row, 0, line.ljust(w), w - 1, attr)
        except curses.error:
            pass
        row += 1

    if not sessions:
        stdscr.addnstr(5, 2, "No Claude Code sessions reporting yet.", w - 3, curses.A_DIM)
        stdscr.addnstr(6, 2, "Start a session in any project (hooks must be installed).", w - 3, curses.A_DIM)

    if status_msg:
        stdscr.addnstr(h - 2, 0, ("  " + status_msg).ljust(w), w - 1, curses.A_DIM)
    foot = "  ↑/↓ select   ⏎ focus window   c clear stale   q quit"
    stdscr.addnstr(h - 1, 0, foot.ljust(w), w - 1, curses.A_BOLD)
    stdscr.refresh()


def main(stdscr):
    curses.curs_set(0)
    stdscr.keypad(True)  # decode arrow keys to curses.KEY_* codes
    stdscr.timeout(int(REFRESH * 1000))
    colors = {}
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_RED, -1)
        curses.init_pair(2, curses.COLOR_YELLOW, -1)
        curses.init_pair(3, curses.COLOR_GREEN, -1)
        colors = {
            "waiting": curses.color_pair(1),
            "working": curses.color_pair(2),
            "idle": curses.color_pair(3),
            "done": curses.color_pair(3),
        }

    selected_sid = None  # track selection by session id so sort churn is harmless
    status_msg = ""
    while True:
        sessions = load_sessions()
        ids = [s.get("session_id") for s in sessions]
        if selected_sid not in ids:
            selected_sid = ids[0] if ids else None

        draw(stdscr, colors, sessions, selected_sid, status_msg)
        try:
            ch = stdscr.getch()
        except KeyboardInterrupt:
            break

        if ch == -1:
            continue  # refresh tick, no key pressed
        if ch in (ord("q"), ord("Q")):
            break
        if ch in (ord("c"), ord("C")):
            clear_stale()
            continue

        idx = ids.index(selected_sid) if selected_sid in ids else 0
        if ch in (curses.KEY_DOWN, ord("j")) and ids:
            selected_sid = ids[min(idx + 1, len(ids) - 1)]
            status_msg = ""
        elif ch in (curses.KEY_UP, ord("k")) and ids:
            selected_sid = ids[max(idx - 1, 0)]
            status_msg = ""
        elif ch in (curses.KEY_ENTER, 10, 13) and selected_sid in ids:
            status_msg = focus_session(sessions[idx])


if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
