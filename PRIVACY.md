# Privacy Policy — cc-notify

_Last updated: 2026-08-04_

**cc-notify collects nothing, transmits nothing, and contacts no server.**
It is a local notification hook: it reads a small amount of local data in order
to label a desktop notification, and that data never leaves your machine.

This document describes the plugin's behaviour precisely, because it does read
files that contain your work, and you deserve to know exactly which and why.

## What it reads

| Source | What is read | Why |
|---|---|---|
| `~/.claude/projects/**.jsonl` (your session transcripts) | The **last 512 KB** of the relevant file, and from it only `custom-title` and `ai-title` records | To title the notification with the session's name. Conversation content in the transcript is not parsed. |
| The hook payload supplied by Claude Code | `session_id`, `transcript_path`, `cwd`, `permission_mode`, `message`, `last_assistant_message`, `error`, `background_tasks`, `stop_hook_active` | These are the fields Claude Code passes to any hook. They determine what the notification says. |
| `~/Library/Application Support/Claude/claude-code-sessions/**.json` (macOS only) | Five fields: `sessionId`, `cliSessionId`, `lastActivityAt`, `lastFocusedAt`, `isArchived` | Two jobs. `lastFocusedAt` tells whether the session that fired is the one you are looking at, so it can stay quiet. The rest resolve a click to the right session row, so clicking a banner opens that conversation instead of importing a duplicate of it. These files hold no conversation content, and titles found there are not read. |
| The triggering application's `Info.plist` and icon (macOS only) | Bundle metadata and the `.icns` file | To give notifications an appropriate icon. |

## What appears on your screen

A notification can display:

- the **session name**, taken from your transcript;
- the **repository directory name** and permission mode;
- the **notification text** Claude Code supplied; and
- on turn-end notifications, **Claude's closing line** — taken from the hook
  payload's `last_assistant_message`, reduced to a single line of at most 140
  characters.

That last item is conversation content. It is displayed and then discarded — it
is never written to disk or transmitted. If you would rather it never appear,
set `CC_NOTIFY_NO_TURN_END=1` and turn-end notifications stop entirely.

Be aware that session names and that closing line are visible on screen, which
matters if you screen-share or leave notifications on a lock screen.

## What it writes

- A re-badged copy of `terminal-notifier` (~1.2 MB) under
  `~/.claude/.cache/cc-notify/` or `${CLAUDE_PLUGIN_DATA}`, built once so that
  notifications carry their own identity rather than being pooled with every
  other tool that uses the shared notifier. Disable with
  `CC_NOTIFY_NO_REBADGE=1`.
- If you use the manual installer instead of the plugin, `install.py` writes
  `~/.claude/settings.json` to register the hook, backing the file up first.

No logs, no history, no database, no cache of your conversations.

## What it runs

`ps`, `lsappinfo`, `codesign`, `mdfind`, `file`, `open`, `terminal-notifier`
and `osascript` on macOS; `notify-send` on Linux; `powershell` on Windows. Every
one is a local command invoked by absolute path, its output is discarded to
`/dev/null`, and each has a timeout.

`codesign` deserves a word, since re-signing a binary is the least ordinary
thing here. macOS takes a notification's icon and its Notification Center group
from the *sending* application, so every tool sharing one `terminal-notifier`
looks identical. The fix is a private copy carrying our own bundle id, and
editing a bundle invalidates its signature — macOS may then refuse to deliver
at all. The copy is signed ad-hoc (`--sign -`), which is a local, identity-less
signature: it grants no entitlements and asserts no developer identity. Nothing
outside `~/.claude/` is signed, and `CC_NOTIFY_NO_REBADGE=1` skips the whole
step.

Clicking a notification runs one more: this same script, re-invoked as
`notify.py --open <claude:// url>`. The click target is a command rather than a
plain URL so the session lookup happens when you click, not when the banner was
posted — a banner can sit in Notification Center for days. It only ever opens a
`claude://` URL, validated against a fixed pattern first, and does nothing else.

## What it does not do

- **No network access.** There is no HTTP client, socket, or fetch of any kind
  anywhere in the source. The only URL-shaped string is a local `claude://` deep
  link handed to your operating system so clicking a notification opens the
  right session.
- **No telemetry, analytics, crash reporting, or usage statistics.**
- **No third parties.** Nothing is shared with anyone, including the author.
- **No accounts, no credentials, no configuration uploaded anywhere.**

You can verify all of this: the plugin is a single readable Python file with no
dependencies beyond the standard library.

## Turning things off

| Variable | Effect |
|---|---|
| `CC_NOTIFY_NO_TURN_END=1` | No turn-end notifications, so Claude's closing line is never displayed |
| `CC_NOTIFY_NO_SUPPRESS=1` | Skip the focus check, so `lastFocusedAt` is not consulted |
| `CC_NOTIFY_NO_DEEPLINK=1` | No click target, so the session index is not consulted to resolve one |
| `CC_NOTIFY_NO_REBADGE=1` | Do not build the private notifier bundle |
| `CC_NOTIFY_DRY_RUN=1` | Run every code path but display nothing |

To remove it entirely: `claude plugin uninstall cc-notify`, or
`python3 install.py --uninstall` for a manual install.

## Changes

Any change to this policy will appear in this file's git history, which is
public. Material changes will be noted in the release that carries them.

## Contact

Questions or corrections: https://github.com/hancheng-ai/cc-notify/issues
