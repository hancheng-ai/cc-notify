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
   in the Claude desktop app - and the uuid it takes is a DESKTOP ROW's own id,
   not a CLI session id. The app mints a `local_<uuid>` row per conversation and
   keeps the CLI id in a separate `cliSessionId` field.

   The two are equal for only a minority of rows, so the click resolves the CLI
   id the banner carries to the row that actually holds it. Measured on a real
   44-conversation store: 6 rows had them equal, and those 6 were exactly the
   ones whose banners had ever navigated - the other 38 failed identically every
   time, which read as flakiness rather than the deterministic bug it was.

   Passing an UNRESOLVED CLI id is what created the duplicate rows: it asks the
   app to import a conversation it may already be showing, the import lands
   first, the app writes its own row after, and one conversation ends up with
   two. So a click that cannot resolve a row raises the app instead of guessing.

3. macOS draws the banner's icon, and its Notification Center grouping, from
   the SENDING app - so every tool sharing one terminal-notifier looks alike and
   piles into one group. Neither of its flags fixes that: `-appIcon` is accepted
   then silently ignored, and `-sender` hangs for over 12 seconds and would hand
   the click to that app. So we build a private re-badged copy of the notifier
   carrying our own bundle id and icon.

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
import sys, json, os, re, shlex, plistlib, shutil, subprocess
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
#
# The NATIVE Homebrew prefix has to come first. A machine that has been through
# an Intel-to-Apple-silicon migration carries both, and a fixed Intel-first order
# keeps selecting the x86_64 build even after the arm64 one is installed - so the
# "Support Ending for Intel-based Apps" warning survives the fix for it, which is
# exactly how this was found.
_TN_BREW = ("/opt/homebrew/bin/terminal-notifier",   # Homebrew on Apple silicon
            "/usr/local/bin/terminal-notifier")      # Homebrew on Intel
TN_PATHS = _TN_BREW if (IS_MAC and os.uname().machine == "arm64") else _TN_BREW[::-1]
NOTIFY_SEND_PATHS = ("/usr/bin/notify-send", "/usr/local/bin/notify-send")
POWERSHELL_PATHS = (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",)
MAC_PS = "/bin/ps"


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

def _icon_bearing_bundle(exe):
    """The .app an executable belongs to, if that bundle has its own icon."""
    i = exe.find(".app/")
    if i < 0:
        return None
    # Skip toolchain paths. /usr/bin/python3 actually lives inside
    # Xcode.app/Contents/Developer, so without this every notification would
    # proudly wear the Xcode icon.
    if "/Contents/Developer/" in exe:
        return None
    bundle = exe[:i + 4]
    try:
        with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as f:
            info = plistlib.load(f) or {}
    except Exception:
        return None
    icon = info.get("CFBundleIconFile")
    if not icon:
        return None  # icon-less wrapper; keep walking up to the real host
    if not icon.endswith(".icns"):
        icon += ".icns"
    src = os.path.join(bundle, "Contents", "Resources", icon)
    return src if os.path.isfile(src) else None


def _ancestor_exes():
    """Executable paths from this process upward, nearest first. macOS only."""
    if not IS_MAC:
        return
    pid = os.getppid()  # start at the parent: this process is just the interpreter
    for _ in range(10):  # bounded, so a strange process tree can never spin
        try:
            out = subprocess.run([MAC_PS, "-o", "ppid=,comm=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=2).stdout.strip()
        except Exception:
            return
        # comm can contain spaces ("Application Support"), so split once only.
        parts = out.split(None, 1)
        if len(parts) < 2:
            return
        ppid, exe = parts[0], parts[1].strip()
        yield exe
        if ppid in ("0", "1", str(pid)):
            return
        try:
            pid = int(ppid)
        except ValueError:
            return


def trigger_app_icns():
    """.icns of whichever app triggered this hook, found by walking ancestors.

    Started from the Claude desktop app you get Claude's icon; started from a
    terminal you get Terminal's or iTerm's. macOS only.
    """
    for exe in _ancestor_exes():
        icns = _icon_bearing_bundle(exe)
        if icns:
            return icns
    return None


def launching_surface():
    """Bundle id of the app this session is running inside, or None.

    The point is to tell a desktop-app session from a terminal one, because the
    deep link only ever opens the desktop app. Clicking a banner for a session
    living in iTerm would not return you to it - it would import a COPY into the
    desktop app and take you there instead.

    Walks past `com.anthropic.claude-code`: the CLI ships its own app wrapper, so
    it is the first bundle on the ancestry chain, and it names the program rather
    than the surface hosting it. The next bundle up is the real host.
    """
    for exe in _ancestor_exes():
        i = exe.find(".app/")
        if i < 0 or "/Contents/Developer/" in exe:
            continue
        try:
            with open(os.path.join(exe[:i + 4], "Contents", "Info.plist"), "rb") as f:
                bid = (plistlib.load(f) or {}).get("CFBundleIdentifier")
        except Exception:
            continue
        if bid and bid != CLI_BUNDLE:
            return bid
    return None


# --------------------------------------------------------------------------
# Notifier identity - claim our own slot in Notification Center
# --------------------------------------------------------------------------
#
# macOS draws a notification's left-hand icon, and its Notification Center
# grouping, from the SENDING application. Every tool that shells out to the one
# shared terminal-notifier therefore looks identical and lands in one pile, so
# you cannot tell which of them is asking for you.
#
# Neither flag terminal-notifier offers solves it. `-appIcon` is accepted and
# then silently ignored by current macOS (verified: indistinguishable from no
# flag at all). `-sender` would set the identity properly, but it HANGS - over
# 12 seconds with no return, measured - and would additionally hand the click to
# that app, destroying the deep-link navigation.
#
# What works is giving ourselves a private, re-badged copy of the notifier: same
# binary, our own bundle id, our own icon. Built once, then reused.

REBADGE_ID = "ai.hancheng.cc-notify"
CODESIGN = "/usr/bin/codesign"


def _rebadge_home():
    base = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.expanduser("~/.claude/.cache/cc-notify")
    return os.path.join(base, "notifier")


def _source_notifier_app(tn_binary):
    """Locate terminal-notifier's own .app bundle from its CLI symlink."""
    real = os.path.realpath(tn_binary)
    for candidate in (os.path.join(os.path.dirname(os.path.dirname(real)), "terminal-notifier.app"),
                      os.path.join(os.path.dirname(real), "terminal-notifier.app")):
        if os.path.isdir(candidate):
            return candidate
    return None


def rebadged_notifier(tn_binary):
    """Path to our own notifier binary, building it once if needed.

    Returns None on any problem, so the caller simply falls back to the shared
    terminal-notifier - a wrong icon is much better than no notification.
    """
    # On by default, having been verified to post and display without any
    # authorization prompt. The residual risk is that a fresh bundle id is a
    # sender macOS has not seen before, so if a machine ever refuses it the
    # symptom is silence - CC_NOTIFY_NO_REBADGE=1 is the first thing --doctor
    # tells you to try.
    if not IS_MAC or os.environ.get("CC_NOTIFY_NO_REBADGE"):
        return None
    app = os.path.join(_rebadge_home(), "cc-notify.app")
    binary = os.path.join(app, "Contents", "MacOS", "terminal-notifier")
    if os.access(binary, os.X_OK):
        return binary

    src = _source_notifier_app(tn_binary)
    if not src:
        return None
    try:
        os.makedirs(_rebadge_home(), exist_ok=True)
        if os.path.isdir(app):
            shutil.rmtree(app)
        shutil.copytree(src, app, symlinks=True)

        plist = os.path.join(app, "Contents", "Info.plist")
        with open(plist, "rb") as f:
            info = plistlib.load(f)
        info["CFBundleIdentifier"] = REBADGE_ID
        info["CFBundleName"] = "cc-notify"
        icon_name = info.get("CFBundleIconFile") or "Terminal"
        if not icon_name.endswith(".icns"):
            icon_name += ".icns"
        with open(plist, "wb") as f:
            plistlib.dump(info, f)

        # Wear the icon of whatever launched the session, so the banner looks
        # like what it is about. Sourced from the local machine at runtime and
        # never redistributed.
        icns = trigger_app_icns()
        if icns:
            shutil.copyfile(icns, os.path.join(app, "Contents", "Resources", icon_name))

        # Re-sign after editing the bundle, or macOS records the wrong identity
        # and may refuse to deliver at all.
        subprocess.run([CODESIGN, "--force", "--deep", "--sign", "-", app],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       start_new_session=True, timeout=30)
    except Exception:
        return None
    return binary if os.access(binary, os.X_OK) else None


# --------------------------------------------------------------------------
# Focus suppression - don't banner a session the user is already watching
# --------------------------------------------------------------------------

DESKTOP_BUNDLE = "com.anthropic.claudefordesktop"

# The CLI's own app wrapper. It sits on the ancestry chain below whichever app
# is really hosting the session, so surface detection has to step over it.
CLI_BUNDLE = "com.anthropic.claude-code"

# Conservative bundle-id shape. The value is interpolated into the shell command
# a banner carries, so it is validated rather than trusted.
BUNDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,127}$")
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


def desktop_records():
    """Every desktop session record, as (sessionId, cliSessionId, dict)."""
    out = []
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
                sid, cli = d.get("sessionId"), d.get("cliSessionId")
                if sid and cli:
                    out.append((sid, cli, d))
    except Exception:
        pass
    return out


def desktop_target(session_id):
    """The uuid to put in a resume link for this CLI session, or None.

    The deep link addresses a DESKTOP ROW, not a CLI session. Measured: opening
    `claude://resume?session=<X>` for a row whose sessionId is `local_<X>` lands
    on that row and creates nothing, even though its cliSessionId is a different
    uuid entirely.

    That is the whole bug. The desktop app mints its own `local_<uuid>` per
    conversation and keeps the CLI id in a separate `cliSessionId` field; the two
    are equal for only a minority of rows, and those were exactly the rows whose
    banners ever navigated. Passing the CLI id for any other row asks the app to
    import a conversation it is already showing -- which is what minted the
    untitled duplicates, recognisable by `lastActivityAt == createdAt`.

    Returns None when nothing is tracked yet, and that refusal is deliberate.
    Navigating blind is precisely how the second row appears: the import lands
    first, the app writes its own row afterwards, and one conversation ends up
    with two. A click that cannot resolve a target should raise the app and let
    the owner pick, not guess.
    """
    if not IS_MAC or not session_id:
        return None
    live = []
    for sid, cli, d in desktop_records():
        if cli != session_id or not sid.startswith("local_"):
            continue
        if d.get("isArchived"):
            # Following the link would un-archive it, putting a row back in a
            # list its owner deliberately cleared. Declining matches what this
            # module already chose to do about archived rows.
            continue
        # Coerced, not trusted. These records are written by another program, and
        # one row with a string timestamp beside one with an int makes sort()
        # raise - which would take the whole click down, not just the ordering.
        # foreground_session() already guards the sibling field the same way.
        ts = d.get("lastActivityAt")
        live.append((ts if isinstance(ts, (int, float)) else 0, sid))
    if not live:
        return None
    live.sort(key=lambda r: r[0])
    return live[-1][1][len("local_"):]


def duplicate_pairs():
    """Conversations still showing more than one row in the list.

    Returns (cliSessionId, [live_row_ids]).

    "More than one LIVE row" is the whole definition. An earlier version singled
    out a canonical `local_<cli>` row and only counted the others, because back
    when the click imported blindly, archiving that row did not stick - the next
    click un-archived it. Clicks no longer un-archive anything, so archiving
    either row now resolves the pair, and treating one of them as privileged
    left a tidied conversation being reported as untidy for good.
    """
    by_cli = {}
    for sid, cli, d in desktop_records():
        if not d.get("isArchived"):
            by_cli.setdefault(cli, []).append(sid)
    return [(cli, sorted(rows)) for cli, rows in by_cli.items() if len(rows) > 1]


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

def click_command(url):
    """Shell command to run on click, so the safety check uses CURRENT state.

    `-open URL` bakes its decision in at post time, and banners outlive that
    decision by days: one posted while a session was un-converged still carries
    that answer long after the session converged, and - worse - a banner posted
    by an older version carries a link this version would never have attached.
    Notification Center is effectively a cache of stale decisions.

    So the click routes back through this script, which re-runs the check at the
    moment of the click. See open_url().

    The interpreter is pinned to /usr/bin/python3 rather than sys.executable,
    because sys.executable resolves through PATH to whatever toolchain is
    active - often Xcode's - and that path is baked into every delivered
    banner. Move or update Xcode and yesterday's notifications stop responding
    to clicks. The system shim outlives toolchains.
    """
    py = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
    cmd = "{} {} --open {}".format(
        shlex.quote(py),
        shlex.quote(os.path.abspath(__file__)),
        shlex.quote(url))
    # The hosting surface IS baked in at post time, and unlike the duplicate
    # check that is correct: a session cannot migrate from iTerm to the desktop
    # app, so this answer cannot go stale the way a state check does.
    surface = launching_surface()
    if surface and BUNDLE_RE.match(surface):
        cmd += " --from " + shlex.quote(surface)
    return cmd


def open_url(url, surface=None):
    """Click handler: navigate if that is safe right now, else just focus Claude.

    Deliberately degrades rather than doing nothing. A click that cannot safely
    navigate still brings the app forward - the user wanted to get to Claude -
    it just leaves them to pick the session, instead of minting a row.

    The bare `claude://` is what raises the app, measured. `open -b <bundle>`,
    `open -a`, and AppleScript `activate` all return success and leave the app
    exactly where it was, which is why none of them is used here. Terminals are
    the opposite: `open -b` raises them fine, so a terminal-hosted session gets
    its own app back rather than the deep link.
    """
    m = re.match(r"^claude://resume\?session=([0-9a-fA-F-]{36})$", url or "")
    if not m:
        return 0  # only ever open our own scheme, never arbitrary input
    sid = m.group(1)
    if not UUID_RE.match(sid):
        return 0

    # A session hosted somewhere other than the desktop app must never follow
    # the deep link. `claude://resume` cannot reach back into iTerm; it would
    # import a COPY of a live session into the desktop app and land you on that
    # - a new row for a conversation that is running elsewhere. Note this check
    # comes FIRST: desktop_target() resolves happily for these, correctly in its
    # own terms, but is blind to the host being wrong.
    if surface and BUNDLE_RE.match(surface) and surface != DESKTOP_BUNDLE:
        try:
            subprocess.run(["/usr/bin/open", "-b", surface], timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        return 0

    # Resolve the CLI id the banner carries to the desktop row it actually
    # refers to. None means "nothing resolvable" -- raise the app and let the
    # owner pick, rather than importing a copy of a conversation already on
    # screen. CC_NOTIFY_ALWAYS_DEEPLINK keeps its old meaning: send the link
    # exactly as posted, guard bypassed, for diagnosing this very code.
    # Resolution reads files another program writes, so it is inside the guard
    # too. A click that raises is worse than every failure this module tolerates
    # elsewhere: the banner is already dismissed, so nothing happens at all and
    # there is nothing left to click again. Falling back to the bare scheme at
    # least surfaces the app.
    argv = ["/usr/bin/open", "claude://"]
    try:
        if os.environ.get("CC_NOTIFY_ALWAYS_DEEPLINK"):
            argv = ["/usr/bin/open", url]
        else:
            target = desktop_target(sid)
            if target:
                argv = ["/usr/bin/open", "claude://resume?session=%s" % target]
    except Exception:
        pass  # keep the fallback argv and still open something
    try:
        subprocess.run(argv, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    return 0


def macos_argv(tn, title, sub, msg, url, group):
    """terminal-notifier invocation. Click-to-jump and grouping both supported.

    Note the absence of `-appIcon`: current macOS accepts and then ignores it,
    so it bought nothing while costing an icon conversion on every notification.
    The banner's icon comes from the sending bundle instead - see the notifier
    identity section above.

    The click is wired with `-execute`, not `-open`, so the duplicate check runs
    when the banner is clicked rather than when it was posted - see
    click_command().
    """
    argv = [tn, "-title", title, "-message", msg]
    if sub:
        argv += ["-subtitle", sub]
    if url:
        argv += ["-execute", click_command(url)]
    if group:
        argv += ["-group", group]  # replaces this session's previous banner
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
    """Run a notifier, discarding its output.

    stdout and stderr go to /dev/null rather than into pipes. That is not a
    stylistic choice: a notifier that spawns a helper which inherits the pipe
    keeps subprocess.run blocked long past its timeout, because the timeout
    kills only the direct child while communicate() waits for every writer to
    close. Measured: this turned a 2.6s first launch into a multi-minute hang.
    start_new_session puts the child in its own process group so the timeout can
    take the whole group down instead of orphaning it.

    Discarding is right on its own merits too - stdout beginning with "{" is
    parsed by Claude Code as a control decision, so nothing may leak upward.
    """
    try:
        return subprocess.run(argv, timeout=10, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, start_new_session=True,
                              **kw).returncode == 0
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
        if tn:
            # Prefer our own re-badged copy so the banner carries our identity
            # rather than being pooled with every other terminal-notifier user.
            if _run(macos_argv(rebadged_notifier(tn) or tn, title, sub, msg, url, group)):
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

    if url and os.environ.get("CC_NOTIFY_NO_DEEPLINK"):
        url = None  # click-to-jump off entirely

    # Note what is NOT decided here. Whether following this link would mint a
    # second row is deliberately left to click time, because a banner outlives
    # the state it was posted under. open_url() answers it at the click.
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

    identity = "shared terminal-notifier"
    if IS_MAC:
        tn = _first_executable(TN_PATHS, "terminal-notifier")
        backend = tn or "osascript (no click-to-jump)"
        if tn and rebadged_notifier(tn):
            identity = f"{REBADGE_ID} (own icon and Notification Center group)"
    elif IS_LINUX:
        backend = _first_executable(NOTIFY_SEND_PATHS, "notify-send") or "MISSING - install libnotify"
        identity = "notify-send"
    elif IS_WIN:
        backend = _first_executable(POWERSHELL_PATHS, "powershell", "pwsh") or "MISSING - powershell"
        identity = "PowerShell toast"
    else:
        backend = f"unsupported platform: {sys.platform}"

    print(f"platform  : {sys.platform}")
    print(f"session   : {sid}")
    print(f"title     : {title}")
    print(f"subtitle  : {sub or '(none)'}")
    print(f"deep link : {url or '(not supported on this platform)'}")
    print(f"backend   : {backend}")
    print(f"identity  : {identity}")
    notify(title, sub, msg, url, group)
    print("\nSent. Check your notifications" + (", then click it." if url else "."))
    return 0


def doctor():
    """Report where clicks will land, and any leftover duplicate rows.

    The duplicates are historical: they date from when the click passed an
    unresolved CLI id and the app imported a conversation it already had. They
    no longer affect click-to-jump, which resolves to whichever row is live and
    most recently active, so this is now a tidiness report rather than a fix-me.
    """
    if not IS_MAC:
        print("Duplicate session entries are a macOS desktop-app concern only.")
        return 0
    tn = _first_executable(TN_PATHS, "terminal-notifier")
    ours = rebadged_notifier(tn) if tn else None
    print("notifier identity")
    if ours:
        print(f"  posting as {REBADGE_ID} (own icon, own Notification Center group)")
        print("  Seeing NO notifications at all? That is the first thing to suspect -")
        print("  a bundle id macOS has not authorized is dropped silently. Compare with")
        print("      CC_NOTIFY_NO_REBADGE=1 python3 notify.py --self-test")
        print("  and if that one arrives while the default does not, keep the variable set.")
    else:
        print("  posting as the shared terminal-notifier (re-badge off or unavailable)")
    stale = intel_only_notifier(ours or tn)
    if stale:
        print()
        print(f"  ⚠ that binary is {stale}-only, on an Apple silicon Mac.")
        print("    macOS shows this as \"Support Ending for Intel-based Apps\" naming")
        print("    cc-notify, because the re-badge puts our name on terminal-notifier's")
        print("    binary. The binary is Homebrew's; we only copy and re-sign it.")
        print("    It still works under Rosetta, and will stop when Rosetta goes.")
        print("    Fix: install terminal-notifier from an arm64 Homebrew (/opt/homebrew).")
    print()

    print("assumptions")
    bad = 0
    for name, ok, detail in check_assumptions():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<24} {detail}")
        bad += not ok
    print(f"\n  {bad} failing. These are undocumented interfaces; re-run after a"
          if bad else "\n  All good. These are undocumented interfaces; re-run after a")
    print("  Claude Code update, when drift would otherwise be silent.\n")

    pairs = duplicate_pairs()
    if not pairs:
        print("No duplicate session entries found.")
        return 0

    print(f"{len(pairs)} conversation(s) listed under more than one session entry.\n")
    print("These are leftovers from when clicking passed an unresolved CLI id, which")
    print("asked the app to import a conversation it was already showing. Clicks no")
    print("longer do that: they resolve to a row that exists, or raise the app.\n")
    print("Nothing here breaks click-to-jump - the click lands on whichever of these")
    print("rows was active most recently. Tidy them if the duplicates bother you.\n")
    for cli, rows in pairs:
        landing = desktop_target(cli)
        print(f"conversation {cli}")
        print(f"  click lands on : local_{landing}" if landing else
              "  click lands on : nothing resolvable - raises the app")
        for o in rows:
            if o != (f"local_{landing}" if landing else None):
                print(f"  also listed as : {o}")
        if landing:
            print(f"  tidy: keep local_{landing} - the one clicks land on - and archive")
            print("        the rest. Archiving the landing row instead sends clicks to")
            print("        whatever is left, or to nothing.")
        else:
            print("  tidy: keep whichever row you want, archive the rest.")
        print("        History is shared - all point at the same transcript.")
        print()
    print("Prefer no click-to-jump at all?  export CC_NOTIFY_NO_DEEPLINK=1")
    return 0


def check_assumptions():
    """Assert every undocumented thing this hook depends on, against live data.

    Returns [(name, ok, detail)].

    The point is that this plugin's failures have all been SILENT. Clicking was
    86% broken for weeks and read as flakiness; the Intel warning survived its
    own fix. The unit tests stayed green throughout, because they exercise
    synthetic records - they would pass just as happily if every real assumption
    below had rotted. So these run against the actual store, the actual
    transcripts and the actual installed binaries, and are meant to be re-run
    after Claude Code updates.
    """
    out = []

    def check(name, fn):
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"raised {type(e).__name__}: {e}"
        out.append((name, ok, detail))

    def store():
        if not os.path.isdir(SESSION_STORE):
            return False, f"missing: {SESSION_STORE}"
        n = len(desktop_records())
        return bool(n), f"{n} session record(s) readable"

    def fields():
        recs = desktop_records()
        if not recs:
            return False, "no records to inspect"
        stamped = sum(1 for _, _, d in recs
                      if isinstance(d.get("lastActivityAt"), (int, float)))
        # sessionId/cliSessionId are guaranteed by desktop_records itself, which
        # drops anything lacking them - so a zero count here means the names
        # changed, which is exactly the drift worth catching.
        return bool(stamped), (f"sessionId/cliSessionId on {len(recs)}, "
                               f"numeric lastActivityAt on {stamped}")

    def deep_link():
        clis = {cli for _, cli, _ in desktop_records()}
        if not clis:
            return False, "no conversations tracked"
        hit = sum(1 for c in clis if desktop_target(c))
        pct = 100 * hit // len(clis)
        # A handful legitimately do not resolve - archived-only conversations.
        # A collapse to near zero is the signature of the contract moving.
        return pct >= 50, f"{hit}/{len(clis)} conversations resolve to a row ({pct}%)"

    def titles():
        root = os.path.expanduser("~/.claude/projects")
        newest, best = None, 0
        for dp, _, ns in os.walk(root):
            for n in ns:
                if not n.endswith(".jsonl"):
                    continue
                p = os.path.join(dp, n)
                try:
                    mt = os.path.getmtime(p)
                except OSError:
                    continue
                if mt > best:
                    newest, best = p, mt
        if not newest:
            return False, "no transcripts found"
        t = session_title(newest)
        return bool(t), (f"recovered {t!r}" if t
                         else "no custom-title/ai-title entry in the newest transcript")

    def bundles():
        if not IS_MAC:
            return True, "n/a off macOS"
        found = []
        for bid in (DESKTOP_BUNDLE, CLI_BUNDLE):
            try:
                r = subprocess.run(["/usr/bin/mdfind",
                                    f"kMDItemCFBundleIdentifier == '{bid}'"],
                                   capture_output=True, text=True, timeout=10)
                if (r.stdout or "").strip():
                    found.append(bid)
            except Exception:
                pass
        return bool(found), f"resolved {len(found)}/2: {', '.join(found) or 'none'}"

    def notifier():
        if not IS_MAC:
            return True, "n/a off macOS"
        tn = _first_executable(TN_PATHS, "terminal-notifier")
        if not tn:
            return False, "terminal-notifier not installed - no click, no grouping"
        stale = intel_only_notifier(tn)
        if stale:
            return False, f"{tn} is {stale}-only on Apple silicon"
        return True, tn

    check("session store readable", store)
    check("record field names", fields)
    check("deep-link contract", deep_link)
    check("title recovery", titles)
    check("bundle ids", bundles)
    check("notifier binary", notifier)
    return out


def intel_only_notifier(binary):
    """Arch string if the notifier is Intel-only on Apple silicon, else None.

    Worth naming explicitly because macOS reports it under OUR name: re-badging
    copies terminal-notifier's binary into a bundle carrying our id, so the
    "Support Ending for Intel-based Apps" warning says cc-notify. The binary is
    Homebrew's and we never rebuild it - only an arm64 Homebrew fixes this.
    """
    if not IS_MAC or not binary or os.uname().machine != "arm64":
        return None
    # A Homebrew shim is a shell script; the Mach-O lives inside the .app.
    real = os.path.realpath(binary)
    cands = [real, os.path.join(os.path.dirname(os.path.dirname(real)),
                                "terminal-notifier.app", "Contents", "MacOS",
                                "terminal-notifier")]
    for c in cands:
        try:
            out = subprocess.run(["/usr/bin/file", "-b", c], capture_output=True,
                                 text=True, timeout=5).stdout
        except Exception:
            continue
        if "Mach-O" not in out:
            continue
        return "x86_64" if "x86_64" in out and "arm64" not in out else None
    return None


def clear_banners():
    """Drop our banners from Notification Center.

    Needed once, when upgrading from a version that used `-open`: those banners
    carry a baked-in link this version would not have attached, and there is no
    way to rewrite a delivered notification. Removing them is the only cure.
    """
    if not IS_MAC:
        print("Only needed on macOS.")
        return 0
    tn = _first_executable(TN_PATHS, "terminal-notifier")
    if not tn:
        print("terminal-notifier not found; clear Notification Center by hand.")
        return 1
    for binary in filter(None, (rebadged_notifier(tn), tn)):
        _run([binary, "-remove", "ALL"])
    print("Cleared. Banners posted from now on resolve their link at click time.")
    return 0


def main():
    if "--doctor" in sys.argv:
        return doctor()
    if "--self-test" in sys.argv:
        return self_test()
    if "--clear-banners" in sys.argv:
        return clear_banners()
    if "--open" in sys.argv:
        i = sys.argv.index("--open")
        surface = None
        if "--from" in sys.argv:
            j = sys.argv.index("--from")
            surface = sys.argv[j + 1] if j + 1 < len(sys.argv) else None
        return open_url(sys.argv[i + 1] if i + 1 < len(sys.argv) else "", surface)
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
