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
import shlex
import shutil
import subprocess
import sys
import tempfile
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

HERE = os.path.dirname(os.path.abspath(__file__))
MONITOR = os.path.join(HERE, "monitor.py")
# Our own AppleScript applet that owns the notifications (built on first use).
# Living in ~/.adhd keeps the repo free of a binary .app bundle and lets it
# self-heal if deleted. It's what makes a banner click open adhd, not Script
# Editor — see ensure_notifier_app().
ADHD_HOME = os.path.join(os.path.expanduser("~"), ".adhd")
NOTIFIER_APP = os.path.join(ADHD_HOME, "adhd.app")
PENDING = os.path.join(ADHD_HOME, "pending_notify")  # one queued toast payload
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


# The applet that owns our banners. On launch it does one of two things:
#   - if a payload is queued (we're posting), it shows that banner, then exits;
#   - otherwise (the user clicked a banner, which relaunches us) it focuses a
#     waiting session via `menubar.py --focus`.
# A macOS notification is owned by whatever app issues `display notification`.
# Routing it through this bundle — instead of a bare `osascript`, which is owned
# by Script Editor — is the whole trick: clicking a banner now opens adhd.
# Fields are joined by US (\x1f) so titles/details can contain anything printable.
APPLET_SRC = r'''on doWork()
	set payload to ""
	try
		set payload to do shell script "f=\"$HOME/.adhd/pending_notify\"; if [ -f \"$f\" ]; then cat \"$f\"; rm -f \"$f\"; fi"
	end try
	if payload is not "" then
		set AppleScript's text item delimiters to (character id 31)
		set parts to text items of payload
		set AppleScript's text item delimiters to ""
		if (count of parts) is greater than or equal to 3 then
			display notification (item 2 of parts) with title (item 1 of parts) sound name (item 3 of parts)
		else
			display notification payload with title "adhd"
		end if
	else
		do shell script "__FOCUS_CMD__"
	end if
end doWork

on run
	doWork()
end run

on reopen
	doWork()
end reopen
'''


def ensure_notifier_app():
    """Build ~/.adhd/adhd.app once and return True if it's available.

    osacompile turns the AppleScript above into a real .app bundle. Because that
    bundle is the process that calls `display notification`, the banner is owned
    by it — so clicking opens adhd (which focuses a waiting session) instead of
    Script Editor. We rebrand it (stable bundle id, agent app, ad-hoc signature)
    so macOS attributes and relaunches it cleanly.
    """
    if os.path.isdir(NOTIFIER_APP):
        return True
    if not shutil.which("osacompile"):
        return False
    os.makedirs(ADHD_HOME, exist_ok=True)
    focus_cmd = "%s %s --focus >/dev/null 2>&1 &" % (
        shlex.quote(sys.executable), shlex.quote(os.path.abspath(__file__)))
    src = APPLET_SRC.replace("__FOCUS_CMD__", _osa_escape(focus_cmd))
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", suffix=".applescript", delete=False) as f:
            f.write(src)
            tmp = f.name
        ok = subprocess.run(
            ["osacompile", "-o", NOTIFIER_APP, tmp],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except Exception:
        ok = False
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
    if not ok:
        return False
    _brand_notifier_app()
    return True


def _brand_notifier_app():
    """Give the applet a stable identity so macOS treats it as 'adhd'.

    A unique bundle id keeps banners from being grouped under Script Editor; the
    agent flag (LSUIElement) keeps it out of the Dock when it launches to post or
    focus; the ad-hoc signature satisfies recent macOS notification gating.
    """
    plist = os.path.join(NOTIFIER_APP, "Contents", "Info.plist")
    pb = "/usr/libexec/PlistBuddy"
    # osacompile applets ship without a CFBundleIdentifier, so `Add` it (Set only
    # edits existing keys). Running Add then Set is robust whether or not the key
    # is already there.
    for cmd in ("Add :CFBundleIdentifier string com.adhd.notifier",
                "Set :CFBundleIdentifier com.adhd.notifier",
                "Set :CFBundleName adhd",
                "Add :LSUIElement bool true"):
        subprocess.run([pb, "-c", cmd, plist],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Re-sign ad-hoc *after* editing Info.plist, or the signature is invalidated.
    subprocess.run(["codesign", "--force", "--sign", "-", NOTIFIER_APP],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def notify(title, message, sound="Glass"):
    """Show a macOS banner that, when clicked, opens adhd (not Script Editor).

    Queues the payload, then launches our applet (hidden, in the background) to
    post it. If the applet can't be built we fall back to a bare `osascript`
    toast — it still delivers; its click just opens Script Editor as before.
    """
    if not ensure_notifier_app():
        subprocess.run(
            ["osascript", "-e",
             'display notification "%s" with title "%s" sound name "%s"' % (
                 _osa_escape(message), _osa_escape(title), sound)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    try:
        with open(PENDING, "w") as f:
            f.write("\x1f".join((title, message, sound)))
    except OSError:
        return
    # -g: don't steal focus, -j: launch hidden. A fresh launch each time (the
    # applet exits after posting), so `on run` re-reads the queued payload.
    subprocess.run(["open", "-gj", NOTIFIER_APP],
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


def focus_waiting():
    """Focus the session most in need of attention, then exit.

    Invoked as `menubar.py --focus` by adhd.app when a notification banner is
    clicked. load_sessions() already sorts waiting-first, then most-recent, so
    the first row is the right target — the same one the banner was about in the
    common single-prompt case.
    """
    sessions = load_sessions()
    if sessions:
        focus_session(sessions[0])


def main():
    if "--focus" in sys.argv[1:]:
        focus_waiting()
        return
    AdhdMenuApp().run()


if __name__ == "__main__":
    main()
