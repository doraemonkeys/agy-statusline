# -*- coding: utf-8 -*-
"""
AGY Statusline Installer
Installs and configures agy-statusline for Google Antigravity CLI (agy).
"""
import os
import sys
import json
import shutil
import time
import subprocess
import shlex

# Ensure UTF-8 console output
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def get_default_config_dir():
    """Get the default Antigravity CLI directory."""
    return os.path.expanduser("~/.gemini/antigravity-cli")


def get_statusline_command(target_script):
    """Pin this interpreter and adapt quoting to the CLI's command runner."""
    executable = os.path.realpath(sys.executable)
    target_script = os.path.abspath(target_script)
    if os.name != "nt":
        return shlex.join([executable, "-S", target_script])

    # The Windows CLI passes the command through Go's argv quoting to cmd /c.
    # Embedded double quotes become literal backslash-quotes. Keep those
    # quotes inside a batch launcher instead of in the configured command.
    launcher = os.path.splitext(target_script)[0] + ".cmd"
    script_name = os.path.basename(target_script).replace("%", "%%")
    with open(launcher, "w", encoding="utf-8", newline="") as f:
        f.write("@echo off\r\nsetlocal DisableDelayedExpansion\r\n")
        if not (executable + script_name).isascii():
            f.write("chcp 65001 >nul\r\n")
        f.write(f'"{executable.replace("%", "%%")}" -S "%~dp0{script_name}" %*\r\n')

    return "".join("^" + char if char in " ^&|<>()" else char for char in launcher)


def run_statusline_command(command, **kwargs):
    """Exercise the same shell boundary used by the CLI, including on Windows."""
    if os.name == "nt":
        shell = os.environ.get("COMSPEC", "cmd.exe")
        args = [shell, "/c", command]
    else:
        args = ["/bin/sh", "-c", command]
    return subprocess.run(args, **kwargs)


def install():
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    source_script = os.path.join(repo_dir, "statusline.py")

    if not os.path.isfile(source_script):
        print(f"❌ Error: Could not find statusline.py in {repo_dir}")
        sys.exit(1)

    config_dir = get_default_config_dir()
    os.makedirs(config_dir, exist_ok=True)
    target_script = os.path.join(config_dir, "statusline.py")
    settings_file = os.path.join(config_dir, "settings.json")

    # Refuse to replace unreadable settings with an empty configuration.
    settings = {}
    if os.path.isfile(settings_file):
        with open(settings_file, "r", encoding="utf-8") as f:
            settings = json.load(f)
        if not isinstance(settings, dict):
            raise ValueError("settings.json must contain a JSON object")

    print(f"📦 Copying statusline.py to {target_script} ...")
    shutil.copy2(source_script, target_script)
    command_str = get_statusline_command(target_script)

    # Validate the installed command itself before publishing it to settings.
    # A direct Python preview would miss shell quoting and PATH failures.
    print("\n🔍 Checking the installed statusline command:")
    preview = run_statusline_command(
        command_str, input="{}", encoding="utf-8", capture_output=True,
        check=True, timeout=5,
    )
    print(preview.stdout, end="")

    if os.path.isfile(settings_file):
        backup_file = f"{settings_file}.bak.{time.time_ns()}"
        shutil.copy2(settings_file, backup_file)
        print(f"🛡️  Backup created: {backup_file}")

    settings["statusLine"] = {
        "type": "command",
        "command": command_str,
        "enabled": True
    }

    with open(settings_file, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"✅ Successfully updated {settings_file}")
    print(f"✨ StatusLine command set to: {command_str}")
    print("\n🎉 Installation complete! Restart or run `agy` to see your new statusline.")


def uninstall():
    config_dir = get_default_config_dir()
    settings_file = os.path.join(config_dir, "settings.json")

    if not os.path.isfile(settings_file):
        print("ℹ️ No settings.json found, nothing to uninstall.")
        return

    try:
        with open(settings_file, "r", encoding="utf-8") as f:
            settings = json.load(f)
    except Exception as e:
        print(f"❌ Error reading settings.json: {e}")
        return

    if "statusLine" in settings:
        del settings["statusLine"]
        with open(settings_file, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"✅ Removed statusLine configuration from {settings_file}")
    else:
        print("ℹ️ statusLine was not configured.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--uninstall", "-u"):
        uninstall()
    else:
        install()
