#!/usr/bin/env python3
"""Installer for adhd — the Claude Code multi-instance monitor.

Does two things, both idempotent (safe to re-run):

1. Drops an `adhd` command on your PATH that launches the dashboard.
2. Wires Claude Code's lifecycle hooks (in ~/.claude/settings.json) to this
   repo's hook.py, so every session reports its state.

Run it from wherever you cloned the repo:

    python3 install.py
"""
import json
import os
import shlex
import subprocess
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(REPO, "hook.py")
MONITOR = os.path.join(REPO, "monitor.py")
MENUBAR = os.path.join(REPO, "menubar.py")
SETTINGS = os.path.expanduser("~/.claude/settings.json")
LAUNCH_AGENT = os.path.expanduser(
    "~/Library/LaunchAgents/com.adhd.menubar.plist")
# launchd doesn't expand ~, so the plist gets this absolute path baked in. The
# menu-bar app's stdout/stderr land here — so if it ever crashes or vanishes,
# there's a trail instead of silence.
LOG = os.path.expanduser("~/Library/Logs/adhd-menubar.log")

# Events we hook and whether they take a "*" matcher.
EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
          "Notification", "Stop", "SessionEnd"]
WILDCARD = {"PreToolUse", "PostToolUse"}


def pick_bin_dir():
    """First writable dir on (or destined for) PATH; create ~/.local/bin if needed."""
    home = os.path.expanduser("~")
    for d in ("/opt/homebrew/bin", "/usr/local/bin", os.path.join(home, ".local", "bin")):
        if os.path.isdir(d) and os.access(d, os.W_OK):
            return d
    fallback = os.path.join(home, ".local", "bin")
    os.makedirs(fallback, exist_ok=True)
    return fallback


def _write_launcher(bin_dir, name, script):
    """Drop a tiny shell launcher `name` on PATH that runs `script`."""
    target = os.path.join(bin_dir, name)
    with open(target, "w") as f:
        f.write("#!/bin/sh\nexec %s %s \"$@\"\n"
                % (shlex.quote(sys.executable), shlex.quote(script)))
    os.chmod(target, 0o755)
    return target


def install_command():
    bin_dir = pick_bin_dir()
    target = _write_launcher(bin_dir, "adhd", MONITOR)
    menu = _write_launcher(bin_dir, "adhd-menu", MENUBAR)
    on_path = bin_dir in os.environ.get("PATH", "").split(os.pathsep)
    return target, menu, bin_dir, on_path


def ensure_rumps():
    """Best-effort: make sure the menu-bar app's one dependency is importable."""
    try:
        import rumps  # noqa: F401
        return True
    except ImportError:
        pass
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "--user", "rumps"],
                       check=True)
        return True
    except Exception:
        return False


def install_login_item():
    """Auto-start adhd-menu at login via a LaunchAgent."""
    os.makedirs(os.path.dirname(LAUNCH_AGENT), exist_ok=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    plist = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        '  <key>Label</key><string>com.adhd.menubar</string>\n'
        '  <key>ProgramArguments</key><array>\n'
        '    <string>%s</string><string>%s</string>\n'
        '  </array>\n'
        '  <key>RunAtLoad</key><true/>\n'
        '  <key>KeepAlive</key><true/>\n'
        '  <key>StandardOutPath</key><string>%s</string>\n'
        '  <key>StandardErrorPath</key><string>%s</string>\n'
        '</dict></plist>\n' % (sys.executable, MENUBAR, LOG, LOG))
    with open(LAUNCH_AGENT, "w") as f:
        f.write(plist)
    subprocess.run(["launchctl", "unload", LAUNCH_AGENT],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["launchctl", "load", LAUNCH_AGENT],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return LAUNCH_AGENT


def _is_ours(group):
    """True if a hook group belongs to a prior adhd/cc-monitor install."""
    for h in group.get("hooks", []):
        c = h.get("command", "")
        if c.endswith("hook.py") and (HOOK in c or "cc-monitor" in c or "/adhd/" in c):
            return True
    return False


def install_hooks():
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    cfg = {}
    if os.path.exists(SETTINGS):
        with open(SETTINGS) as f:
            cfg = json.load(f)

    hooks = cfg.setdefault("hooks", {})
    cmd = "python3 %s" % shlex.quote(HOOK)
    for ev in EVENTS:
        # Keep the user's unrelated hooks; replace only our own.
        groups = [g for g in hooks.get(ev, []) if not _is_ours(g)]
        entry = {"hooks": [{"type": "command", "command": cmd, "timeout": 5}]}
        if ev in WILDCARD:
            entry["matcher"] = "*"
        groups.append(entry)
        hooks[ev] = groups

    with open(SETTINGS, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


def main():
    want_login = "--login" in sys.argv[1:]
    target, menu, bin_dir, on_path = install_command()
    install_hooks()
    have_rumps = ensure_rumps()

    print("adhd installed.")
    print("  command   : %s" % target)
    print("  menu bar  : %s" % menu)
    print("  hooks     : wired in %s" % SETTINGS)
    print("  monitor   : %s" % MONITOR)
    print("  rumps     : %s" % ("ready" if have_rumps
                                else "MISSING — run: pip3 install --user rumps"))

    if want_login:
        plist = install_login_item()
        print("  login item: %s (adhd-menu starts at login)" % plist)

    if on_path:
        print("\nRun the dashboard:  adhd")
        print("Run the menu bar :  adhd-menu" + ("" if want_login else
              "   (or re-run with --login to auto-start it)"))
    else:
        rc = "~/.zshrc" if os.environ.get("SHELL", "").endswith("zsh") else "~/.bashrc"
        print("\n%s is not on your PATH. Add it once:" % bin_dir)
        print('  echo \'export PATH="%s:$PATH"\' >> %s' % (bin_dir, rc))
        print("  source %s" % rc)
        print("\nThen run:  adhd   (dashboard)   or   adhd-menu   (menu bar)")
    print("\nAlready-open Claude sessions will report after their next event "
          "(or restart them).")


if __name__ == "__main__":
    main()
