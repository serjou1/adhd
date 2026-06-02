# adhd

A live terminal dashboard that shows every running Claude Code instance and its
state, so you don't have to switch between projects to find the one stuck on a
`yes / don't ask again` permission prompt. Select a session and hit Enter to jump
straight to its window.

![The adhd dashboard listing running Claude Code sessions by state](docs/monitor.png)

## Install

```bash
git clone https://github.com/serjou1/adhd.git ~/.claude/adhd
python3 ~/.claude/adhd/install.py
```

The installer drops an `adhd` command on your PATH and wires the Claude Code
hooks. (The repo is private — `git clone` uses your existing GitHub credentials.)

## Quick start

```bash
adhd
```

That's it. It refreshes every second and picks up every Claude Code session
automatically. No setup per project. Runtime state lives in `~/.adhd/state`
(override with `ADHD_STATE_DIR`).

## Menu bar (macOS)

Don't want a whole terminal pane? `adhd-menu` puts a status-bar icon up top with
a badge counting the sessions **WAITING** on a permission prompt — the ones that
literally need your click. `◧ 2` means two sessions are blocked.

![The menu-bar icon with a badge showing one waiting session](docs/menubar-badge.png)

```bash
adhd-menu                     # run it now
python3 install.py --login    # ...or auto-start it at login
```

Click the icon for a live menu:

```
  1 waiting · 2 working · 4 idle
  ───────────────────────────────
  🔴  arbitrage-core — Claude needs your permission (12s)
  🟡  statistics-service — ran Bash (1s)
  🟢  twitter-crawler — waiting for input (47s)
  ───────────────────────────────
  Open adhd monitor…
  ───────────────────────────────
  Quit
```

Pick any session to bring its window to the front (same focus logic as the
dashboard — tmux pane, iTerm/Terminal tab, or VS Code window). **Open adhd
monitor…** launches the full curses dashboard in a new Terminal window. The
badge hides when nothing is waiting. Override the glyph with `ADHD_MENU_ICON`.

### Notifications

While `adhd-menu` is running it pops a macOS notification when a session needs
you, so you can work in one window and get pulled back to another only when it
matters:

| When | Notification |
|------|--------------|
| A session **finishes its turn** (working → idle) | ✅ *project* — done |
| A session **blocks on a permission prompt** (needs access) | 🔴 *project* needs you |

![A macOS notification reading "adhd needs you — claude-code · main — waiting for approval"](docs/notification.png)

Toggle notifications from the menu (**Notifications**), or start muted with
`ADHD_NOTIFY=0`. No burst on startup — already-running sessions are seeded
silently and only *transitions* after that fire a toast.

Toasts aren't clickable — to jump to a session, click the menu-bar icon and
pick it. (The toast just tells you *which* one needs you.)

> **Why not clickable toasts?** A clickable notification needs macOS's modern
> `UNUserNotificationCenter` API, which only authorizes a signed `.app` bundle.
> The usual CLI tools (`terminal-notifier`, `alerter`) are stuck on the legacy
> `NSUserNotification` API that **Apple removed in recent macOS** — their toasts
> silently never appear, and no permission toggle fixes it. So `adhd` uses
> `osascript`, which always delivers.

> Needs the `rumps` package (a tiny PyObjC wrapper). `install.py` installs it for
> you; otherwise `pip3 install --user rumps`.

## What you see

```
  Claude Code Monitor
  4 running   1 waiting   2 working   1 idle
  STATE    PROJECT                DETAIL                       AGE  CWD
  WAITING  upbit-crawler          needs permission to use...   12s  /Users/serjou/upbit-crawler
  WORKING  arbitrage-core         ran Bash                      3s  /Users/serjou/arbitrage-core
  WORKING  statistics-service     Bash                          1s  /Users/serjou/statistics-service
  IDLE     twitter-crawler        waiting for input           47s  /Users/serjou/twitter-crawler
```

| State              | Meaning                                                        | What to do        |
|--------------------|----------------------------------------------------------------|-------------------|
| **WAITING** (red)  | Blocked on a permission / approval prompt — needs your click   | Go to that one    |
| **WORKING** (yellow) | Actively processing a prompt or running a tool               | Leave it alone    |
| **IDLE** (green)   | Finished its turn, sitting at the prompt for your next message | Free when you are |

Rows are sorted so WAITING is always at the top.

## Keys

| Key | Action |
|-----|--------|
| `↑` / `↓` (or `k` / `j`) | Move the selection (highlighted row) |
| `⏎` Enter | Jump to / focus the terminal window running the selected session |
| `c` | Clear stale entries (sessions older than 6h that never sent a clean exit) |
| `q` | Quit the dashboard |

### Jumping to a session's window

Select a row and press Enter to bring its terminal to the front. The hook records
how each session was launched and the dashboard picks the best focus method:

| Terminal | How it focuses | Precision |
|----------|----------------|-----------|
| **tmux** | `tmux select-window` + `select-pane` on the recorded pane | exact pane |
| **iTerm2** | AppleScript match on `ITERM_SESSION_ID` | exact tab/session |
| **Terminal.app** | AppleScript match on the tab's `tty` | exact tab |
| **VS Code** (one window per session) | `code <project-root>` focuses the window that has that folder open | exact window |
| other | activates the app (can't target the exact pane) | app only |

The result of each jump is shown just above the footer. Sessions that were
already running before this feature was installed have no captured window info —
restart them (or just let them fire one more event) and they'll become jumpable.

> **VS Code jumping uses the `code` CLI** — no Accessibility permission needed.
> `code <folder>` brings an already-open window for that folder to the front.
> The target folder is the session's **git root** (so a session started in a
> deep subdir like `crates/foo/src` still resolves to the `arbitrage-core`
> window), falling back to the cwd for non-git folders. This is also why the
> `PROJECT` column shows the repo name instead of the subdir name. Requires the
> `code` command on your PATH — in VS Code run *Shell Command: Install 'code'
> command in PATH* if it's missing.

## How it works

1. Claude Code fires **hooks** on lifecycle events (prompt submitted, tool about
   to run, permission notification, turn finished, session ended).
2. `hook.py` runs on each event and writes a small JSON state file to
   `~/.adhd/state/<session_id>.json`.
3. `monitor.py` polls that folder once a second and renders the table.

Event → state mapping (in `hook.py`):

| Hook event        | State it sets        |
|-------------------|----------------------|
| `SessionStart`    | idle                 |
| `UserPromptSubmit`| working              |
| `PreToolUse`      | working (tool name)  |
| `PostToolUse`     | working              |
| `Notification`    | **waiting** (permission) or idle (idle prompt) |
| `Stop`            | idle (done)          |
| `SessionEnd`      | removed from dashboard |

## Files

| Path | Role |
|------|------|
| `install.py`              | One-shot installer: adds the `adhd` / `adhd-menu` commands, wires the hooks, installs `rumps` (`--login` adds a LaunchAgent). |
| `hook.py`                 | Event handler; writes per-session state. Always exits 0 so it can't break a session. |
| `monitor.py`              | The terminal dashboard (also the shared session-loading / window-focus layer). |
| `menubar.py`              | The macOS menu-bar app. Reuses `monitor.py`'s loading + focus logic. |
| `~/.adhd/state/`          | One JSON file per live session (override with `ADHD_STATE_DIR`). |
| `~/.claude/settings.json` | Holds the global `hooks` block that wires the events to `hook.py`. |

## Notes & troubleshooting

- **A session isn't showing up?** Sessions already open before the hooks were
  installed may not report. Restart that `claude` and it will appear. New
  sessions are picked up automatically.
- **A permission prompt didn't turn a row red?** The "waiting" detection relies
  on the `Notification` hook. If a prompt isn't classified as waiting, adjust the
  keyword matching in the `classify()` function in `hook.py`.
- **A crashed/force-closed session lingers.** It never sends `SessionEnd`, but
  the monitor auto-reaps it: each refresh it drops any session whose terminal
  (`tty`) no longer has a live `claude` process. Sessions whose tty wasn't
  captured (or if the process list can't be read) are kept, and `c` still clears
  anything with no update for 6h as a backstop.
- **"No sessions reporting yet."** Normal when no Claude Code instances are
  running, or none have fired an event since install.
