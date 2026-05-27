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
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(REPO, "hook.py")
MONITOR = os.path.join(REPO, "monitor.py")
SETTINGS = os.path.expanduser("~/.claude/settings.json")

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


def install_command():
    bin_dir = pick_bin_dir()
    target = os.path.join(bin_dir, "adhd")
    with open(target, "w") as f:
        f.write("#!/bin/sh\nexec %s %s \"$@\"\n"
                % (shlex.quote(sys.executable), shlex.quote(MONITOR)))
    os.chmod(target, 0o755)
    on_path = bin_dir in os.environ.get("PATH", "").split(os.pathsep)
    return target, bin_dir, on_path


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
    target, bin_dir, on_path = install_command()
    install_hooks()

    print("adhd installed.")
    print("  command : %s" % target)
    print("  hooks   : wired in %s" % SETTINGS)
    print("  monitor : %s" % MONITOR)
    if on_path:
        print("\nRun it any time:  adhd")
    else:
        rc = "~/.zshrc" if os.environ.get("SHELL", "").endswith("zsh") else "~/.bashrc"
        print("\n%s is not on your PATH. Add it once:" % bin_dir)
        print('  echo \'export PATH="%s:$PATH"\' >> %s' % (bin_dir, rc))
        print("  source %s" % rc)
        print("\nThen run:  adhd")
    print("\nAlready-open Claude sessions will report after their next event "
          "(or restart them).")


if __name__ == "__main__":
    main()
