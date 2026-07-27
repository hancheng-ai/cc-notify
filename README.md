# claude-code-notify

Session-aware, clickable macOS notifications for [Claude Code](https://claude.com/claude-code).

Run several Claude Code sessions at once and the stock notifications stop being
useful. Every banner looks the same, so you can't tell which session is asking
for you, and clicking one does nothing. You end up polling your sessions by hand
— which defeats the point of running them in parallel.

This hook makes each notification say **which session** wants you, and makes
clicking it **jump straight back to that session**.

```
┌──────────────────────────────────────────────┐
│ ◆  Refactor auth middleware                  │   ← session name, as in the sidebar
│    my-api · plan                             │   ← repo · permission mode
│    Claude needs your permission to use Bash  │
└──────────────────────────────────────────────┘
        click  →  jumps to that session
```

## What you get

| | |
|---|---|
| **Session name** | Read from the transcript — the same name shown in your sidebar |
| **Repo + mode** | Which project, and the permission mode when it isn't `default` |
| **Click to jump** | Opens that exact session in the Claude desktop app |
| **Trigger icon** | Claude's icon when Claude launched it, Terminal's when a terminal did |
| **One banner per session** | New notifications replace that session's previous one instead of piling up |

## Requirements

- macOS
- Claude Code
- [terminal-notifier](https://github.com/julienXX/terminal-notifier) — optional, but
  required for click-to-jump and grouping:
  ```bash
  brew install terminal-notifier
  ```
  Without it the hook degrades to a plain `osascript` banner. You still get the
  session name; you lose the click.

No Python packages. Standard library only.

## Install

```bash
git clone https://github.com/hancheng-ai/claude-code-notify.git
cd claude-code-notify
python3 install.py
```

The installer copies the hook to `~/.claude/hooks/notify.py` and registers it in
`~/.claude/settings.json`. It **merges** into your existing configuration rather
than overwriting it, and backs the file up first. Use `--dry-run` to preview.

Restart Claude Code, then check it end to end:

```bash
python3 ~/.claude/hooks/notify.py --self-test
```

That prints what it resolved — session, title, deep link, icon, notifier — and
sends a real notification for your most recent session. Click it; you should
land in that session.

## How it works

Three things here are not obvious, and are the reason this isn't a five-line script.

**The session title isn't in the payload.** A Notification hook receives only
`session_id`, `transcript_path`, `cwd`, `permission_mode` and `message` — no
title. The name is recovered from the transcript, which carries `custom-title`
entries (names you set) and `ai-title` entries (generated ones). Custom wins;
otherwise the most recent generated title is used. Only the last 512 KB of the
file is read, because transcripts routinely reach tens of megabytes — a 6.4 MB
transcript resolves in about 0.1 s.

**Clicking uses an internal deep link.** `claude://resume?session=<uuid>` is
handled by the Claude desktop app: the first use adopts the CLI session as a
desktop session, and later uses simply navigate to it. This was verified to be
idempotent — firing it repeatedly creates no duplicate sessions. The session id
is checked against a UUID pattern first, because the app silently discards
anything that doesn't match.

**The icon uses `-appIcon`, never `-sender`.** Both can show another app's icon,
but `-sender` reassigns ownership of the notification and hands the click to that
app — which would destroy the deep-link navigation. `-appIcon` changes only the
picture. The triggering app is found by walking the process ancestry until a
`.app` bundle with an icon appears; its `.icns` is converted once with `sips` and
cached. Toolchain paths are skipped, because `/usr/bin/python3` actually lives
inside `Xcode.app` and would otherwise brand every notification with the Xcode
icon.

Some smaller decisions worth knowing:

- **Absolute paths everywhere.** A GUI app spawns hooks with a nearly empty
  `PATH`, so `terminal-notifier`, `ps` and `sips` are all addressed absolutely. A
  hook that resolves tools through `PATH` works in your terminal and then fails
  silently in the app.
- **Failures are silent and bounded.** Every subprocess has a timeout, the
  ancestry walk has a hard depth limit, and any error degrades to a simpler
  notification. A hook that hangs is worse than one that says less.
- **No string interpolation into AppleScript.** The fallback passes message text
  as arguments to a handler, so notification text can't be injected as script.

## Limitations

- **macOS only.** It's built on macOS notifications.
- **`claude://resume` is internal and undocumented.** It works today and was
  verified idempotent, but Anthropic may change it. If clicking stops working,
  that's the first thing to check — notifications themselves will keep working.
- **Click-to-jump targets the desktop app.** If you live in the terminal CLI,
  you'll still get properly labelled notifications, but clicking opens the
  desktop app.
- **terminal-notifier is at 2.0.0** and lightly maintained. Notifications appear
  under its identity and need notification permission granted once.
- The first click on a terminal-CLI session **adopts** it into the desktop app as
  a desktop session. That's the deep link's designed behaviour, not a side effect
  of this hook.

## Uninstall

```bash
python3 install.py --uninstall
```

Removes the registration (leaving your other hooks untouched) and deletes the
installed script. `terminal-notifier` is left alone.

## License

MIT
