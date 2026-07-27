# claude-code-notify

Session-aware, clickable desktop notifications for [Claude Code](https://claude.com/claude-code).

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

## Platform support

| | macOS | Linux | Windows |
|---|:---:|:---:|:---:|
| Session name as the title | ✅ | ✅ | ✅ |
| Repo + permission mode | ✅ | ✅ *(in body)* | ✅ |
| One live banner per session | ✅ | ✅ | ✅ |
| Click to jump to the session | ✅ | ➖ | ⚠️ |
| Icon of the triggering app | ✅ | ➖ | ➖ |
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
claude plugin marketplace add hancheng-ai/claude-code-notify
claude plugin install claude-code-notify@hancheng-ai
```

Or from inside Claude Code, `/plugin marketplace add hancheng-ai/claude-code-notify`
then `/plugin install claude-code-notify@hancheng-ai`, followed by `/reload-plugins`.

The plugin adds a single `Notification` hook and **no model context** —
`claude plugin details claude-code-notify` reports it as
`harness-only — no model context cost`, so it costs nothing per session.

Updates come through `claude plugin update claude-code-notify`.

### Or manually

If you'd rather not use the plugin system:

```bash
git clone https://github.com/hancheng-ai/claude-code-notify.git
cd claude-code-notify
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

That prints what it resolved — platform, session, title, deep link, icon,
backend — and sends a real notification for your most recent session.

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

- **It reads transcript files** (`~/.claude/projects/**.jsonl`) for one purpose:
  recovering the session name. Only the last 512 KB of a file is read, and only
  `custom-title` / `ai-title` entries are used. Conversation content is never
  parsed or displayed.
- **No network access whatsoever.** There are no HTTP calls, no telemetry, no
  analytics, no crash reporting. The only URL-shaped string is the local
  `claude://` link handed to your OS.
- **What it writes:** cached app-icon PNGs under
  `~/.claude/.cache/claude-code-notify-icons/` (macOS only). The installer
  additionally writes `~/.claude/settings.json`, backing it up first.
- **What it runs:** `ps`, `sips`, `terminal-notifier`, `osascript` (macOS);
  `notify-send` (Linux); `powershell` (Windows). Nothing else.
- **Your session names appear on screen.** If a session is named after something
  sensitive, that name will be visible in notifications — worth knowing before
  you screen-share.
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

**On macOS the icon uses `-appIcon`, never `-sender`.** Both can show another
app's icon, but `-sender` reassigns ownership of the notification and hands the
click to that app — which would destroy the deep-link navigation. `-appIcon`
changes only the picture. The triggering app is found by walking the process
ancestry until a `.app` bundle with an icon appears; its `.icns` is converted
once with `sips` and cached. Toolchain paths are skipped, because
`/usr/bin/python3` actually lives inside `Xcode.app` and would otherwise brand
every notification with the Xcode icon.

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
hooks/hooks.json                 the Notification hook, exec form
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

43 tests covering title recovery (custom over generated, last-wins, truncated
JSON, tail-read boundary), payload handling, platform gating of the deep link,
the exact argv / toast XML each backend emits — including injection attempts
through session names — and the packaging itself: manifest/marketplace version
agreement, exec-form hook shape, and that no component directory has drifted
into `.claude-plugin/`.

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
claude plugin uninstall claude-code-notify
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
