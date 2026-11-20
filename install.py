# -*- coding: utf-8 -*-
"""
AGY Statusline Installer
Installs and configures agy-statusline for Google Antigravity CLI (agy).
"""
import os
import sys
import json
import shutil
import stat
import tempfile
import time
import subprocess
import shlex

# Ensure UTF-8 console output
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Characters that cmd.exe interprets as delimiters, operators, quotes, or
# variable/wildcard syntax when unquoted on a `cmd /c` command line.
_WINDOWS_CMD_UNSAFE_CHARS = frozenset('"&|<>()^%!;,=*?')
_WINDOWS_CMD_ESCAPE_CHARS = frozenset(" ^&|<>(),;=")


def get_default_config_dir():
    """Get the default Antigravity CLI directory."""
    return os.path.expanduser("~/.gemini/antigravity-cli")


def _is_windows_cmd_token_safe(token):
    """Return True if token can be passed unquoted to `cmd /c` without shell mangling."""
    if not token or token.startswith("@") or token.endswith("\\"):
        return False
    return not any(
        ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F or ch in _WINDOWS_CMD_UNSAFE_CHARS
        for ch in token
    )


def _atomic_write_bytes(path, data):
    """Atomically write bytes via a same-directory temp file, skipping unchanged files."""
    try:
        with open(path, "rb") as existing:
            if existing.read() == data:
                return
    except OSError:
        pass

    target = os.path.abspath(path)
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)

    existing_mode = None
    if os.name != "nt":
        try:
            existing_mode = stat.S_IMODE(os.stat(target).st_mode)
        except OSError:
            pass

    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(target)}.",
        suffix=".tmp",
        dir=parent,
    )
    try:
        with os.fdopen(fd, "wb") as tmp_file:
            tmp_file.write(data)
            tmp_file.flush()
        if existing_mode is not None:
            os.chmod(tmp_path, existing_mode)
        for attempt in range(5):
            try:
                os.replace(tmp_path, target)
                tmp_path = None
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _atomic_write_text(path, text):
    """Atomically write UTF-8 text without newline translation."""
    _atomic_write_bytes(path, text.encode("utf-8"))


def get_statusline_command(target_script):
    """Pin this interpreter and adapt quoting to the CLI's command runner."""
    executable = os.path.realpath(sys.executable)
    target_script = os.path.abspath(target_script)
    if os.name != "nt":
        return shlex.join([executable, "-S", target_script])

    # Invoke Python directly whenever both paths are safe unquoted tokens for
    # `cmd /c`. This avoids creating a `.cmd` batch wrapper that `cmd.exe`
    # repeatedly opens/closes on every refresh and exposes to AV file locks.
    if _is_windows_cmd_token_safe(executable) and _is_windows_cmd_token_safe(target_script):
        return f"{executable} -S {target_script}"

    # The Windows CLI passes the command through Go's argv quoting to cmd /c.
    # Embedded double quotes become literal backslash-quotes. When either path
    # needs quoting or cmd escaping, keep those quotes inside a minimal batch
    # launcher instead of in the configured command.
    launcher = os.path.splitext(target_script)[0] + ".cmd"
    script_name = os.path.basename(target_script).replace("%", "%%")
    escaped_exe = executable.replace("%", "%%")
    lines = []
    if not (executable + script_name).isascii():
        lines.append("@chcp 65001 >nul")
    lines.append(
        f'@setlocal DisableDelayedExpansion & "{escaped_exe}" -S "%~dp0{script_name}" %*'
    )
    _atomic_write_bytes(launcher, ("\r\n".join(lines) + "\r\n").encode("utf-8"))

    return "".join(
        "^" + char if char in _WINDOWS_CMD_ESCAPE_CHARS else char
        for char in launcher
    )


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
    with open(source_script, "rb") as f:
        _atomic_write_bytes(target_script, f.read())
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

    _atomic_write_text(
        settings_file,
        json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
    )

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
        _atomic_write_text(
            settings_file,
            json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
        )
        print(f"✅ Removed statusLine configuration from {settings_file}")
    else:
        print("ℹ️ statusLine was not configured.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--uninstall", "-u"):
        uninstall()
    else:
        install()
