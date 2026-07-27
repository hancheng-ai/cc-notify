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
import sys, os, json, shutil, subprocess, datetime

HOME = os.path.expanduser("~")
HOOKS_DIR = os.path.join(HOME, ".claude", "hooks")
TARGET = os.path.join(HOOKS_DIR, "notify.py")
SETTINGS = os.path.join(HOME, ".claude", "settings.json")
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notify.py")
COMMAND = f"/usr/bin/python3 {TARGET}"
TN_PATHS = ("/usr/local/bin/terminal-notifier", "/opt/homebrew/bin/terminal-notifier")

DRY = "--dry-run" in sys.argv


def say(msg, ok=None):
    mark = {True: "  ok  ", False: " warn ", None: "      "}[ok]
    print(f"[{mark}] {msg}")


def load_settings():
    if not os.path.isfile(SETTINGS):
        return {}
    try:
        with open(SETTINGS) as f:
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
    with open(tmp, "w") as f:
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


def check_terminal_notifier():
    found = next((p for p in TN_PATHS if os.access(p, os.X_OK)), None)
    if found:
        say(f"terminal-notifier: {found}", True)
        return
    say("terminal-notifier not found", False)
    print("         Without it you still get notifications, but clicking one")
    print("         will not jump to the session. Install with:")
    print("             brew install terminal-notifier")


def install():
    if sys.platform != "darwin":
        print("macOS only - this builds on macOS notifications.")
        return 1
    if not os.path.isfile(SRC):
        print(f"Cannot find {SRC}")
        return 1

    check_terminal_notifier()

    if DRY:
        say(f"would copy notify.py -> {TARGET}")
    else:
        os.makedirs(HOOKS_DIR, exist_ok=True)
        shutil.copy2(SRC, TARGET)
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
    print(f"    python3 {TARGET} --self-test")
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
    print("\nUninstalled. terminal-notifier was left alone "
          "(brew uninstall terminal-notifier if you want it gone).")
    return 0


if __name__ == "__main__":
    sys.exit(uninstall() if "--uninstall" in sys.argv else install())
