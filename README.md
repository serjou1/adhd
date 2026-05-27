# adhd

A live terminal dashboard that shows every running Claude Code instance and its
state, so you don't have to switch between projects to find the one stuck on a
`yes / don't ask again` permission prompt. Select a session and hit Enter to jump
straight to its window.

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
| `install.py`              | One-shot installer: adds the `adhd` command and wires the hooks. |
| `hook.py`                 | Event handler; writes per-session state. Always exits 0 so it can't break a session. |
| `monitor.py`              | The dashboard. |
| `~/.adhd/state/`          | One JSON file per live session (override with `ADHD_STATE_DIR`). |
| `~/.claude/settings.json` | Holds the global `hooks` block that wires the events to `hook.py`. |

## Notes & troubleshooting

- **A session isn't showing up?** Sessions already open before the hooks were
  installed may not report. Restart that `claude` and it will appear. New
  sessions are picked up automatically.
- **A permission prompt didn't turn a row red?** The "waiting" detection relies
  on the `Notification` hook. If a prompt isn't classified as waiting, adjust the
  keyword matching in the `classify()` function in `hook.py`.
- **A crashed/force-closed session lingers.** It never sent `SessionEnd`, so its
  file stays until you press `c` (clears entries with no update for 6h). Tune
  `STALE_AFTER` in `monitor.py` to change that window.
- **"No sessions reporting yet."** Normal when no Claude Code instances are
  running, or none have fired an event since install.
