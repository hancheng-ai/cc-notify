#!/usr/bin/env python3
"""Installer for claude-code-notify.

Copies the hook into ~/.claude/hooks/ and registers it as a Notification hook in
~/.claude/settings.json, merging into whatever is already there rather than
overwriting it. Your existing hooks are preserved, and the settings file is
backed up before any change.

    python3 install.py              install or update
    python3 install.py --uninstall  remove the hook and its registration
    python3 install.py --dry-run    show what would change, touch nothing
"""
import sys, os, json, shutil, datetime

HOME = os.path.expanduser("~")
HOOKS_DIR = os.path.join(HOME, ".claude", "hooks")
TARGET = os.path.join(HOOKS_DIR, "notify.py")
SETTINGS = os.path.join(HOME, ".claude", "settings.json")
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notify.py")

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform in ("win32", "cygwin")
IS_LINUX = sys.platform.startswith("linux")
DRY = "--dry-run" in sys.argv


def interpreter():
    """Pick the interpreter the hook will be launched with.

    On macOS and Linux prefer the absolute system python: a GUI app spawns hooks
    with a nearly empty PATH, and a virtualenv path would break the moment that
    environment is rebuilt or switched. On Windows there is no such fixed
    location, so the current interpreter is the best available answer.
    """
    if not IS_WIN and os.access("/usr/bin/python3", os.X_OK):
        return "/usr/bin/python3"
    return sys.executable or "python3"


def quoted(p):
    return f'"{p}"' if " " in p else p


COMMAND = f"{quoted(interpreter())} {quoted(TARGET)}"


def say(msg, ok=None):
    print(f"[{ {True: '  ok  ', False: ' warn ', None: '      '}[ok] }] {msg}")


def load_settings():
    if not os.path.isfile(SETTINGS):
        return {}
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as e:
        print(f"\n{SETTINGS} is not valid JSON ({e}).")
        print("Fix or move it first - refusing to touch it.")
        sys.exit(1)


def save_settings(data):
    if DRY:
        say("would write settings.json")
        return
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    if os.path.isfile(SETTINGS):
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = f"{SETTINGS}.bak.{stamp}"
        shutil.copy2(SETTINGS, backup)
        say(f"backed up settings.json -> {os.path.basename(backup)}", True)
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, SETTINGS)  # atomic; never leaves a half-written settings file


def entries(data):
    return data.setdefault("hooks", {}).setdefault("Notification", [])


def mentions_us(block):
    for h in (block or {}).get("hooks", []) or []:
        if "notify.py" in str(h.get("command", "")):
            return True
    return False


def check_dependencies():
    """Report the notification backend this platform will use."""
    if IS_MAC:
        for p in ("/usr/local/bin/terminal-notifier", "/opt/homebrew/bin/terminal-notifier"):
            if os.access(p, os.X_OK):
                return say(f"terminal-notifier: {p}", True)
        if shutil.which("terminal-notifier"):
            return say(f"terminal-notifier: {shutil.which('terminal-notifier')}", True)
        say("terminal-notifier not found", False)
        print("         Notifications still work, but clicking one will not jump")
        print("         to the session. Install with:")
        print("             brew install terminal-notifier")
    elif IS_LINUX:
        found = shutil.which("notify-send")
        if found:
            return say(f"notify-send: {found}", True)
        say("notify-send not found - no notifications will appear", False)
        print("         Install libnotify:")
        print("             sudo apt install libnotify-bin     # Debian/Ubuntu")
        print("             sudo dnf install libnotify         # Fedora")
    elif IS_WIN:
        found = shutil.which("powershell") or shutil.which("pwsh")
        if found:
            return say(f"powershell: {found}", True)
        say("powershell not found - no notifications will appear", False)
    else:
        say(f"unsupported platform: {sys.platform}", False)


def install():
    if not (IS_MAC or IS_LINUX or IS_WIN):
        print(f"Unsupported platform: {sys.platform}")
        return 1
    if not os.path.isfile(SRC):
        print(f"Cannot find {SRC}")
        return 1

    check_dependencies()

    if DRY:
        say(f"would copy notify.py -> {TARGET}")
    else:
        os.makedirs(HOOKS_DIR, exist_ok=True)
        shutil.copy2(SRC, TARGET)
        if not IS_WIN:
            os.chmod(TARGET, 0o755)
        say(f"installed {TARGET}", True)

    data = load_settings()
    lst = entries(data)
    block = {"hooks": [{"type": "command", "command": COMMAND, "timeout": 10}]}

    existing = next((i for i, b in enumerate(lst) if mentions_us(b)), None)
    if existing is not None:
        lst[existing] = block
        say("updated existing Notification hook registration", True)
    else:
        lst.append(block)
        say(f"registered Notification hook ({len(lst)} total)", True)
    save_settings(data)

    print("\nDone. Restart Claude Code, then verify with:")
    print(f"    {interpreter()} {TARGET} --self-test")
    return 0


def uninstall():
    data = load_settings()
    lst = data.get("hooks", {}).get("Notification", [])
    kept = [b for b in lst if not mentions_us(b)]
    removed = len(lst) - len(kept)

    if removed:
        if kept:
            data["hooks"]["Notification"] = kept
        else:
            data["hooks"].pop("Notification", None)
            if not data["hooks"]:
                data.pop("hooks", None)
        save_settings(data)
        say(f"removed {removed} registration(s)", True)
    else:
        say("no registration found in settings.json")

    if os.path.isfile(TARGET):
        if DRY:
            say(f"would delete {TARGET}")
        else:
            os.remove(TARGET)
            say(f"deleted {TARGET}", True)
    print("\nUninstalled. The notification backend was left installed.")
    return 0


if __name__ == "__main__":
    sys.exit(uninstall() if "--uninstall" in sys.argv else install())
