#!/usr/bin/env python3
"""adhd menu-bar app: a macOS status-bar icon for the Claude Code monitor.

Lives in the menu bar and shows a badge with the number of sessions WAITING on
a permission/approval prompt — the ones that literally need your click. Open the
menu to jump straight to any session's window, or launch the full curses
dashboard (monitor.py) in a terminal.

It reads the same per-session state files as the dashboard and reuses its
session-loading and window-focus logic, so the two always agree.

Run it:   adhd-menu      (after install.py)
   or:    python3 menubar.py

Requires `rumps` (a small PyObjC wrapper):  pip3 install rumps
"""
import os
import shutil
import subprocess
import sys
import time

try:
    import rumps
except ImportError:
    sys.stderr.write(
        "adhd-menu needs the 'rumps' package:\n"
        "    pip3 install rumps\n"
        "(install.py installs it for you.)\n")
    sys.exit(1)

# Reuse the dashboard's data layer: loading + reaping + window focus all live in
# monitor.py. Importing it has no side effects (curses only runs under its own
# __main__), so the menu bar and the terminal dashboard never drift apart.
from monitor import load_sessions, focus_session, fmt_age  # noqa: E402

MONITOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor.py")
REFRESH = 1.0  # seconds between badge/menu refreshes
# Menu-bar glyph shown next to the badge. Override with ADHD_MENU_ICON.
ICON = os.environ.get("ADHD_MENU_ICON", "◧")
# Notifications on by default; set ADHD_NOTIFY=0 to start with them muted.
NOTIFY_DEFAULT = os.environ.get("ADHD_NOTIFY", "1") != "0"
# Colored dot per state, used in the per-session menu rows.
DOT = {"waiting": "🔴", "working": "🟡", "idle": "🟢", "done": "🟢"}
LABEL = {"waiting": "waiting", "working": "working", "idle": "idle", "done": "idle"}


def _osa_escape(s):
    """Escape a Python string for embedding inside an AppleScript "..." literal."""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def notify(title, message, sound="Glass"):
    """Post a macOS notification via osascript's `display notification`.

    Works on every Mac with no setup — it always gets delivered (it shows the
    Script Editor icon and isn't clickable, but that's the only path that still
    reliably delivers on current macOS; the legacy `NSUserNotification`-based
    CLI tools like terminal-notifier silently no-op on macOS 14+).
    """
    script = 'display notification "%s" with title "%s" sound name "%s"' % (
        _osa_escape(message), _osa_escape(title), sound)
    subprocess.run(["osascript", "-e", script],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def open_monitor():
    """Launch the curses dashboard in a new Terminal window.

    Prefer the installed `adhd` command (on PATH, so its shell sources the
    profile); fall back to running monitor.py directly with this interpreter.
    """
    cmd = shutil.which("adhd") or "%s %s" % (sys.executable, MONITOR)
    # `do script` opens a new window and runs the command in it.
    script = (
        'tell application "Terminal"\n'
        '  activate\n'
        '  do script "exec %s"\n'
        'end tell' % cmd.replace('"', '\\"'))
    subprocess.run(["osascript", "-e", script],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class AdhdMenuApp(rumps.App):
    def __init__(self):
        # quit_button=None: we render our own Quit so it survives menu rebuilds.
        super().__init__("adhd", title=ICON, quit_button=None)
        self.notify_enabled = NOTIFY_DEFAULT
        # Last-seen state per session id, so we can spot transitions worth
        # announcing (turn finished, or newly blocked on a prompt).
        self.prev_state = {}
        self.timer = rumps.Timer(self.refresh, REFRESH)
        self.timer.start()
        self.refresh(None)

    def refresh(self, _):
        sessions = load_sessions()
        self._maybe_notify(sessions)
        counts = {"waiting": 0, "working": 0, "idle": 0}
        for s in sessions:
            st = s.get("state", "idle")
            st = "idle" if st == "done" else st
            counts[st] = counts.get(st, 0) + 1

        waiting = counts["waiting"]
        # Badge: glyph alone when nothing needs you, glyph + count otherwise.
        self.title = "%s %d" % (ICON, waiting) if waiting else ICON
        self.menu.clear()
        self.menu.update(self._build_menu(sessions, counts))

    def _maybe_notify(self, sessions):
        """Fire a notification when a session finishes a turn or blocks on a prompt.

        Compares each session's current state to what we saw last tick. We seed
        prev_state silently on first sight so we never fire a burst of stale
        notifications on startup. Reaped sessions are dropped from the map.
        """
        seen = set()
        for s in sessions:
            sid = s.get("session_id")
            seen.add(sid)
            st = "idle" if s.get("state") == "done" else s.get("state", "idle")
            prev = self.prev_state.get(sid)
            self.prev_state[sid] = st
            if prev is None or prev == st or not self.notify_enabled:
                continue
            proj = s.get("project") or "?"
            detail = s.get("detail") or ""
            if st == "waiting" and prev != "waiting":
                notify("🔴 %s needs you" % proj, detail or "waiting for approval",
                       sound="Funk")
            elif st == "idle" and prev == "working":
                notify("✅ %s — done" % proj, detail or "turn finished")
        # Forget sessions that have gone away so a restart re-seeds cleanly.
        for sid in list(self.prev_state):
            if sid not in seen:
                del self.prev_state[sid]

    def _build_menu(self, sessions, counts):
        items = []

        summary = "%d waiting · %d working · %d idle" % (
            counts["waiting"], counts["working"], counts["idle"])
        header = rumps.MenuItem(summary)
        header.set_callback(None)  # disabled (grayed, non-clickable)
        items.append(header)
        items.append(rumps.separator)

        if sessions:
            now = time.time()
            for s in sessions:
                st = s.get("state", "idle")
                dot = DOT.get(st, "⚪️")
                proj = s.get("project") or "?"
                detail = s.get("detail") or LABEL.get(st, st)
                age = fmt_age(now - s.get("updated", now))
                title = "%s  %s — %s (%s)" % (dot, proj, detail, age)
                item = rumps.MenuItem(title, callback=self._make_focus(s))
                items.append(item)
        else:
            none = rumps.MenuItem("No Claude Code sessions reporting")
            none.set_callback(None)
            items.append(none)

        items.append(rumps.separator)
        items.append(rumps.MenuItem(
            "Open adhd monitor…", callback=lambda _: open_monitor()))
        toggle = rumps.MenuItem("Notifications", callback=self._toggle_notify)
        toggle.state = 1 if self.notify_enabled else 0
        items.append(toggle)
        items.append(rumps.separator)
        items.append(rumps.MenuItem("Quit", callback=rumps.quit_application))
        return items

    def _toggle_notify(self, _):
        self.notify_enabled = not self.notify_enabled

    def _make_focus(self, session):
        """Closure so each session row focuses its own window when clicked."""
        def cb(_):
            focus_session(session)
        return cb


def main():
    AdhdMenuApp().run()


if __name__ == "__main__":
    main()
