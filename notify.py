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


def build(d):
    """Turn a hook payload into (title, subtitle, message, deep link, group)."""
    msg = (str(d.get("message") or "Claude Code needs you").strip())[:180]

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
    except Exception:
        d = {}
    notify(*build(d))
    return 0


if __name__ == "__main__":
    sys.exit(main())
