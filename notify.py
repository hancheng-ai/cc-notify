#!/usr/bin/env python3
"""claude-code-notify — session-aware, clickable macOS notifications for Claude Code.

Run several Claude Code sessions at once and the built-in notifications become
useless: every banner looks identical, so you cannot tell which session wants
you, and clicking one does nothing. This hook fixes both.

Each notification carries:
  * title    - the session's name, the same one shown in the sidebar
  * subtitle - the repo, plus the permission mode when it isn't "default"
  * icon     - the app that triggered it (Claude, Terminal, iTerm, ...)
  * click    - jumps straight back to that session in the Claude desktop app
  * grouping - one live banner per session instead of a growing pile

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

3. The icon uses terminal-notifier's `-appIcon`, never `-sender`. `-sender`
   reassigns the notification to another app and hands it the click, which would
   destroy the deep-link navigation. `-appIcon` changes only the picture.

Design rules: standard library only, absolute interpreter paths (a GUI app
spawns hooks with a nearly empty PATH), every failure silent, and never block
the session. A notification hook that hangs is worse than no notification.

Requires macOS. terminal-notifier is optional but needed for click-to-jump;
without it this degrades to a plain osascript banner.

MIT licensed. https://github.com/hancheng-ai/claude-code-notify
"""
import sys, json, os, re, plistlib, subprocess, urllib.parse

TAIL = 512 * 1024  # transcripts get large; the newest title is near the end
ICONS = os.path.expanduser("~/.claude/.cache/claude-code-notify-icons")

# The desktop app validates the session id with exactly this pattern and
# silently drops anything else, so match it before building a deep link.
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# Absolute paths throughout: hooks launched by a GUI app get PATH=/usr/bin:/bin,
# so anything found via PATH lookup will not be found at all.
PS, SIPS = "/bin/ps", "/usr/bin/sips"
TN_PATHS = ("/usr/local/bin/terminal-notifier",    # Homebrew on Intel
            "/opt/homebrew/bin/terminal-notifier")  # Homebrew on Apple Silicon


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
            subprocess.run([SIPS, "-s", "format", "png", "-Z", "128", src, "--out", dst],
                           capture_output=True, timeout=10)
        except Exception:
            return None
        if not os.path.isfile(dst):
            return None
    return "file://" + urllib.parse.quote(dst)


def trigger_icon():
    """Icon of whichever app triggered this hook, found by walking ancestors.

    Started from the Claude desktop app you get the Claude icon; started from a
    terminal you get Terminal's or iTerm's.
    """
    pid = os.getppid()  # start at the parent: this process is just the interpreter
    for _ in range(10):  # bounded, so a strange process tree can never spin
        try:
            out = subprocess.run([PS, "-o", "ppid=,comm=", "-p", str(pid)],
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


def notify(title, sub, msg, url, group):
    """Post the notification, preferring terminal-notifier, falling back to osascript."""
    for tn in TN_PATHS:
        if not os.access(tn, os.X_OK):
            continue
        args = [tn, "-title", title, "-message", msg]
        if sub:
            args += ["-subtitle", sub]
        if url:
            args += ["-open", url]
        if group:
            args += ["-group", group]  # replaces this session's previous banner
        icon = trigger_icon()
        if icon:
            args += ["-appIcon", icon]
        try:
            if subprocess.run(args, timeout=8, capture_output=True).returncode == 0:
                return
        except Exception:
            pass  # fall through to osascript

    # Fallback. No click action is possible here: `display notification` simply
    # has no mechanism for one, which is why the stock experience is a dead end.
    # Arguments are passed to an AppleScript handler rather than interpolated
    # into the source, so message text can never be injected as script.
    try:
        lines = ["on run {m, t, s}",
                 "display notification m with title t subtitle s" if sub
                 else "display notification m with title t",
                 "end run"]
        args = ["osascript"]
        for ln in lines:
            args += ["-e", ln]
        args += [msg, title] + ([sub] if sub else [])
        subprocess.run(args, timeout=5, capture_output=True)
    except Exception:
        pass


def build(d):
    """Turn a hook payload into (title, subtitle, message, deep link, group)."""
    msg = (str(d.get("message") or "Claude Code needs you").strip())[:180]

    # Derive the repo from cwd. Hardcoding a project name here would make every
    # repo's notifications claim to come from whichever one you wrote down.
    proj = os.path.basename(str(d.get("cwd") or "").rstrip("/"))
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
    url = f"claude://resume?session={sid}" if UUID_RE.match(sid) else None
    return title, sub, msg, url, (sid or None)


def self_test():
    """Send a real notification for your most recent session, then report."""
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
    payload = {"session_id": sid, "transcript_path": path,
               "cwd": os.getcwd(), "permission_mode": "default",
               "message": "Self-test - click me to jump to this session."}
    title, sub, msg, url, group = build(payload)

    have_tn = next((t for t in TN_PATHS if os.access(t, os.X_OK)), None)
    print(f"session   : {sid}")
    print(f"title     : {title}")
    print(f"subtitle  : {sub or '(none)'}")
    print(f"deep link : {url or '(none - session id is not a UUID)'}")
    print(f"icon      : {trigger_icon() or '(none detected)'}")
    print(f"notifier  : {have_tn or 'osascript fallback (no click-to-jump)'}")
    notify(title, sub, msg, url, group)
    print("\nSent. Check Notification Center, then click it.")
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
