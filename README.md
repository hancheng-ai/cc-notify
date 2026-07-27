# cc-notify

Session-aware, clickable desktop notifications for [Claude Code](https://claude.com/claude-code).

Run several Claude Code sessions at once and the stock notifications stop being
useful. Every banner looks the same, so you can't tell which session is asking
for you, and clicking one does nothing. You end up polling your sessions by hand
— which defeats the point of running them in parallel.

This hook makes each notification say **which session** wants you, tells you
**what happened**, and makes clicking it **jump straight back to that session** —
while staying quiet about the session you're already looking at.

```
┌──────────────────────────────────────────────┐
│ ◆  Refactor auth middleware                  │   ← session name, as in the sidebar
│    my-api · plan                             │   ← repo · permission mode
│    Claude needs your permission to use Bash  │
└──────────────────────────────────────────────┘
        click  →  jumps to that session
```

## When it fires

| Event | Banner says |
|---|---|
| Needs permission, or gone idle | the notification text |
| **Turn finished** | Claude's closing line, e.g. *"Refactored the parser."* |
| **Turn failed** | *"Failed: API Error: Rate limit reached"* |

Turn-end events matter because `Notification` alone never fires when a turn
simply completes — it waits on an idle timer. Kick off a 30-second task, look
away, and without a `Stop` hook nothing tells you it's done.

**It stays quiet about the session you're watching.** If the Claude desktop app
is frontmost *and* the event belongs to the session currently on screen, the
banner is skipped — you can already see it. This is deliberately narrow: a
frontmost app is not enough, because with several sessions open in one app the
one that needs you is usually *not* the one you're reading. When it can't
establish that this exact session is on screen, it notifies. Permission prompts
are never suppressed, since a wrong guess there would leave a session hanging.

## Platform support

| | macOS | Linux | Windows |
|---|:---:|:---:|:---:|
| Session name as the title | ✅ | ✅ | ✅ |
| Repo + permission mode | ✅ | ✅ *(in body)* | ✅ |
| Turn finished / failed | ✅ | ✅ | ✅ |
| One live banner per session | ✅ | ✅ | ✅ |
| Click to jump to the session | ✅ | ➖ | ⚠️ |
| Quiet when you're watching | ✅ | ➖ | ➖ |
| Own icon + Notification Center group | ✅ | ➖ | ➖ |
| Backend | `terminal-notifier`, falling back to `osascript` | `notify-send` | PowerShell toast |
| Verified on real hardware | ✅ | ⚠️ | ⚠️ |

**➖ Linux has no click-to-jump by design.** There is no Claude desktop app on
Linux for a deep link to open, and `notify-send --action` blocks until the user
responds — a hook that blocks is exactly what must never happen here. You still
get correctly labelled, per-session notifications, which is the part that makes
parallel sessions workable.

**⚠️ Honest status:** macOS is verified end to end on real hardware. The Linux
and Windows backends are implemented and covered by tests that assert the exact
command and toast XML they emit, including escaping — but they have **not** been
run against a real Linux desktop or Windows machine. If you try them, please
open an issue either way. Windows click-to-jump additionally depends on the
Windows Claude app registering the `claude://` scheme, which is unverified.

## Requirements

- Claude Code
- **macOS**: [terminal-notifier](https://github.com/julienXX/terminal-notifier)
  (optional but recommended — without it you lose click-to-jump and grouping)
  ```bash
  brew install terminal-notifier
  ```
- **Linux**: `notify-send`
  ```bash
  sudo apt install libnotify-bin     # Debian/Ubuntu
  sudo dnf install libnotify         # Fedora
  ```
- **Windows**: PowerShell (built in)

No Python packages. Standard library only.

## Install

### As a plugin (recommended)

```bash
claude plugin marketplace add hancheng-ai/cc-notify
claude plugin install cc-notify@hancheng-ai
```

Or from inside Claude Code, `/plugin marketplace add hancheng-ai/cc-notify`
then `/plugin install cc-notify@hancheng-ai`, followed by `/reload-plugins`.

The plugin registers `Notification`, `Stop` and `StopFailure` hooks and adds
**no model context** — `claude plugin details cc-notify` reports them as
`harness-only — no model context cost`, so it costs nothing per session.

Updates come through `claude plugin update cc-notify`.

### Or manually

If you'd rather not use the plugin system:

```bash
git clone https://github.com/hancheng-ai/cc-notify.git
cd cc-notify
python3 install.py
```

This copies the hook to `~/.claude/hooks/notify.py` and registers it in
`~/.claude/settings.json`. It **merges** into your existing configuration rather
than overwriting it, and backs the file up first. Use `--dry-run` to preview.

> **Don't do both.** Plugin hooks *merge* with your `settings.json` hooks rather
> than replacing them, and command hooks are deduplicated by command string —
> which differs between the two installs. Running both gives you **two
> notifications for every event**. `install.py` detects this and tells you how to
> resolve it; to keep the plugin, run `python3 install.py --uninstall`.

### Verify

Restart Claude Code, then check it end to end:

```bash
python3 ~/.claude/hooks/notify.py --self-test
```

That prints what it resolved — platform, session, title, deep link, backend and
notifier identity — and sends a real notification for your most recent session.

### Turn it down

Three environment variables, no config file needed:

| Variable | Effect |
|---|---|
| `CC_NOTIFY_NO_TURN_END=1` | Stop notifying when turns finish or fail; keep permission and idle prompts |
| `CC_NOTIFY_NO_SUPPRESS=1` | Always notify, even for the session you're looking at |
| `CC_NOTIFY_DRY_RUN=1` | Run every code path but post nothing — for debugging |
| `CC_NOTIFY_NO_REBADGE=1` | Post as the shared `terminal-notifier` instead of building our own identity |

Per-session grouping means a chatty session replaces its own banner rather than
stacking, so turn-end notifications stay bounded at one per session.

### Narrow which notifications you get

By default the hook fires for every notification type. To limit it, add a
`matcher` in `hooks/hooks.json` (plugin) or your `settings.json` entry —
for example only permission prompts and idle prompts:

```json
{ "matcher": "permission_prompt|idle_prompt", "hooks": [ ... ] }
```

Available types include `permission_prompt`, `idle_prompt`, `auth_success`,
`elicitation_dialog`, `agent_needs_input` and `agent_completed`.

## Privacy

Everything is local, and it is worth being explicit about that because the hook
reads your transcripts:

- **It reads transcript files** (`~/.claude/projects/**.jsonl`) to recover the
  session name. Only the last 512 KB of a file is read, and only `custom-title` /
  `ai-title` entries are parsed from it.
- **Claude's closing line is shown on turn-end banners.** This is conversation
  content, and you should know that before installing. It comes from the hook
  payload's `last_assistant_message` — not from scraping the transcript — is
  reduced to a single line of at most 140 characters, and is never stored or
  sent anywhere. Set `CC_NOTIFY_NO_TURN_END=1` to switch those banners off
  entirely.
- **It reads the desktop app's session index** (`~/Library/Application
  Support/Claude/claude-code-sessions/**.json`) on macOS, for one field —
  `lastFocusedAt` — to tell whether the session that fired is the one you're
  looking at. No conversation content lives in those files.
- **No network access whatsoever.** There are no HTTP calls, no telemetry, no
  analytics, no crash reporting. The only URL-shaped string is the local
  `claude://` link handed to your OS.
- **What it writes:** a re-badged copy of `terminal-notifier` (~1.2 MB) under
  `~/.claude/.cache/cc-notify/` or `${CLAUDE_PLUGIN_DATA}`, built once so
  notifications carry their own identity. The installer additionally writes
  `~/.claude/settings.json`, backing it up first.
- **What it runs:** `ps`, `lsappinfo`, `codesign`, `terminal-notifier`,
  `osascript` (macOS); `notify-send` (Linux); `powershell` (Windows). Nothing
  else. Every one of them has its output discarded to `/dev/null` and a timeout.
- **Your session names and Claude's last line appear on screen.** If either is
  sensitive it will be visible in notifications — worth knowing before you
  screen-share.
- `--self-test` prints local session details; redact before pasting into a
  public issue.

## How it works

Three things here are not obvious, and are the reason this isn't a five-line script.

**The session title isn't in the payload.** A Notification hook receives only
`session_id`, `transcript_path`, `cwd`, `permission_mode` and `message` — no
title. The name is recovered from the transcript, which carries `custom-title`
entries (names you set) and `ai-title` entries (generated ones). Custom wins;
otherwise the most recent generated title is used. Only the tail of the file is
read, because transcripts routinely reach tens of megabytes — a 6.4 MB
transcript resolves in about 0.1 s.

**Clicking uses an internal deep link.** `claude://resume?session=<uuid>` is
handled by the Claude desktop app: the first use adopts the CLI session as a
desktop session, and later uses simply navigate to it. This was verified to be
idempotent — firing it repeatedly creates no duplicate sessions. The session id
is checked against a UUID pattern first, because the app silently discards
anything that doesn't match.

**On macOS it posts under its own identity.** macOS takes a banner's icon — and
its Notification Center grouping — from the *sending* application, so every tool
that shells out to the one shared `terminal-notifier` looks identical and lands
in a single pile. You can't tell which of them is asking for you, which is the
whole problem this plugin exists to solve.

Neither flag `terminal-notifier` offers fixes that. `-appIcon` is accepted and
then **silently ignored** by current macOS — visually indistinguishable from
sending nothing. `-sender` would set the identity properly but **hangs for over
12 seconds** (measured), and would additionally hand the click to that app,
destroying the deep link.

So on first use it builds a private, re-badged copy of the notifier in
`~/.claude/.cache/cc-notify/` (or `${CLAUDE_PLUGIN_DATA}`): same binary, its own
bundle id, and the icon of whichever app launched the session — found by walking
the process ancestry, skipping toolchain paths, since `/usr/bin/python3` lives
inside `Xcode.app` and would otherwise brand every notification with the Xcode
icon. The bundle is re-signed ad-hoc afterwards, because macOS otherwise records
the wrong identity. That build costs ~2s once; later notifications reuse it and
run in ~0.3s. If anything about it fails, it silently falls back to the shared
notifier — a wrong icon beats no notification. `CC_NOTIFY_NO_REBADGE=1` opts out.

Some smaller decisions worth knowing:

- **Known locations before a `PATH` lookup.** Hooks inherit whatever environment
  Claude Code itself runs with — measured to be the full user `PATH`, including
  Homebrew directories, when launched from the desktop app. That is not
  guaranteed for every setup, so the well-known install paths are checked first;
  it costs nothing and removes a failure mode.
- **Nothing cached inside the plugin.** `${CLAUDE_PLUGIN_ROOT}` changes on every
  plugin update, so icon caches go to `${CLAUDE_PLUGIN_DATA}` when running as a
  plugin, and to `~/.claude/.cache/` otherwise.
- **Exec form, not shell form.** The plugin hook passes the script path as an
  `args` element rather than interpolating it into a shell string, which is what
  the docs recommend for any hook referencing a path placeholder — no quoting to
  get wrong, and install paths containing spaces just work.
- **Failures are silent and bounded.** Every subprocess has a timeout, the
  ancestry walk has a hard depth limit, and any error degrades to a simpler
  notification. A hook that hangs is worse than one that says less.
- **Untrusted text is never interpolated into code.** Session names come from
  your transcript and messages from the hook payload. The AppleScript path
  passes them as handler arguments; the Windows path XML-escapes them and
  doubles single quotes for PowerShell; the Linux path puts `--` before the
  title so a session named `--help` can't be read as a flag. All three are
  covered by tests.

## Layout

```
.claude-plugin/plugin.json       plugin manifest
.claude-plugin/marketplace.json  self-hosted marketplace (source: "./")
hooks/hooks.json                 Notification + Stop + StopFailure, exec form
notify.py                        the hook itself
install.py                       manual (non-plugin) installer
test_backends.py                 test suite
```

The repo is both the plugin and its own marketplace, which is why `source` is
`"./"` — no separate marketplace repo to keep in sync.

## Tests

```bash
python3 test_backends.py
```

76 tests covering title recovery (custom over generated, last-wins, truncated
JSON, tail-read boundary), message extraction from every event shape, focus
suppression (including that a *different* session in the same app is never
suppressed, and that permission prompts never are), the exact argv / toast XML
each backend emits — including injection attempts through session names — and
the packaging itself: manifest/marketplace version agreement, exec-form hook
shape, turn-end event registration, and that no component directory has drifted
into `.claude-plugin/`.

One of them is a regression test with teeth: it asserts a notifier that leaves a
helper holding the output pipe cannot block us past the timeout. Reintroduce the
bug it guards and that test fails in 10s instead of passing in 0.3s.

The suite posts no real notifications and never blocks: it asserts the hook
exits 0 on malformed input, on `stop_hook_active`, and on garbage stdin.

Manifests are additionally checked with the real validator:

```bash
claude plugin validate .claude-plugin/plugin.json --strict
claude plugin validate .claude-plugin/marketplace.json --strict
```

## Limitations

- **`claude://resume` is internal and undocumented.** It works today and was
  verified idempotent, but Anthropic may change it. If clicking stops working,
  that's the first thing to check — notifications themselves will keep working.
- **terminal-notifier is at 2.0.0** and lightly maintained. On macOS,
  notifications appear under its identity and need notification permission
  granted once.
- The first click on a terminal-CLI session **adopts** it into the desktop app
  as a desktop session. That's the deep link's designed behaviour, not a side
  effect of this hook.
- Linux replacement uses the `x-canonical-private-synchronous` hint. GNOME, KDE
  and dunst honour it; a daemon that doesn't will simply stack notifications
  instead of replacing them.

## Uninstall

If you installed the plugin:

```bash
claude plugin uninstall cc-notify
```

If you installed manually:

```bash
python3 install.py --uninstall
```

The manual uninstall removes only its own registration — your other hooks are
left untouched — and deletes the installed script. The notification backend
(`terminal-notifier` / `libnotify`) is left installed either way.

## License

MIT
