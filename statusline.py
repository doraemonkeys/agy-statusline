# -*- coding: utf-8 -*-
"""
AGY Statusline - Real-time HUD Status Line for Google Antigravity CLI (agy)
"""
import sys
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

# Ensure UTF-8 output across Windows, macOS, and Linux
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")

# ANSI Color Codes
RESET = "\033[0m"
BOLD = "\033[1m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"
BLUE = "\033[34m"
GRAY = "\033[90m"
RED = "\033[31m"


def format_tokens(n):
    """Format token count into human-readable strings (e.g. 1.2k, 630.6k, 1.0M)."""
    try:
        n = float(n)
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}k"
        return str(int(n))
    except Exception:
        return "0"


def format_time_remaining(seconds):
    """Format seconds into concise hours and minutes (e.g. 4h 56m, 23m)."""
    try:
        seconds = float(seconds)
        if seconds <= 0:
            return ""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    except Exception:
        return ""


def make_progress_bar(pct, width=10):
    """Generate a colored progress bar block."""
    try:
        pct = max(0.0, min(100.0, float(pct)))
        filled = int(round((pct / 100.0) * width))
        empty = width - filled
        # Color tiers: Low (Green), Medium (Yellow), High (Red)
        bar_color = GREEN if pct < 60 else (YELLOW if pct < 85 else RED)
        return f"{bar_color}{'█' * filled}{GRAY}{'░' * empty}{RESET}"
    except Exception:
        return f"{'░' * width}"


def get_git_branch(start_path):
    """
    Detect current git branch without spawning a child process.
    Walks up parent directories looking for .git/HEAD or gitdir pointer (worktrees).
    """
    if not start_path:
        return ""
    try:
        cur = os.path.abspath(start_path)
    except Exception:
        return ""

    for _ in range(8):
        git_dir = os.path.join(cur, ".git")
        if os.path.isdir(git_dir):
            head_file = os.path.join(git_dir, "HEAD")
            if os.path.isfile(head_file):
                try:
                    with open(head_file, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read().strip()
                        if content.startswith("ref: refs/heads/"):
                            return content[len("ref: refs/heads/"):]
                        elif len(content) >= 7:
                            return content[:7]
                except Exception:
                    pass
        elif os.path.isfile(git_dir):
            try:
                with open(git_dir, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read().strip()
                    if content.startswith("gitdir:"):
                        real_git_dir = content[len("gitdir:"):].strip()
                        if not os.path.isabs(real_git_dir):
                            real_git_dir = os.path.join(cur, real_git_dir)
                        head_file = os.path.join(real_git_dir, "HEAD")
                        if os.path.isfile(head_file):
                            with open(head_file, "r", encoding="utf-8", errors="ignore") as hf:
                                hcontent = hf.read().strip()
                                if hcontent.startswith("ref: refs/heads/"):
                                    return hcontent[len("ref: refs/heads/"):]
                                elif len(hcontent) >= 7:
                                    return hcontent[:7]
            except Exception:
                pass
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return ""


def _parse_chat_model_metadata(buf):
    """
    Extract (response_model, model_display_name) from a serialized
    CortexStepGeneratorMetadata protobuf blob without external dependencies.
    Field 1 is chat_model (ChatModelMetadata); inside it:
      - Field 19 is response_model (e.g. 'gemini-4-argon-trial', 'gemini-3.8-flash-n')
      - Field 21 is model_display_name (e.g. 'Gemini 4 Argon (High)')
    """
    mv = memoryview(buf)
    n = len(mv)
    i = 0
    while i < n:
        tag = 0
        shift = 0
        while i < n:
            b = mv[i]
            i += 1
            tag |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        fnum = tag >> 3
        wtype = tag & 7
        if wtype == 0:
            while i < n and (mv[i] & 0x80):
                i += 1
            i += 1
        elif wtype == 2:
            ln = 0
            shift = 0
            while i < n:
                b = mv[i]
                i += 1
                ln |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            if fnum == 1:
                sub = mv[i : i + ln]
                m = len(sub)
                j = 0
                resp_model = ""
                disp_name = ""
                while j < m:
                    ctag = 0
                    cshift = 0
                    while j < m:
                        cb = sub[j]
                        j += 1
                        ctag |= (cb & 0x7F) << cshift
                        if not (cb & 0x80):
                            break
                        cshift += 7
                    cfnum = ctag >> 3
                    cwtype = ctag & 7
                    if cwtype == 0:
                        while j < m and (sub[j] & 0x80):
                            j += 1
                        j += 1
                    elif cwtype == 2:
                        cln = 0
                        cshift = 0
                        while j < m:
                            cb = sub[j]
                            j += 1
                            cln |= (cb & 0x7F) << cshift
                            if not (cb & 0x80):
                                break
                            cshift += 7
                        if cfnum == 19:
                            resp_model = bytes(sub[j : j + cln]).decode("utf-8", errors="ignore").strip()
                        elif cfnum == 21:
                            disp_name = bytes(sub[j : j + cln]).decode("utf-8", errors="ignore").strip()
                        j += cln
                    elif cwtype == 5:
                        j += 4
                    elif cwtype == 1:
                        j += 8
                    else:
                        break
                if resp_model or disp_name:
                    return resp_model, disp_name
            i += ln
        elif wtype == 5:
            i += 4
        elif wtype == 1:
            i += 8
        else:
            break
    return "", ""


def get_upstream_response_model(data, expected_display_name=""):
    """
    Query the current conversation SQLite database for the latest upstream
    response_model recorded in gen_metadata.
    """
    if isinstance(data.get("model"), dict) and data["model"].get("response_model"):
        return str(data["model"]["response_model"]).strip()

    conv_id = data.get("conversation_id") or data.get("session_id")
    if not conv_id or not isinstance(conv_id, str):
        return ""

    db_path = os.path.join(
        os.path.expanduser("~/.gemini/antigravity-cli/conversations"),
        f"{conv_id}.db",
    )
    if not os.path.isfile(db_path):
        transcript_path = data.get("transcript_path") or ""
        if transcript_path:
            # Walk up from <config_dir>/brain/<conv_id>/... to <config_dir>
            cur = os.path.abspath(transcript_path)
            for _ in range(6):
                parent = os.path.dirname(cur)
                if os.path.basename(cur) == "brain" and parent:
                    candidate = os.path.join(parent, "conversations", f"{conv_id}.db")
                    if os.path.isfile(candidate):
                        db_path = candidate
                    break
                if parent == cur:
                    break
                cur = parent

    if not os.path.isfile(db_path):
        return ""

    try:
        # Preserve WAL visibility while preventing writes or accidental creation.
        db_uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(db_uri, uri=True, timeout=0.05)) as con:
            rows = con.execute(
                "SELECT data FROM gen_metadata ORDER BY idx DESC LIMIT 5"
            ).fetchall()
        for (blob,) in rows:
            if not blob:
                continue
            resp_model, disp_name = _parse_chat_model_metadata(blob)
            # If the user just switched models in the UI and hasn't sent a turn
            # with the new model yet, avoid displaying the previous model's ID.
            if disp_name and expected_display_name and disp_name != expected_display_name:
                return ""
            if resp_model:
                return resp_model
    except Exception:
        pass
    return ""


def render_statusline(data):
    """Render the 2-line HUD status string from Antigravity state data."""
    # === 1. Line 1: Model, Directory, Git Branch, Agent State ===
    line1_items = []

    # (1) Model Name & Upstream Response Model
    model_info = data.get("model")
    model_name = "Gemini"
    if isinstance(model_info, dict):
        model_name = model_info.get("display_name") or model_info.get("id") or "Gemini"
    elif isinstance(model_info, str) and model_info.strip():
        model_name = model_info.strip()

    # Simplify model name (e.g. Gemini 3.8 Flash (High) -> 3.8 Flash (High))
    clean_model = model_name.replace("Gemini", "").replace("Google", "").strip()
    if not clean_model:
        clean_model = model_name

    response_model = get_upstream_response_model(data, expected_display_name=model_name)
    model_item = f"🤖 {CYAN}{BOLD}{clean_model}{RESET}"
    if response_model:
        model_item += f" {GRAY}[{response_model}]{RESET}"
    line1_items.append(model_item)

    # (2) Working Directory
    cwd = data.get("cwd") or (data.get("workspace", {}) or {}).get("current_dir") or ""
    if cwd:
        dir_name = os.path.basename(os.path.abspath(cwd)) if os.path.exists(cwd) else os.path.basename(cwd)
        if not dir_name:
            dir_name = cwd
        line1_items.append(f"📁 {YELLOW}{dir_name}{RESET}")

    # (3) Git Branch
    branch = (data.get("vcs") or {}).get("branch") or get_git_branch(cwd or os.getcwd())
    if branch:
        line1_items.append(f"🌿 {MAGENTA}{branch}{RESET}")

    # (4) Agent State
    agent_state = data.get("agent_state") or "Idle"
    state_color = GREEN if agent_state.lower() in ("idle", "ready") else YELLOW
    line1_items.append(f"⚡ {state_color}{agent_state}{RESET}")

    line1 = f" {GRAY}│{RESET} ".join(line1_items)

    # === 2. Line 2: Context Window progress bar, 5h Quota, Weekly Quota ===
    line2_items = []

    # (1) Context Window & Progress Bar
    cw = data.get("context_window")
    if not isinstance(cw, dict):
        cw = {}

    input_tokens = cw.get("total_input_tokens") or 0
    output_tokens = cw.get("total_output_tokens") or 0
    try:
        total_tokens = float(input_tokens) + float(output_tokens)
    except Exception:
        total_tokens = 0

    max_tokens = cw.get("context_window_size") or 1_000_000
    try:
        max_tokens = float(max_tokens)
    except Exception:
        max_tokens = 1_000_000.0

    used_pct = cw.get("used_percentage")
    if used_pct is None:
        used_pct = (total_tokens / max_tokens * 100) if max_tokens > 0 else 0.0
    else:
        try:
            used_pct = float(used_pct)
        except Exception:
            used_pct = 0.0

    progress_bar = make_progress_bar(used_pct, width=10)
    line2_items.append(
        f"🧠 {BOLD}Context{RESET} {progress_bar} {format_tokens(total_tokens)}/{format_tokens(max_tokens)} ({used_pct:.1f}%)"
    )

    # (2) Quotas: 5h & Weekly
    quotas = data.get("quota") or data.get("quotas") or {}
    five_hour_info = None
    weekly_info = None

    if isinstance(quotas, dict):
        for key, val in quotas.items():
            if not isinstance(val, dict):
                continue
            k = str(key).lower()
            if "five_hour" in k or "5h" in k or "short" in k:
                five_hour_info = val
            elif "week" in k or "7d" in k or "long" in k:
                weekly_info = val
            elif "remaining_fraction" in val:
                if five_hour_info is None:
                    five_hour_info = val
                elif weekly_info is None:
                    weekly_info = val

    if isinstance(five_hour_info, dict):
        rem_frac = five_hour_info.get("remaining_fraction")
        if rem_frac is not None:
            try:
                pct = float(rem_frac) * 100
                res_sec = five_hour_info.get("reset_in_seconds", 0)
                res_str = f" {GRAY}({format_time_remaining(res_sec)}){RESET}" if res_sec else ""
                color = GREEN if pct > 30 else (YELLOW if pct > 10 else RED)
                line2_items.append(f"⏳ 5h: {color}{pct:.0f}%{RESET}{res_str}")
            except Exception:
                pass

    if isinstance(weekly_info, dict):
        rem_frac = weekly_info.get("remaining_fraction")
        if rem_frac is not None:
            try:
                pct = float(rem_frac) * 100
                res_sec = weekly_info.get("reset_in_seconds", 0)
                res_str = f" {GRAY}({format_time_remaining(res_sec)}){RESET}" if res_sec else ""
                color = GREEN if pct > 30 else (YELLOW if pct > 10 else RED)
                line2_items.append(f"📅 Wk: {color}{pct:.0f}%{RESET}{res_str}")
            except Exception:
                pass

    line2 = f" {GRAY}│{RESET} ".join(line2_items)

    return f"{line1}\n{line2}"


def main():
    # Preview / test mode
    if len(sys.argv) > 1 and sys.argv[1] in ("--test", "-t", "--preview"):
        preview_data = {
            "model": {
                "display_name": "Gemini 4 Argon (High)",
                "response_model": "gemini-4-argon-trial"
            },
            "cwd": os.getcwd(),
            "vcs": {"branch": get_git_branch(os.getcwd()) or "main"},
            "agent_state": "reviewing",
            "context_window": {
                "total_input_tokens": 239000,
                "total_output_tokens": 5600,
                "context_window_size": 1000000,
                "used_percentage": 24.5
            },
            "quota": {
                "five_hour": {
                    "remaining_fraction": 0.95,
                    "reset_in_seconds": 17760
                },
                "weekly": {
                    "remaining_fraction": 0.99,
                    "reset_in_seconds": 604560
                }
            }
        }
        print(render_statusline(preview_data))
        return

    try:
        raw = sys.stdin.read()
        if not raw or not raw.strip():
            print("🧠 Context: Ready")
            return
        data = json.loads(raw)
        if not isinstance(data, dict):
            print("🧠 Context: Ready")
            return
    except Exception:
        print("🧠 Context: Ready")
        return

    try:
        print(render_statusline(data))
    except Exception:
        print("🧠 Context: Ready")


if __name__ == "__main__":
    main()
