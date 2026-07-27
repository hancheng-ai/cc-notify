#!/usr/bin/env python3
"""cc-notify — session-aware, clickable desktop notifications for Claude Code.

Run several Claude Code sessions at once and the built-in notifications become
useless: every banner looks identical, so you cannot tell which session wants
you, and clicking one does nothing. This hook fixes both.

Each notification carries:
  * title    - the session's name, the same one shown in the sidebar
  * subtitle - the repo, plus the permission mode when it isn't "default"
  * click    - jumps straight back to that session (macOS/Windows)
  * grouping - one live banner per session instead of a growing pile
  * icon     - the app that triggered it (macOS)

Three things make this work, none of them obvious:

1. The Notification hook payload has no session title. It carries only
   session_id, transcript_path, cwd, permission_mode and message. The title is
   recovered from the transcript itself, which contains `custom-title` and
   `ai-title` entries. Only the tail of the file is read, since transcripts
   routinely reach tens of megabytes.

2. Clicking is wired to `claude://resume?session=<uuid>`, an internal deep link
   in the Claude desktop app. The first use adopts the CLI session as a desktop
   session; later uses just navigate. Verified idempotent - repeat clicks do not
   create duplicate sessions.

3. On macOS the icon uses terminal-notifier's `-appIcon`, never `-sender`.
   `-sender` reassigns the notification to another app and hands it the click,
   which would destroy the deep-link navigation. `-appIcon` changes only the
   picture.

Design rules: standard library only, known install locations checked before a
PATH lookup, every failure silent, and never block the session. Notification
hooks cannot block or modify anything, and a non-zero exit only produces stderr
noise for the user - so this always exits 0, and a hook that hangs is worse than
no notification.

Privacy: everything is local. The transcript is read only to recover the session
name, nothing is transmitted anywhere, and there is no telemetry. The only
network-shaped string in this file is a `claude://` URL handed to the OS.

MIT licensed. https://github.com/hancheng-ai/cc-notify
"""
import sys, json, os, re, shutil, subprocess, urllib.parse
from xml.sax.saxutils import escape as xml_escape, quoteattr as xml_attr

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform in ("win32", "cygwin")
IS_LINUX = sys.platform.startswith("linux")

TAIL = 512 * 1024  # transcripts get large; the newest title is near the end

# The desktop app validates the session id with exactly this pattern and
# silently drops anything else, so match it before building a deep link.
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# Well-known absolute locations are tried before a PATH lookup. Hooks inherit
# whatever environment Claude Code itself runs with - usually the full user PATH
# - but that is not guaranteed to include Homebrew's directories, so checking
# the known install paths first costs nothing and removes a failure mode.
TN_PATHS = ("/usr/local/bin/terminal-notifier",     # Homebrew on Intel
            "/opt/homebrew/bin/terminal-notifier")  # Homebrew on Apple Silicon
NOTIFY_SEND_PATHS = ("/usr/bin/notify-send", "/usr/local/bin/notify-send")
POWERSHELL_PATHS = (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",)
MAC_PS, MAC_SIPS = "/bin/ps", "/usr/bin/sips"

# ${CLAUDE_PLUGIN_ROOT} changes on every plugin update, so cached artefacts must
# not live there. When running as a plugin Claude Code provides a persistent data
# directory; standalone installs fall back to the usual cache location.
ICONS = (os.path.join(os.environ["CLAUDE_PLUGIN_DATA"], "icons")
         if os.environ.get("CLAUDE_PLUGIN_DATA")
         else os.path.expanduser("~/.claude/.cache/cc-notify-icons"))


def _first_executable(paths, *names):
    for p in paths:
        if os.access(p, os.X_OK):
            return p
    for n in names:
        found = shutil.which(n)
        if found:
            return found
    return None


# --------------------------------------------------------------------------
# Session title recovery (all platforms)
# --------------------------------------------------------------------------

def session_title(path):
    """Return the session's display name, or None.

    Prefers `custom-title` (a name you set yourself, which is what the sidebar
    shows) over `ai-title` (generated). Within each kind the last entry wins,
    because titles are rewritten as the session evolves.
    """
    if not path or not os.path.isfile(path):
        return None
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > TAIL:
                f.seek(size - TAIL)
                f.readline()  # discard the partial line the seek landed in
            chunk = f.read().decode("utf-8", "replace")
    except Exception:
        return None

    custom = ai = None
    for line in chunk.splitlines():
        # Cheap substring filter first; json.loads on every line of a large
        # transcript is what makes naive versions of this slow.
        if '"custom-title"' not in line and '"ai-title"' not in line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if not isinstance(o, dict):
            continue
        if o.get("type") == "custom-title" and o.get("customTitle"):
            custom = str(o["customTitle"])
        elif o.get("type") == "ai-title" and o.get("aiTitle"):
            ai = str(o["aiTitle"])
    return custom or ai


# --------------------------------------------------------------------------
# macOS: trigger icon via process ancestry
# --------------------------------------------------------------------------

def _icon_for(exe):
    """Map an executable path to its .app icon as a cached PNG file:// URL."""
    i = exe.find(".app/")
    if i < 0:
        return None
    # Skip toolchain paths. /usr/bin/python3 actually lives inside
    # Xcode.app/Contents/Developer, so without this every notification would
    # proudly display the Xcode icon.
    if "/Contents/Developer/" in exe:
        return None
    bundle = exe[:i + 4]
    try:
        import plistlib
        with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as f:
            icon = (plistlib.load(f) or {}).get("CFBundleIconFile")
    except Exception:
        return None
    if not icon:
        return None  # icon-less wrapper bundle; keep walking up to the real host
    if not icon.endswith(".icns"):
        icon += ".icns"
    src = os.path.join(bundle, "Contents", "Resources", icon)
    if not os.path.isfile(src):
        return None

    dst = os.path.join(ICONS, (os.path.basename(bundle)[:-4] or "app") + ".png")
    if not os.path.isfile(dst):  # convert once, then reuse
        try:
            os.makedirs(ICONS, exist_ok=True)
            subprocess.run([MAC_SIPS, "-s", "format", "png", "-Z", "128", src, "--out", dst],
                           capture_output=True, timeout=10)
        except Exception:
            return None
        if not os.path.isfile(dst):
            return None
    return "file://" + urllib.parse.quote(dst)


def trigger_icon():
    """Icon of whichever app triggered this hook, found by walking ancestors.

    Started from the Claude desktop app you get the Claude icon; started from a
    terminal you get Terminal's or iTerm's. macOS only - there is no comparable
    per-app icon lookup on Linux, and Windows toasts use their own image model.
    """
    if not IS_MAC:
        return None
    pid = os.getppid()  # start at the parent: this process is just the interpreter
    for _ in range(10):  # bounded, so a strange process tree can never spin
        try:
            out = subprocess.run([MAC_PS, "-o", "ppid=,comm=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=2).stdout.strip()
        except Exception:
            return None
        # comm can contain spaces ("Application Support"), so split once only.
        parts = out.split(None, 1)
        if len(parts) < 2:
            return None
        ppid, exe = parts[0], parts[1].strip()
        url = _icon_for(exe)
        if url:
            return url
        if ppid in ("0", "1", str(pid)):
            return None
        try:
            pid = int(ppid)
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------
# Focus suppression - don't banner a session the user is already watching
# --------------------------------------------------------------------------

DESKTOP_BUNDLE = "com.anthropic.claudefordesktop"
LSAPPINFO = "/usr/bin/lsappinfo"
SESSION_STORE = os.path.expanduser(
    "~/Library/Application Support/Claude/claude-code-sessions")


def frontmost_bundle():
    """Bundle id of the frontmost macOS app, or None.

    `lsappinfo` needs no accessibility permission and raises no TCC prompt,
    unlike the System Events route other notifiers use. Measured at ~0.02s.
    """
    if not IS_MAC or not os.access(LSAPPINFO, os.X_OK):
        return None
    try:
        asn = subprocess.run([LSAPPINFO, "front"], capture_output=True,
                             text=True, timeout=2).stdout.strip()
        if not asn:
            return None
        out = subprocess.run([LSAPPINFO, "info", "-only", "bundleid", asn],
                             capture_output=True, text=True, timeout=2).stdout
    except Exception:
        return None
    m = re.search(r'"CFBundleIdentifier"\s*=\s*"([^"]+)"', out or "")
    return m.group(1) if m else None


def foreground_session():
    """cliSessionId of the desktop session most recently switched to, or None.

    The desktop app stamps lastFocusedAt (epoch ms) into each session record, so
    the newest one is the tab on screen whenever the app itself is frontmost.
    """
    best_ts, best_id = 0, None
    try:
        for dirpath, _, names in os.walk(SESSION_STORE):
            for n in names:
                if not (n.startswith("local_") and n.endswith(".json")):
                    continue
                try:
                    with open(os.path.join(dirpath, n), encoding="utf-8") as f:
                        d = json.load(f)
                except Exception:
                    continue
                ts = d.get("lastFocusedAt") or 0
                if isinstance(ts, (int, float)) and ts > best_ts:
                    best_ts, best_id = ts, d.get("cliSessionId")
    except Exception:
        return None
    return best_id


def user_is_watching(session_id):
    """True only when THIS session can be established as the one on screen.

    Fails OPEN - returns False, meaning notify - on any uncertainty.

    The dangerous mistake here is suppressing merely because the app is
    frontmost, when the user is actually reading a DIFFERENT session inside that
    same app. That is the exact situation this tool exists for, so the desktop
    path additionally requires this session to be the most recently focused one.

    Terminal hosts always fail open: a frontmost Terminal.app says nothing about
    which tab or pane holds this session, and wrongly suppressing there could
    hide a session that is genuinely blocked on you.
    """
    if not session_id or os.environ.get("CC_NOTIFY_NO_SUPPRESS"):
        return False
    if frontmost_bundle() != DESKTOP_BUNDLE:
        return False
    return foreground_session() == session_id


# --------------------------------------------------------------------------
# Command builders - pure functions, so every backend is testable without a
# desktop session. The runners below are thin wrappers around these.
# --------------------------------------------------------------------------

def macos_argv(tn, title, sub, msg, url, group, icon):
    """terminal-notifier invocation. Click-to-jump and grouping both supported."""
    argv = [tn, "-title", title, "-message", msg]
    if sub:
        argv += ["-subtitle", sub]
    if url:
        argv += ["-open", url]
    if group:
        argv += ["-group", group]  # replaces this session's previous banner
    if icon:
        argv += ["-appIcon", icon]
    return argv


def linux_argv(ns, title, sub, msg, group):
    """notify-send invocation.

    libnotify has no subtitle, so the subtitle becomes the body's first line.
    Replacement uses the `x-canonical-private-synchronous` hint, which GNOME,
    KDE and dunst all honour, keyed by session so each session keeps one banner.

    No click action: notify-send's `--action` blocks until the user responds,
    and a hook that blocks is exactly what must never happen here. Linux also
    has no Claude desktop app for a deep link to open.
    """
    body = f"{sub}\n{msg}" if sub else msg
    argv = [ns, "-a", "Claude Code", "-u", "normal"]
    if group:
        argv += ["-h", f"string:x-canonical-private-synchronous:ccnotify-{group}"]
    return argv + ["--", title, body]


def windows_script(title, sub, msg, url, group):
    """PowerShell toast via WinRT. Returns the script text.

    Click-to-jump uses protocol activation (`activationType="protocol"`), which
    hands the `claude://` URL to the shell. Grouping uses the toast's Tag/Group,
    so a new toast for the same session replaces the old one.

    Everything interpolated is XML-escaped; text arrives from a transcript and
    a hook payload and must never be able to close a tag or inject an attribute.
    """
    lines = [xml_escape(t) for t in ([sub] if sub else []) + [msg]]
    body = "".join(f"<text>{t}</text>" for t in lines)
    launch = f' activationType="protocol" launch={xml_attr(url)}' if url else ""
    toast = (f'<toast{launch}><visual><binding template="ToastGeneric">'
             f'<text>{xml_escape(title)}</text>{body}</binding></visual></toast>')

    # PowerShell single-quoted strings escape a quote by doubling it.
    def psq(s):
        return "'" + str(s).replace("'", "''") + "'"

    tag = (group or "claude-code")[:64]
    return (
        "$ErrorActionPreference='Stop'\n"
        "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,"
        "ContentType=WindowsRuntime]|Out-Null\n"
        "[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,"
        "ContentType=WindowsRuntime]|Out-Null\n"
        f"$x=New-Object Windows.Data.Xml.Dom.XmlDocument\n"
        f"$x.LoadXml({psq(toast)})\n"
        "$t=New-Object Windows.UI.Notifications.ToastNotification $x\n"
        f"$t.Tag={psq(tag)}\n"
        "$t.Group='cc-notify'\n"
        "$id='{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe'\n"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show($t)\n"
    )


def applescript_argv(title, sub, msg):
    """macOS fallback. No click action is possible: `display notification` has
    no mechanism for one, which is why the stock experience is a dead end.

    Text is passed as arguments to a handler rather than interpolated into the
    source, so message content can never be executed as script.
    """
    body = ("display notification m with title t subtitle s" if sub
            else "display notification m with title t")
    argv = ["osascript"]
    for ln in ("on run {m, t, s}", body, "end run"):
        argv += ["-e", ln]
    return argv + [msg, title] + ([sub] if sub else [])


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

def _run(argv, **kw):
    try:
        return subprocess.run(argv, timeout=10, capture_output=True, **kw).returncode == 0
    except Exception:
        return False


def notify(title, sub, msg, url, group):
    """Post the notification using the best backend this platform offers."""
    if os.environ.get("CC_NOTIFY_DRY_RUN"):
        # Escape hatch for the test suite and for debugging: exercise every code
        # path up to the send without putting banners in someone's face.
        return
    if IS_MAC:
        tn = _first_executable(TN_PATHS, "terminal-notifier")
        if tn and _run(macos_argv(tn, title, sub, msg, url, group, trigger_icon())):
            return
        _run(applescript_argv(title, sub, msg))
        return

    if IS_LINUX:
        ns = _first_executable(NOTIFY_SEND_PATHS, "notify-send")
        if ns:
            _run(linux_argv(ns, title, sub, msg, group))
        return

    if IS_WIN:
        ps = _first_executable(POWERSHELL_PATHS, "powershell", "pwsh")
        if ps:
            _run([ps, "-NoProfile", "-NonInteractive", "-Command",
                  windows_script(title, sub, msg, url, group)])
        return


def deep_link_supported():
    """Linux has no Claude desktop app, so there is nothing for a link to open."""
    return IS_MAC or IS_WIN


def flatten_text(v):
    """Reduce a message value to plain text.

    `last_assistant_message` is documented as text, but content in this API is
    routinely a list of typed blocks, so both shapes are handled rather than
    assumed. Anything unrecognised degrades to "".
    """
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        out = []
        for b in v:
            if isinstance(b, str):
                out.append(b)
            elif isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                out.append(str(b["text"]))
        return " ".join(out)
    if isinstance(v, dict):
        return flatten_text(v.get("content") or v.get("text") or "")
    return ""


def first_line(s, limit=140):
    """First meaningful line, lightly de-marked-down, truncated by CHARACTER.

    Byte truncation would split a multi-byte character mid-codepoint, which
    matters immediately for CJK output.
    """
    for raw in str(s).splitlines():
        line = raw.strip()
        if not line or line.startswith("```"):
            continue
        line = re.sub(r"^[#>\-*•\s]+", "", line)  # heading / quote / bullet
        line = re.sub(r"[*_`]", "", line).strip()      # inline emphasis
        if line:
            return line[:limit] + ("…" if len(line) > limit else "")
    return ""


def message_for(d, event):
    """The banner body, which differs by what actually happened."""
    if event in ("Stop", "SubagentStop"):
        body = first_line(flatten_text(d.get("last_assistant_message")))
        agent = str(d.get("agent_type") or "").strip()
        if event == "SubagentStop" and agent:
            return body or f"{agent} finished"
        return body or "Turn finished"
    if event == "StopFailure":
        # For StopFailure `last_assistant_message` is the rendered error string
        # ("API Error: Rate limit reached"), while `error` is an enum token
        # (rate_limit, billing_error, ...). Prefer the readable one, and only
        # fall back to humanising the token.
        err = (first_line(flatten_text(d.get("last_assistant_message")))
               or first_line(flatten_text(d.get("error_details")))
               or str(d.get("error") or "").replace("_", " "))
        return f"Failed: {err}" if err else "Turn failed"
    return str(d.get("message") or "Claude Code needs you").strip()


TURN_END = ("Stop", "SubagentStop", "StopFailure")


def should_notify(d, session_id):
    """Whether this event deserves a banner at all.

    Permission prompts always get through: they block the session, so a false
    suppression would leave it hanging indefinitely. Everything else is
    informational and can be skipped when you are demonstrably already looking
    at that session.

    Turn-end events fire on every turn, which is welcome for the long refactor
    you walked away from and tiresome for a rapid back-and-forth. Per-session
    grouping means they replace rather than stack, and CC_NOTIFY_NO_TURN_END
    turns them off outright.
    """
    event = str(d.get("hook_event_name") or "Notification")
    if event in TURN_END and os.environ.get("CC_NOTIFY_NO_TURN_END"):
        return False

    # Stop fires once per PAUSE, not once per turn. When the model backgrounds
    # subagents it fires again each time it stops to wait for them - measured at
    # three fires for a single prompt, each with a different prompt_id. Only the
    # last one has an empty background_tasks, so the earlier fires would claim
    # "turn finished" while work is still running. Deduping on session_id or
    # prompt_id does not help: the former is identical across all three and the
    # latter differs across all three.
    #
    # Deliberately Stop-only. On SubagentStop this array describes the PARENT
    # session, so one subagent finishing while other background work continues
    # is still a genuine completion worth reporting.
    if event == "Stop" and (d.get("background_tasks") or []):
        return False
    if str(d.get("notification_type") or "") == "permission_prompt":
        return True
    return not user_is_watching(session_id)


def build(d):
    """Turn a hook payload into (title, subtitle, message, deep link, group)."""
    event = str(d.get("hook_event_name") or "Notification")
    msg = message_for(d, event).strip()[:180]

    # Derive the repo from cwd. Hardcoding a project name here would make every
    # repo's notifications claim to come from whichever one you wrote down.
    # Split on both separators rather than using os.path.basename, which only
    # recognises "\" when the interpreter itself is running on Windows.
    proj = re.split(r"[/\\]", str(d.get("cwd") or "").rstrip("/\\"))[-1]
    stitle = session_title(d.get("transcript_path"))
    title = (stitle or proj or "Claude Code")[:64]

    # When no session title exists the title has already degraded to the repo
    # name, so don't repeat it in the subtitle.
    bits = [proj] if (stitle and proj) else []
    mode = str(d.get("permission_mode") or "")
    if mode and mode != "default":
        bits.append(mode)
    sub = " · ".join(bits)

    sid = str(d.get("session_id") or "")
    url = (f"claude://resume?session={sid}"
           if UUID_RE.match(sid) and deep_link_supported() else None)
    return title, sub, msg, url, (sid or None)


def self_test():
    """Send a real notification for your most recent session, then report.

    Prints local session details (name, path). Redact before pasting into a
    public bug report.
    """
    root = os.path.expanduser("~/.claude/projects")
    newest = None
    for dirpath, _, names in os.walk(root):
        for n in names:
            if not n.endswith(".jsonl"):
                continue
            p = os.path.join(dirpath, n)
            try:
                mt = os.path.getmtime(p)
            except Exception:
                continue
            if newest is None or mt > newest[0]:
                newest = (mt, p)
    if not newest:
        print("No transcripts under ~/.claude/projects - start a session first.")
        return 1

    path = newest[1]
    sid = os.path.basename(path)[:-6]
    title, sub, msg, url, group = build(
        {"session_id": sid, "transcript_path": path, "cwd": os.getcwd(),
         "permission_mode": "default",
         "message": "Self-test - click me to jump to this session."})

    if IS_MAC:
        backend = _first_executable(TN_PATHS, "terminal-notifier") or "osascript (no click-to-jump)"
    elif IS_LINUX:
        backend = _first_executable(NOTIFY_SEND_PATHS, "notify-send") or "MISSING - install libnotify"
    elif IS_WIN:
        backend = _first_executable(POWERSHELL_PATHS, "powershell", "pwsh") or "MISSING - powershell"
    else:
        backend = f"unsupported platform: {sys.platform}"

    print(f"platform  : {sys.platform}")
    print(f"session   : {sid}")
    print(f"title     : {title}")
    print(f"subtitle  : {sub or '(none)'}")
    print(f"deep link : {url or '(not supported on this platform)'}")
    print(f"icon      : {trigger_icon() or '(macOS only)'}")
    print(f"backend   : {backend}")
    notify(title, sub, msg, url, group)
    print("\nSent. Check your notifications" + (", then click it." if url else "."))
    return 0


def main():
    if "--self-test" in sys.argv:
        return self_test()
    try:
        d = json.load(sys.stdin)
        if not isinstance(d, dict):
            d = {}  # valid JSON that isn't an object, e.g. a bare array
    except Exception:
        d = {}

    # Everything below is wrapped, because for Stop and SubagentStop the exit
    # code is not cosmetic: exit 2 feeds stderr back to the model and continues
    # the turn, which lets a crashing notifier silently rewrite the user's
    # session. Nothing this script does is worth that risk, so no failure is
    # allowed to escape. Nothing is printed to stdout either - stdout beginning
    # with "{" is parsed as a control decision.
    try:
        # True when this turn is a continuation that a stop hook itself caused
        # by blocking - so it is not a real turn end, and firing would banner
        # the user mid-loop.
        if d.get("stop_hook_active"):
            return 0
        if should_notify(d, str(d.get("session_id") or "")):
            notify(*build(d))
    except Exception:
        pass
    return 0  # observational hook: must never block or alter a turn


if __name__ == "__main__":
    sys.exit(main())
