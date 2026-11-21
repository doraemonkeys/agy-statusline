# -*- coding: utf-8 -*-
"""
AGY Statusline - Real-time HUD Status Line for Google Antigravity CLI (agy)
"""
import sys
import json
import math
import os
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

# Ensure UTF-8 output across Windows, macOS, and Linux
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

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


def format_duration(seconds):
    """Format elapsed seconds into a compact stopwatch string (e.g. 42s, 3m 05s, 1h 24m, 2d 3h)."""
    if seconds is None or isinstance(seconds, bool):
        return ""
    try:
        val = float(seconds)
    except Exception:
        return ""
    if not math.isfinite(val) or val < 0:
        return ""
    total = int(val)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        minutes, secs = divmod(total, 60)
        return f"{minutes}m {secs:02d}s"
    if total < 86400:
        hours, rem = divmod(total, 3600)
        minutes = rem // 60
        return f"{hours}h {minutes:02d}m"
    days, rem = divmod(total, 86400)
    hours = rem // 3600
    return f"{days}d {hours}h"


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


# Antigravity trajectory SQLite constants (CortexStepType / CortexStepStatus)
_STEP_TYPE_USER_INPUT = 14
_ACTIVE_STEP_STATUSES = (1, 2)  # PENDING, RUNNING
_IDLE_AGENT_STATES = ("idle", "ready")
_UNSET = object()


def _read_varint(mv, i, n):
    """Decode a base-128 protobuf varint from mv[i:n]. Returns (value, next_i) or (None, n)."""
    val = 0
    shift = 0
    while i < n:
        b = mv[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, i
        shift += 7
        if shift >= 70:
            return None, n
    return None, n


def _iter_proto_fields(buf):
    """Yield (field_number, wire_type, value_or_slice) from a protobuf wire buffer."""
    if not buf:
        return
    mv = buf if isinstance(buf, memoryview) else memoryview(buf)
    n = len(mv)
    i = 0
    while i < n:
        tag, i = _read_varint(mv, i, n)
        if tag is None or tag == 0:
            break
        fnum = tag >> 3
        wtype = tag & 7
        if fnum == 0:
            break
        if wtype == 0:
            val, i = _read_varint(mv, i, n)
            if val is None:
                break
            yield fnum, 0, val
        elif wtype == 2:
            ln, i = _read_varint(mv, i, n)
            if ln is None or i + ln > n:
                break
            yield fnum, 2, mv[i : i + ln]
            i += ln
        elif wtype == 5:
            if i + 4 > n:
                break
            i += 4
        elif wtype == 1:
            if i + 8 > n:
                break
            i += 8
        else:
            break


def _parse_proto_timestamp(buf):
    """Parse google.protobuf.Timestamp into Unix epoch float seconds, or None."""
    seconds = 0
    nanos = 0
    has_seconds = False
    for fnum, wtype, val in _iter_proto_fields(buf):
        if wtype == 0:
            if fnum == 1:
                if val <= 0 or val >= (1 << 62):
                    return None
                seconds = val
                has_seconds = True
            elif fnum == 2 and 0 <= val < 1_000_000_000:
                nanos = val
    return (seconds + nanos / 1_000_000_000.0) if has_seconds else None


def _parse_trajectory_created_at(buf):
    """Extract conversation created_at timestamp (Field 2) from TrajectoryMetadata."""
    for fnum, wtype, val in _iter_proto_fields(buf):
        if fnum == 2 and wtype == 2:
            ts = _parse_proto_timestamp(val)
            if ts is not None:
                return ts
    return None


def _parse_step_timestamps(buf):
    """
    Extract (created_at, latest_at) Unix timestamps from CortexStepMetadata.
    Field 1 is created_at; Fields 6 and 32 record step update / completion timestamps.
    """
    created_at = None
    latest_at = None
    for fnum, wtype, val in _iter_proto_fields(buf):
        if wtype == 2 and fnum in (1, 6, 32):
            ts = _parse_proto_timestamp(val)
            if ts is not None:
                if fnum == 1 and created_at is None:
                    created_at = ts
                if latest_at is None or ts > latest_at:
                    latest_at = ts
    return created_at, latest_at


def _parse_chat_model_metadata(buf):
    """
    Extract (response_model, model_display_name) from a serialized
    CortexStepGeneratorMetadata protobuf blob without external dependencies.
    Field 1 is chat_model (ChatModelMetadata); inside it:
      - Field 19 is response_model (e.g. 'gemini-4-argon-trial', 'gemini-3.8-flash-n')
      - Field 21 is model_display_name (e.g. 'Gemini 4 Argon (High)')
    """
    for fnum, wtype, val in _iter_proto_fields(buf):
        if fnum == 1 and wtype == 2:
            resp_model = ""
            disp_name = ""
            for cfnum, cwtype, cval in _iter_proto_fields(val):
                if cwtype == 2:
                    if cfnum == 19:
                        resp_model = bytes(cval).decode("utf-8", errors="ignore").strip()
                    elif cfnum == 21:
                        disp_name = bytes(cval).decode("utf-8", errors="ignore").strip()
            if resp_model or disp_name:
                return resp_model, disp_name
    return "", ""


def _resolve_conversation_db_path(data):
    """Locate the current conversation SQLite database path, or return empty string."""
    conv_id = data.get("conversation_id") or data.get("session_id")
    if not conv_id or not isinstance(conv_id, str):
        return ""
    conv_id = conv_id.strip()
    if not conv_id or "/" in conv_id or "\\" in conv_id or conv_id in (".", ".."):
        return ""

    db_path = os.path.join(
        os.path.expanduser("~/.gemini/antigravity-cli/conversations"),
        f"{conv_id}.db",
    )
    if not os.path.isfile(db_path):
        transcript_path = data.get("transcript_path") or ""
        if isinstance(transcript_path, str) and transcript_path.strip():
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

    return db_path if os.path.isfile(db_path) else ""


def _query_conversation_db(db_path, need_model=True, expected_display_name="", need_timing=True):
    """
    Read upstream model metadata and conversation/turn timestamps from the
    conversation SQLite database in a single read-only connection.
    """
    result = {
        "response_model": "",
        "session_start_ts": None,
        "turn_start_ts": None,
        "turn_end_ts": None,
        "has_running_step": False,
    }
    if not db_path:
        return result

    try:
        # Preserve WAL visibility while preventing writes or accidental creation.
        db_uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(db_uri, uri=True, timeout=0.05)) as con:
            if need_model:
                try:
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
                            break
                        if resp_model:
                            result["response_model"] = resp_model
                            break
                except sqlite3.Error:
                    pass

            if need_timing:
                session_start_ts = None
                try:
                    tmeta_row = con.execute(
                        "SELECT data FROM trajectory_metadata_blob LIMIT 1"
                    ).fetchone()
                    if tmeta_row and tmeta_row[0]:
                        session_start_ts = _parse_trajectory_created_at(tmeta_row[0])
                except sqlite3.Error:
                    pass

                try:
                    first_row = con.execute(
                        "SELECT metadata FROM steps ORDER BY idx ASC LIMIT 1"
                    ).fetchone()
                    if first_row and first_row[0] and session_start_ts is None:
                        first_created, _ = _parse_step_timestamps(first_row[0])
                        if first_created is not None:
                            session_start_ts = first_created

                    if first_row is not None:
                        turn_row = con.execute(
                            "SELECT idx, metadata FROM steps WHERE step_type = ? ORDER BY idx DESC LIMIT 1",
                            (_STEP_TYPE_USER_INPUT,),
                        ).fetchone()
                        if turn_row is None:
                            turn_row = con.execute(
                                "SELECT idx, metadata FROM steps ORDER BY idx ASC LIMIT 1"
                            ).fetchone()

                        if turn_row is not None:
                            turn_idx, turn_meta = turn_row
                            turn_start_ts, turn_latest = _parse_step_timestamps(turn_meta)
                            turn_end_ts = turn_latest
                            earliest_step_ts = turn_start_ts
                            has_running_step = False

                            tail_rows = con.execute(
                                "SELECT status, metadata FROM steps WHERE idx >= ? ORDER BY idx DESC LIMIT 64",
                                (turn_idx,),
                            ).fetchall()
                            for status, meta in tail_rows:
                                if status in _ACTIVE_STEP_STATUSES:
                                    has_running_step = True
                                step_created, step_latest = _parse_step_timestamps(meta)
                                if step_created is not None and (
                                    earliest_step_ts is None or step_created < earliest_step_ts
                                ):
                                    earliest_step_ts = step_created
                                if step_latest is not None and (
                                    turn_end_ts is None or step_latest > turn_end_ts
                                ):
                                    turn_end_ts = step_latest

                            if len(tail_rows) == 64 and not has_running_step:
                                running_row = con.execute(
                                    "SELECT 1 FROM steps WHERE idx >= ? AND status IN (?, ?) LIMIT 1",
                                    (turn_idx, *_ACTIVE_STEP_STATUSES),
                                ).fetchone()
                                if running_row is not None:
                                    has_running_step = True

                            if turn_start_ts is None:
                                turn_start_ts = earliest_step_ts
                            if session_start_ts is None and turn_start_ts is not None:
                                session_start_ts = turn_start_ts

                            result["turn_start_ts"] = turn_start_ts
                            result["turn_end_ts"] = turn_end_ts
                            result["has_running_step"] = has_running_step
                except sqlite3.Error:
                    pass

                result["session_start_ts"] = session_start_ts
    except Exception:
        pass
    return result


def get_upstream_response_model(data, expected_display_name=""):
    """
    Query the current conversation SQLite database for the latest upstream
    response_model recorded in gen_metadata.
    """
    if isinstance(data.get("model"), dict) and data["model"].get("response_model"):
        return str(data["model"]["response_model"]).strip()

    db_path = _resolve_conversation_db_path(data)
    if not db_path:
        return ""

    info = _query_conversation_db(
        db_path,
        need_model=True,
        expected_display_name=expected_display_name,
        need_timing=False,
    )
    return info["response_model"]


def _coerce_nonneg_seconds(val, scale=1.0):
    """Convert a numeric duration value into non-negative seconds, or None."""
    if val is None or isinstance(val, bool):
        return None
    try:
        sec = float(val) * scale
    except Exception:
        return None
    if not math.isfinite(sec) or sec < 0:
        return None
    return sec


def _extract_json_durations(data):
    """Extract explicit (turn_seconds, session_seconds) from JSON payload if present."""
    turn_obj = data.get("turn") if isinstance(data.get("turn"), dict) else {}
    session_obj = data.get("session") if isinstance(data.get("session"), dict) else {}
    cost_obj = data.get("cost") if isinstance(data.get("cost"), dict) else {}

    turn_candidates = (
        (turn_obj.get("duration_seconds"), 1.0),
        (turn_obj.get("duration_ms"), 0.001),
        (turn_obj.get("durationMs"), 0.001),
        (session_obj.get("turn_duration_seconds"), 1.0),
        (session_obj.get("turn_duration_ms"), 0.001),
        (session_obj.get("turnDurationMs"), 0.001),
        (cost_obj.get("turn_duration_ms"), 0.001),
    )
    turn_sec = None
    for raw_val, scale in turn_candidates:
        parsed = _coerce_nonneg_seconds(raw_val, scale)
        if parsed is not None:
            turn_sec = parsed
            break

    session_candidates = (
        (session_obj.get("duration_seconds"), 1.0),
        (session_obj.get("duration_ms"), 0.001),
        (session_obj.get("durationMs"), 0.001),
        (cost_obj.get("total_duration_ms"), 0.001),
        (cost_obj.get("duration_ms"), 0.001),
        (data.get("duration_seconds"), 1.0),
        (data.get("total_duration_ms"), 0.001),
    )
    session_sec = None
    for raw_val, scale in session_candidates:
        parsed = _coerce_nonneg_seconds(raw_val, scale)
        if parsed is not None:
            session_sec = parsed
            break

    return turn_sec, session_sec


def get_work_durations(data, is_state_active=False, _db_info=_UNSET, _now=None):
    """
    Resolve (turn_seconds, session_seconds, is_turn_active) for the statusline.
    Combines explicit JSON telemetry with conversation trajectory timestamps.
    """
    now_ts = time.time() if _now is None else float(_now)
    json_turn, json_session = _extract_json_durations(data)

    db_info = _db_info
    if db_info is _UNSET:
        db_info = None
        need_db = (
            json_turn is None
            or json_turn <= 0
            or json_session is None
            or json_session <= 0
        )
        if need_db:
            db_path = _resolve_conversation_db_path(data)
            if db_path:
                db_info = _query_conversation_db(db_path, need_model=False, need_timing=True)

    db_turn = None
    db_session = None
    has_running_step = False
    if isinstance(db_info, dict):
        has_running_step = bool(db_info.get("has_running_step"))
        session_start_ts = db_info.get("session_start_ts")
        turn_start_ts = db_info.get("turn_start_ts")
        turn_end_ts = db_info.get("turn_end_ts")

        if session_start_ts is not None:
            db_session = max(0.0, now_ts - session_start_ts)

        is_active = bool(is_state_active or has_running_step)
        if turn_start_ts is not None:
            if is_active:
                db_turn = max(0.0, now_ts - turn_start_ts)
            else:
                end_ts = turn_end_ts if turn_end_ts is not None else turn_start_ts
                db_turn = max(0.0, end_ts - turn_start_ts)
        elif session_start_ts is not None:
            # Trajectory initialized before the first step is persisted.
            db_turn = max(0.0, now_ts - session_start_ts) if is_active else 0.0

    is_turn_active = bool(is_state_active or has_running_step)
    turn_sec = json_turn if (json_turn is not None and (json_turn > 0 or db_turn is None)) else db_turn
    session_sec = (
        json_session
        if (json_session is not None and (json_session > 0 or db_session is None))
        else db_session
    )

    if turn_sec is not None and session_sec is not None:
        session_sec = max(session_sec, turn_sec)

    return turn_sec, session_sec, is_turn_active


_QUOTA_FIVE_HOUR_TOKENS = frozenset({"5h", "fivehour"})
_QUOTA_WEEKLY_TOKENS = frozenset({"weekly", "week", "7d"})
_QUOTA_FALLBACK_FIVE_HOUR_TOKENS = frozenset({"short"})
_QUOTA_FALLBACK_WEEKLY_TOKENS = frozenset({"long"})
_QUOTA_NOISE_TOKENS = frozenset({"quota", "quotas", "limit", "limits", "window", "bucket", "term"})

_THIRD_PARTY_MODEL_TOKENS = frozenset({
    "claude",
    "opus",
    "sonnet",
    "haiku",
    "anthropic",
    "gpt",
    "openai",
    "chatgpt",
    "o1",
    "o3",
    "o4",
    "deepseek",
    "llama",
    "mistral",
    "qwen",
    "grok",
})
_THIRD_PARTY_MODEL_PREFIXES = ("claude", "gpt")
_GEMINI_MODEL_TOKENS = frozenset({"gemini", "orchestrator", "gemma"})

_THIRD_PARTY_GROUP_ALIASES = ("3p", "third-party", "thirdparty")
_GEMINI_FLASH_GROUP_ALIASES = ("gemini-flash", "flash", "gemini")
_GEMINI_PRO_GROUP_ALIASES = ("gemini-pro", "pro", "gemini")


def _tokenize_identifier(text):
    """Split an identifier or display label into lowercase alphanumeric tokens."""
    if not text or not isinstance(text, str):
        return ()
    tokens = []
    cur = []
    for ch in text.lower():
        if ch.isalnum():
            cur.append(ch)
        elif cur:
            tokens.append("".join(cur))
            cur = []
    if cur:
        tokens.append("".join(cur))
    return tuple(tokens)


def _parse_quota_key(key):
    """
    Parse a quota bucket key into (group_name, window_name).
    Examples:
      '3p-5h'           -> ('3p', 'five_hour')
      'gemini-weekly'   -> ('gemini', 'weekly')
      'gemini-flash-5h' -> ('gemini-flash', 'five_hour')
      'five_hour'       -> ('', 'five_hour')
    """
    tokens = _tokenize_identifier(str(key))
    window = None
    remaining = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "five" and i + 1 < len(tokens) and tokens[i + 1] == "hour":
            if window is None:
                window = "five_hour"
            i += 2
            continue
        if tok in _QUOTA_FIVE_HOUR_TOKENS:
            if window is None:
                window = "five_hour"
            i += 1
            continue
        if tok in _QUOTA_WEEKLY_TOKENS:
            if window is None:
                window = "weekly"
            i += 1
            continue
        remaining.append(tok)
        i += 1

    if window is None:
        fallback_remaining = []
        for tok in remaining:
            if tok in _QUOTA_FALLBACK_FIVE_HOUR_TOKENS:
                if window is None:
                    window = "five_hour"
                continue
            if tok in _QUOTA_FALLBACK_WEEKLY_TOKENS:
                if window is None:
                    window = "weekly"
                continue
            fallback_remaining.append(tok)
        remaining = fallback_remaining

    group_tokens = [tok for tok in remaining if tok not in _QUOTA_NOISE_TOKENS]
    return "-".join(group_tokens), window


def _is_valid_quota_entry(val):
    """Return True if val is an enabled quota entry with a finite remaining_fraction."""
    if not isinstance(val, dict) or val.get("disabled") is True:
        return False
    rem_frac = val.get("remaining_fraction")
    if rem_frac is None or isinstance(rem_frac, bool):
        return False
    try:
        return math.isfinite(float(rem_frac))
    except Exception:
        return False


def _classify_model_family(tokens):
    """Classify tokenized model identifiers into '3p', 'gemini', or None."""
    if not tokens:
        return None
    if (tokens & _THIRD_PARTY_MODEL_TOKENS) or any(
        tok.startswith(_THIRD_PARTY_MODEL_PREFIXES) for tok in tokens
    ):
        return "3p"
    if tokens & _GEMINI_MODEL_TOKENS:
        return "gemini"
    return None


def _classify_group_family(group):
    """Classify a normalized quota group name into '3p', 'gemini', or None."""
    if not group:
        return None
    group_tokens = set(_tokenize_identifier(group))
    if (
        group in _THIRD_PARTY_GROUP_ALIASES
        or (group_tokens & _THIRD_PARTY_MODEL_TOKENS)
        or any(tok.startswith(_THIRD_PARTY_MODEL_PREFIXES) for tok in group_tokens)
    ):
        return "3p"
    if (group_tokens & _GEMINI_MODEL_TOKENS) or group in ("flash", "pro"):
        return "gemini"
    return None


def _find_direct_group_match(groups, model_tokens):
    """
    Pick the most specific non-empty quota group whose tokens all appear in
    model_tokens (e.g. 'gemini-flash' beats 'gemini' for 'Gemini 3.8 Flash').
    """
    if not model_tokens:
        return None
    best_group = None
    best_score = 0
    for group in sorted(groups):
        if not group:
            continue
        group_tokens = _tokenize_identifier(group)
        if not group_tokens:
            continue
        if all(tok in model_tokens for tok in group_tokens):
            score = len(group_tokens)
            if score > best_score:
                best_score = score
                best_group = group
    return best_group


def select_model_quotas(quotas, model_name="", response_model=""):
    """
    Select (five_hour_info, weekly_info) matching the active model's quota group.
    Handles multi-group payloads (e.g. '3p-*' and 'gemini-*') without letting
    key ordering overwrite one model family's quota with another's.
    """
    if not isinstance(quotas, dict):
        return None, None

    groups = {}
    seen_groups = set()
    unclassified = []

    for key, val in quotas.items():
        if not isinstance(val, dict):
            continue
        group, window = _parse_quota_key(key)
        if window is not None:
            seen_groups.add(group)
        if not _is_valid_quota_entry(val):
            continue
        if window is not None:
            bucket = groups.setdefault(group, {"five_hour": None, "weekly": None})
            if bucket[window] is None:
                bucket[window] = val
        else:
            unclassified.append(val)

    if not groups:
        if unclassified:
            first = unclassified[0]
            second = unclassified[1] if len(unclassified) > 1 else None
            return first, second
        return None, None

    primary_tokens = set(_tokenize_identifier(model_name))
    secondary_tokens = set(_tokenize_identifier(response_model))
    primary_family = _classify_model_family(primary_tokens)
    secondary_family = _classify_model_family(secondary_tokens)

    # Combine response_model hints unless they contradict the active display model.
    if primary_family is None or secondary_family is None or secondary_family == primary_family:
        effective_tokens = primary_tokens | secondary_tokens
    else:
        effective_tokens = primary_tokens

    model_family = primary_family or secondary_family

    # 1. Exact/sub-group token match (e.g. 'gemini-flash' or 'claude').
    matched_group = _find_direct_group_match(groups, effective_tokens)
    if matched_group is not None:
        selected = groups[matched_group]
        return selected["five_hour"], selected["weekly"]

    # 2. Known family alias lookup (e.g. Claude/GPT -> '3p', Orchestrator/Neon -> 'gemini').
    if model_family == "3p":
        candidates = _THIRD_PARTY_GROUP_ALIASES
    elif model_family == "gemini":
        candidates = (
            _GEMINI_FLASH_GROUP_ALIASES
            if "flash" in effective_tokens
            else _GEMINI_PRO_GROUP_ALIASES
        )
    else:
        candidates = ()

    for candidate in candidates:
        if candidate in groups:
            selected = groups[candidate]
            return selected["five_hour"], selected["weekly"]

    # 3. Unprefixed generic buckets (e.g. legacy 'five_hour' / 'weekly') apply to any model.
    if "" in groups:
        selected = groups[""]
        return selected["five_hour"], selected["weekly"]

    # 4. Single-group payload fallback only when it does not conflict with model_family.
    if len(seen_groups) == 1 and len(groups) == 1:
        sole_group, selected = next(iter(groups.items()))
        sole_family = _classify_group_family(sole_group)
        if model_family is None or sole_family is None or sole_family == model_family:
            return selected["five_hour"], selected["weekly"]

    return None, None


# Cross-terminal shared quota synchronization constants
_QUOTA_CONFIDENCE_COLD = 1
_QUOTA_CONFIDENCE_CONFIRMED = 2
_FRESH_PROCESS_WINDOW_SECONDS = 30.0
_WINDOW_ROLLOVER_GRACE_SECONDS = 60.0
_CACHE_MAX_STALE_SECONDS = 6 * 3600.0
_TERMINAL_TOUCH_INTERVAL_SECONDS = 1800.0
_TERMINAL_PRUNE_AGE_SECONDS = 24 * 3600.0
_MAX_ANCESTOR_HOPS = 6
_QUOTA_WINDOW_DURATIONS = {
    "five_hour": 5 * 3600.0,
    "weekly": 7 * 86400.0,
}

_WRAPPER_PROCESS_NAMES = frozenset({
    "cmd",
    "sh",
    "bash",
    "zsh",
    "dash",
    "ash",
    "ksh",
    "fish",
    "csh",
    "tcsh",
    "pwsh",
    "powershell",
    "python",
    "python3",
    "py",
    "env",
    "timeout",
})
_NON_TERMINAL_ROOT_NAMES = frozenset({
    "init",
    "systemd",
    "launchd",
    "system",
    "services",
    "wininit",
})

_WIN32_PROC_API = None
_LINUX_BOOT_TIME = None
_DARWIN_PROC_API = None


def _parse_iso_timestamp(value):
    """Parse an RFC 3339 / ISO-8601 timestamp string into Unix epoch seconds, or None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text[-1] in ("Z", "z"):
        text = text[:-1] + "+00:00"

    sep_idx = text.find("T")
    if sep_idx < 0:
        sep_idx = text.find("t")
        if sep_idx > 0:
            text = text[:sep_idx] + "T" + text[sep_idx + 1 :]
    if sep_idx < 0:
        sep_idx = text.find(" ")

    # Normalize fractional seconds to 6 digits for Python 3.8-3.10 fromisoformat support.
    if sep_idx > 0:
        time_part = text[sep_idx + 1 :]
        dot_idx = time_part.find(".")
        if dot_idx >= 0:
            frac_start = sep_idx + 1 + dot_idx + 1
            frac_end = frac_start
            while frac_end < len(text) and text[frac_end].isdigit():
                frac_end += 1
            if frac_end > frac_start:
                frac = text[frac_start:frac_end]
                if len(frac) != 6:
                    frac = (frac + "000000")[:6]
                    text = text[:frac_start] + frac + text[frac_end:]

    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ts = dt.timestamp()
    return float(ts) if math.isfinite(ts) else None


def _normalize_process_name(raw_name):
    """Normalize an executable path or comm name to a lowercase stem without .exe."""
    if not raw_name:
        return ""
    base = os.path.basename(str(raw_name).replace("\\", "/")).strip().lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return base


def _is_host_agy_name(norm_name):
    """Return True if norm_name identifies a host Antigravity CLI process."""
    return bool(
        norm_name == "agy"
        or norm_name.startswith("agy-")
        or norm_name.startswith("antigravity")
    )


def _is_wrapper_process_name(norm_name):
    """Return True if norm_name is a transient shell or Python launcher wrapper."""
    if not norm_name or norm_name in _WRAPPER_PROCESS_NAMES:
        return True
    if norm_name.startswith("python"):
        suffix = norm_name[6:]
        if suffix.startswith("w"):
            suffix = suffix[1:]
        if not suffix or suffix.replace(".", "").isdigit():
            return True
    return False


def _get_win32_proc_api():
    """Lazily initialize typed ctypes bindings for Windows process inspection."""
    global _WIN32_PROC_API
    if _WIN32_PROC_API is not None:
        return _WIN32_PROC_API

    import ctypes
    from ctypes import wintypes

    class PROCESS_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("ExitStatus", ctypes.c_void_p),
            ("PebBaseAddress", ctypes.c_void_p),
            ("AffinityMask", ctypes.c_void_p),
            ("BasePriority", ctypes.c_void_p),
            ("UniqueProcessId", ctypes.c_void_p),
            ("InheritedFromUniqueProcessId", ctypes.c_void_p),
        ]

    class FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    ntdll = ctypes.WinDLL("ntdll")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    ntdll.NtQueryInformationProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.ULONG,
        ctypes.POINTER(wintypes.ULONG),
    ]
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long

    _WIN32_PROC_API = (
        ctypes,
        wintypes,
        ntdll,
        kernel32,
        PROCESS_BASIC_INFORMATION,
        FILETIME,
    )
    return _WIN32_PROC_API


def _inspect_process_windows(pid):
    """Return (ppid, image_name, start_token, start_ts) for a Windows PID, or None."""
    if pid <= 4:
        return None
    try:
        (
            ctypes,
            wintypes,
            ntdll,
            kernel32,
            pbi_cls,
            ft_cls,
        ) = _get_win32_proc_api()
    except Exception:
        return None

    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        pbi = pbi_cls()
        ret_len = wintypes.ULONG(0)
        status = ntdll.NtQueryInformationProcess(
            handle, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), ctypes.byref(ret_len)
        )
        ppid = int(pbi.InheritedFromUniqueProcessId or 0) if status == 0 else 0

        ct, et, kt, ut = ft_cls(), ft_cls(), ft_cls(), ft_cls()
        token = ""
        start_ts = None
        if kernel32.GetProcessTimes(
            handle, ctypes.byref(ct), ctypes.byref(et), ctypes.byref(kt), ctypes.byref(ut)
        ):
            raw_ticks = (ct.dwHighDateTime << 32) | ct.dwLowDateTime
            if raw_ticks > 116444736000000000:
                token = str(raw_ticks)
                start_ts = (raw_ticks - 116444736000000000) / 10_000_000.0

        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        name = ""
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            name = buf.value

        return ppid, name, token, start_ts
    except Exception:
        return None
    finally:
        kernel32.CloseHandle(handle)


def _parse_linux_proc_stat(stat_text):
    """Parse /proc/<pid>/stat text into (ppid, comm, starttime_ticks), or None."""
    if not stat_text or not isinstance(stat_text, str):
        return None
    lparen = stat_text.find("(")
    rparen = stat_text.rfind(")")
    if lparen < 0 or rparen <= lparen:
        return None
    comm = stat_text[lparen + 1 : rparen]
    rest = stat_text[rparen + 2 :].split()
    if len(rest) <= 19:
        return None
    try:
        ppid = int(rest[1])
        starttime = int(rest[19])
    except ValueError:
        return None
    if starttime < 0:
        return None
    return ppid, comm, starttime


def _get_linux_boot_time():
    """Read Linux system boot Unix timestamp from /proc/stat once per process."""
    global _LINUX_BOOT_TIME
    if _LINUX_BOOT_TIME is not None:
        return _LINUX_BOOT_TIME
    try:
        with open("/proc/stat", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("btime "):
                    _LINUX_BOOT_TIME = float(line.split()[1])
                    return _LINUX_BOOT_TIME
    except Exception:
        pass
    return None


def _inspect_process_linux(pid):
    """Return (ppid, comm, start_token, start_ts) for a Linux PID, or None."""
    if pid <= 1:
        return None
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="ignore") as f:
            parsed = _parse_linux_proc_stat(f.read())
    except OSError:
        return None
    if parsed is None:
        return None
    ppid, comm, starttime = parsed
    start_ts = None
    btime = _get_linux_boot_time()
    if btime is not None:
        try:
            clk_tck = float(os.sysconf("SC_CLK_TCK"))
            if clk_tck > 0:
                start_ts = btime + (starttime / clk_tck)
        except Exception:
            pass
    return ppid, comm, str(starttime), start_ts


def _get_darwin_proc_api():
    """Lazily initialize typed ctypes bindings for macOS proc_pidinfo."""
    global _DARWIN_PROC_API
    if _DARWIN_PROC_API is not None:
        return _DARWIN_PROC_API

    import ctypes

    class ProcBsdInfo(ctypes.Structure):
        _fields_ = [
            ("pbi_flags", ctypes.c_uint32),
            ("pbi_status", ctypes.c_uint32),
            ("pbi_xstatus", ctypes.c_uint32),
            ("pbi_pid", ctypes.c_uint32),
            ("pbi_ppid", ctypes.c_uint32),
            ("pbi_uid", ctypes.c_uint32),
            ("pbi_gid", ctypes.c_uint32),
            ("pbi_ruid", ctypes.c_uint32),
            ("pbi_rgid", ctypes.c_uint32),
            ("pbi_svuid", ctypes.c_uint32),
            ("pbi_svgid", ctypes.c_uint32),
            ("rfu_1", ctypes.c_uint32),
            ("pbi_comm", ctypes.c_char * 16),
            ("pbi_name", ctypes.c_char * 32),
            ("pbi_nfiles", ctypes.c_uint32),
            ("pbi_pgid", ctypes.c_uint32),
            ("pbi_pjobc", ctypes.c_uint32),
            ("e_tdev", ctypes.c_uint32),
            ("e_tpgid", ctypes.c_uint32),
            ("pbi_nice", ctypes.c_int32),
            ("pbi_start_tvsec", ctypes.c_uint64),
            ("pbi_start_tvusec", ctypes.c_uint64),
        ]

    libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    libsystem.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    libsystem.proc_pidinfo.restype = ctypes.c_int

    _DARWIN_PROC_API = (ctypes, libsystem, ProcBsdInfo)
    return _DARWIN_PROC_API


def _inspect_process_darwin(pid):
    """Return (ppid, proc_name, start_token, start_ts) for a macOS PID, or None."""
    if pid <= 1:
        return None
    try:
        ctypes, libsystem, bsd_cls = _get_darwin_proc_api()
        info = bsd_cls()
        size = ctypes.sizeof(info)
        if size != 136:
            return None
        ret = libsystem.proc_pidinfo(pid, 3, 0, ctypes.byref(info), size)  # PROC_PIDTBSDINFO
        if ret != size:
            return None
        raw_name = info.pbi_name if info.pbi_name and info.pbi_name[0] else info.pbi_comm
        name = bytes(raw_name).split(b"\x00", 1)[0].decode("utf-8", errors="ignore")
        token = f"{info.pbi_start_tvsec}.{info.pbi_start_tvusec:06d}"
        start_ts = (
            float(info.pbi_start_tvsec) + float(info.pbi_start_tvusec) / 1_000_000.0
            if info.pbi_start_tvsec > 0
            else None
        )
        return int(info.pbi_ppid), name, token, start_ts
    except Exception:
        return None


def _get_terminal_identity(_inspector=None, _start_pid=None, _platform_tag=None):
    """
    Identify the host Antigravity CLI process by walking parent processes past
    transient shell wrappers (cmd.exe / sh). Returns (terminal_id, proc_start_ts)
    or (None, None) when no stable non-wrapper ancestor can be verified.
    """
    if _inspector is None:
        if os.name == "nt":
            _inspector = _inspect_process_windows
            platform_tag = _platform_tag or "win"
        elif sys.platform.startswith("linux"):
            _inspector = _inspect_process_linux
            platform_tag = _platform_tag or "linux"
        elif sys.platform == "darwin":
            _inspector = _inspect_process_darwin
            platform_tag = _platform_tag or "macos"
        else:
            return None, None
    else:
        platform_tag = _platform_tag or "proc"

    try:
        pid = os.getppid() if _start_pid is None else int(_start_pid)
    except Exception:
        return None, None

    visited = set()
    ancestors = []
    min_pid = 5 if platform_tag == "win" else 2

    for _ in range(_MAX_ANCESTOR_HOPS):
        if pid < min_pid or pid in visited:
            break
        visited.add(pid)
        info = _inspector(pid)
        if not info:
            break
        ppid, raw_name, token, start_ts = info
        norm_name = _normalize_process_name(raw_name)
        ancestors.append((pid, norm_name, token, start_ts))
        if _is_host_agy_name(norm_name) and token:
            return f"{platform_tag}:{pid}:{token}", start_ts
        if not ppid or ppid == pid:
            break
        pid = int(ppid)

    for anc_pid, norm_name, token, start_ts in ancestors:
        if (
            token
            and not _is_wrapper_process_name(norm_name)
            and norm_name not in _NON_TERMINAL_ROOT_NAMES
        ):
            return f"{platform_tag}:{anc_pid}:{token}", start_ts

    return None, None


def _coerce_finite_float(val):
    """Return finite float(val) for non-bool numeric inputs, or None."""
    if val is None or isinstance(val, bool):
        return None
    try:
        num = float(val)
    except Exception:
        return None
    return num if math.isfinite(num) else None


def _quota_fingerprint(quotas):
    """
    Compute a canonical fingerprint of server-reported quota buckets, excluding
    locally ticked countdown fields such as reset_in_seconds.
    """
    if not isinstance(quotas, dict):
        return ""
    items = []
    for key in sorted(quotas):
        val = quotas.get(key)
        if not isinstance(val, dict):
            continue
        disabled = val.get("disabled") is True
        rem = _coerce_finite_float(val.get("remaining_fraction"))
        if not disabled and rem is None:
            continue
        raw_reset = val.get("reset_time")
        reset_time = raw_reset.strip() if isinstance(raw_reset, str) else ""
        items.append([str(key), disabled, repr(rem) if rem is not None else None, reset_time])
    if not items:
        return ""
    return json.dumps(items, separators=(",", ":"), ensure_ascii=True)


def _extract_quota_entries(quotas, now_ts):
    """Normalize valid quota buckets from stdin into structured records."""
    entries = {}
    if not isinstance(quotas, dict):
        return entries
    for key, val in quotas.items():
        if not isinstance(val, dict):
            continue
        bucket_key = str(key).strip()
        if not bucket_key:
            continue
        disabled = val.get("disabled") is True
        rem = _coerce_finite_float(val.get("remaining_fraction"))
        if not disabled and rem is None:
            continue
        raw_reset = val.get("reset_time")
        reset_time = raw_reset.strip() if isinstance(raw_reset, str) else ""
        reset_epoch = _parse_iso_timestamp(reset_time) if reset_time else None
        if reset_epoch is None:
            res_sec = _coerce_finite_float(val.get("reset_in_seconds"))
            if res_sec is not None:
                reset_epoch = now_ts + res_sec
        entries[bucket_key] = {
            "bucket_key": bucket_key,
            "remaining_fraction": rem,
            "reset_time": reset_time,
            "reset_epoch": reset_epoch,
            "disabled": disabled,
        }
    return entries


def _infer_sliding_fetch_ts(entries, now_ts, proc_start_ts=None):
    """
    Infer server fetch time from any unused 100% bucket whose reset_time slides
    forward as server_now + window_duration on every refresh. Sanity-bounded so
    unexpected window lengths are ignored.
    """
    candidates = []
    for bucket_key, entry in entries.items():
        if entry["disabled"]:
            continue
        rem = entry["remaining_fraction"]
        reset_epoch = entry["reset_epoch"]
        if rem is None or rem < 1.0 - 1e-6 or reset_epoch is None or not entry["reset_time"]:
            continue
        _, window = _parse_quota_key(bucket_key)
        window_sec = _QUOTA_WINDOW_DURATIONS.get(window)
        if not window_sec:
            continue
        inferred = reset_epoch - window_sec
        if inferred > now_ts + 120.0:
            continue
        if inferred < now_ts - 7 * 86400.0:
            continue
        if proc_start_ts is not None and inferred < proc_start_ts - 120.0:
            continue
        candidates.append(inferred)
    return max(candidates) if candidates else None


def _should_replace_bucket(cached, incoming, now_ts):
    """
    Decide whether an incoming bucket observation should replace a cached entry.
    Confirmed state-transition refreshes supersede cold-start entries and follow
    chronological arrival order (handling non-monotonic server jitter cleanly).
    """
    if cached is None:
        return True

    cached_reset = cached.get("reset_epoch")
    incoming_reset = incoming.get("reset_epoch")

    # 1. Active newer window always beats an already-expired window.
    if cached_reset is not None and incoming_reset is not None:
        if cached_reset <= now_ts and incoming_reset > cached_reset + _WINDOW_ROLLOVER_GRACE_SECONDS:
            return True
        if incoming_reset <= now_ts and cached_reset > incoming_reset + _WINDOW_ROLLOVER_GRACE_SECONDS:
            return False

    # 2. Expire stale cache records so long-idle accounts always recover.
    if (now_ts - cached["observed_at"]) > _CACHE_MAX_STALE_SECONDS:
        return True

    # 3. Confirmed refreshes strictly outrank unconfirmed cold-start snapshots.
    if incoming["confidence"] > cached["confidence"]:
        return True
    if incoming["confidence"] < cached["confidence"]:
        return False

    # 4. Both confirmed: latest observed server refresh wins (immune to jitter).
    if incoming["confidence"] >= _QUOTA_CONFIDENCE_CONFIRMED:
        return incoming["observed_at"] >= cached["observed_at"]

    # 5. Both cold-start: prefer newer window, then sliding server clock, then lower quota.
    if cached_reset is not None and incoming_reset is not None:
        if abs(incoming_reset - cached_reset) > _WINDOW_ROLLOVER_GRACE_SECONDS:
            return incoming_reset > cached_reset

    cached_inferred = cached.get("inferred_fetch_ts")
    incoming_inferred = incoming.get("inferred_fetch_ts")
    if cached_inferred is not None and incoming_inferred is not None:
        if abs(incoming_inferred - cached_inferred) > 1.0:
            return incoming_inferred > cached_inferred

    if not cached["disabled"] and not incoming["disabled"]:
        c_rem = cached.get("remaining_fraction")
        i_rem = incoming.get("remaining_fraction")
        if c_rem is not None and i_rem is not None:
            return i_rem < c_rem - 1e-6

    return False


def _should_use_cached_bucket(cached, incoming, now_ts, can_trust_terminal):
    """Return True if cached should be rendered in place of incoming."""
    if cached is None:
        return False
    if (now_ts - cached["observed_at"]) > _CACHE_MAX_STALE_SECONDS:
        return False
    cached_reset = cached.get("reset_epoch")
    incoming_reset = incoming.get("reset_epoch")
    if (
        cached_reset is not None
        and incoming_reset is not None
        and cached_reset <= now_ts
        and incoming_reset > cached_reset + _WINDOW_ROLLOVER_GRACE_SECONDS
    ):
        return False
    if not can_trust_terminal:
        return not _should_replace_bucket(cached, incoming, now_ts)
    return True


def _build_merged_quota_map(raw_quotas, incoming_entries, cached_buckets, now_ts, can_trust_terminal):
    """Merge fresher cached buckets onto raw_quotas with live recomputed countdowns."""
    merged = {}
    for key, val in raw_quotas.items():
        if not isinstance(val, dict):
            merged[key] = val
            continue
        bucket_key = str(key).strip()
        incoming = incoming_entries.get(bucket_key)
        cached = cached_buckets.get(bucket_key)
        if incoming is not None and _should_use_cached_bucket(
            cached, incoming, now_ts, can_trust_terminal
        ):
            updated = dict(val)
            if cached["disabled"]:
                updated["disabled"] = True
                if cached["remaining_fraction"] is not None:
                    updated["remaining_fraction"] = cached["remaining_fraction"]
                else:
                    updated.pop("remaining_fraction", None)
            else:
                updated.pop("disabled", None)
                if cached["remaining_fraction"] is not None:
                    updated["remaining_fraction"] = cached["remaining_fraction"]
            if cached["reset_time"]:
                updated["reset_time"] = cached["reset_time"]
            if cached["reset_epoch"] is not None:
                updated["reset_in_seconds"] = max(0, int(cached["reset_epoch"] - now_ts))
            merged[key] = updated
        else:
            merged[key] = val
    return merged


def _resolve_quota_cache_db_path():
    """Return the shared quota SQLite cache path, or '' when explicitly disabled."""
    disabled_flag = os.environ.get("AGY_STATUSLINE_SHARED_QUOTA", "").strip().lower()
    if disabled_flag in ("0", "false", "no", "off"):
        return ""
    override = os.environ.get("AGY_STATUSLINE_QUOTA_DB", "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(
        os.path.expanduser("~/.gemini/antigravity-cli"),
        "cache",
        "statusline_quota.db",
    )


def _init_quota_cache_db(con):
    """Initialize WAL mode and schema for the shared cross-terminal quota cache."""
    con.execute("PRAGMA busy_timeout=150")
    mode_row = con.execute("PRAGMA journal_mode").fetchone()
    if not mode_row or str(mode_row[0]).lower() != "wal":
        con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS terminal_state (
                account_key TEXT NOT NULL,
                terminal_id TEXT NOT NULL,
                last_fp TEXT NOT NULL,
                last_seen_at REAL NOT NULL,
                PRIMARY KEY (account_key, terminal_id)
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS quota_cache (
                account_key TEXT NOT NULL,
                bucket_key TEXT NOT NULL,
                remaining_fraction REAL,
                reset_time TEXT NOT NULL,
                reset_epoch REAL,
                disabled INTEGER NOT NULL,
                confidence INTEGER NOT NULL,
                observed_at REAL NOT NULL,
                inferred_fetch_ts REAL,
                source_terminal TEXT NOT NULL,
                PRIMARY KEY (account_key, bucket_key)
            )
            """
        )
        con.execute("COMMIT")
    except Exception:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.execute("PRAGMA busy_timeout=50")


def _read_quota_cache_rows(con, account_key):
    """Load all cached quota buckets for account_key as a dict."""
    rows = con.execute(
        """
        SELECT bucket_key, remaining_fraction, reset_time, reset_epoch,
               disabled, confidence, observed_at, inferred_fetch_ts, source_terminal
        FROM quota_cache
        WHERE account_key = ?
        """,
        (account_key,),
    ).fetchall()
    cached = {}
    for (
        bucket_key,
        rem_frac,
        reset_time,
        reset_epoch,
        disabled,
        confidence,
        observed_at,
        inferred_fetch_ts,
        source_terminal,
    ) in rows:
        cached[bucket_key] = {
            "bucket_key": bucket_key,
            "remaining_fraction": rem_frac,
            "reset_time": reset_time or "",
            "reset_epoch": reset_epoch,
            "disabled": bool(disabled),
            "confidence": int(confidence),
            "observed_at": float(observed_at),
            "inferred_fetch_ts": inferred_fetch_ts,
            "source_terminal": source_terminal or "",
        }
    return cached


def _is_corrupt_sqlite_error(exc):
    """Return True if exc signals a malformed/non-database SQLite file."""
    msg = str(exc).lower()
    return "malformed" in msg or "not a database" in msg


def _is_safe_cache_unlink_target(path):
    """Only auto-remove corrupt cache files that clearly look like a regular .db file."""
    return bool(
        path
        and path.lower().endswith(".db")
        and not os.path.islink(path)
    )


def _classify_observation_confidence(existing_term_row, proc_start_ts, now_ts):
    """Return CONFIRMED for observed transitions or newly launched agy processes, else COLD."""
    if existing_term_row is not None:
        return _QUOTA_CONFIDENCE_CONFIRMED
    if proc_start_ts is not None and -5.0 <= (now_ts - proc_start_ts) <= _FRESH_PROCESS_WINDOW_SECONDS:
        return _QUOTA_CONFIDENCE_CONFIRMED
    return _QUOTA_CONFIDENCE_COLD


def _commit_quota_update(
    con,
    account_key,
    terminal_id,
    proc_start_ts,
    fp,
    incoming_entries,
    inferred_ts,
    now_ts,
):
    """
    Record terminal state and conditionally update quota_cache inside a single
    BEGIN IMMEDIATE transaction. Returns the up-to-date cached_buckets dict.
    """
    con.execute("BEGIN IMMEDIATE")
    try:
        tx_term = con.execute(
            "SELECT last_fp, last_seen_at FROM terminal_state "
            "WHERE account_key = ? AND terminal_id = ?",
            (account_key, terminal_id),
        ).fetchone()

        if not incoming_entries:
            if tx_term is None:
                con.execute(
                    "INSERT OR REPLACE INTO terminal_state "
                    "(account_key, terminal_id, last_fp, last_seen_at) "
                    "VALUES (?, ?, ?, ?)",
                    (account_key, terminal_id, "", now_ts),
                )
            con.execute("COMMIT")
            return {}

        fp_changed = tx_term is None or tx_term[0] != fp
        confidence = _classify_observation_confidence(tx_term, proc_start_ts, now_ts)

        con.execute(
            "INSERT OR REPLACE INTO terminal_state "
            "(account_key, terminal_id, last_fp, last_seen_at) "
            "VALUES (?, ?, ?, ?)",
            (account_key, terminal_id, fp, now_ts),
        )

        cached_buckets = _read_quota_cache_rows(con, account_key)
        if fp_changed:
            for bucket_key, entry in incoming_entries.items():
                candidate = {
                    **entry,
                    "confidence": confidence,
                    "observed_at": now_ts,
                    "inferred_fetch_ts": inferred_ts,
                    "source_terminal": terminal_id,
                }
                if _should_replace_bucket(cached_buckets.get(bucket_key), candidate, now_ts):
                    con.execute(
                        """
                        INSERT OR REPLACE INTO quota_cache (
                            account_key, bucket_key, remaining_fraction,
                            reset_time, reset_epoch, disabled, confidence,
                            observed_at, inferred_fetch_ts, source_terminal
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            account_key,
                            bucket_key,
                            candidate["remaining_fraction"],
                            candidate["reset_time"],
                            candidate["reset_epoch"],
                            1 if candidate["disabled"] else 0,
                            candidate["confidence"],
                            candidate["observed_at"],
                            candidate["inferred_fetch_ts"],
                            candidate["source_terminal"],
                        ),
                    )
                    cached_buckets[bucket_key] = candidate

        prune_cutoff = now_ts - _TERMINAL_PRUNE_AGE_SECONDS
        con.execute(
            "DELETE FROM terminal_state WHERE last_seen_at < ?",
            (prune_cutoff,),
        )
        con.execute(
            "DELETE FROM quota_cache WHERE observed_at < ?",
            (prune_cutoff,),
        )

        con.execute("COMMIT")
        return cached_buckets
    except Exception:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise


def sync_shared_quota(data, db_path=None, _now=None, _terminal_identity=_UNSET):
    """
    Synchronize quota buckets across concurrent agy terminals on the same account
    using per-terminal state-transition detection backed by a local SQLite WAL DB.

    Returns a shallow copy of data with fresher synced quota buckets when available,
    or the original data unchanged on missing email/quota or any storage error.
    """
    if not isinstance(data, dict):
        return data

    raw_email = data.get("email")
    if not isinstance(raw_email, str) or not raw_email.strip():
        return data
    account_key = raw_email.strip().lower()

    if isinstance(data.get("quota"), dict):
        quota_field = "quota"
        raw_quotas = data["quota"]
    elif isinstance(data.get("quotas"), dict):
        quota_field = "quotas"
        raw_quotas = data["quotas"]
    else:
        quota_field = None
        raw_quotas = None

    resolved_path = _resolve_quota_cache_db_path() if db_path is None else str(db_path)
    if not resolved_path:
        return data

    now_ts = time.time() if _now is None else float(_now)
    if _terminal_identity is _UNSET:
        terminal_id, proc_start_ts = _get_terminal_identity()
    elif isinstance(_terminal_identity, tuple) and len(_terminal_identity) == 2:
        terminal_id, proc_start_ts = _terminal_identity
    else:
        terminal_id, proc_start_ts = None, None

    if raw_quotas:
        fp = _quota_fingerprint(raw_quotas)
        incoming_entries = _extract_quota_entries(raw_quotas, now_ts) if fp else {}
    else:
        fp = ""
        incoming_entries = {}

    # Pre-quota frames only need to register a brand-new terminal once so its
    # subsequent first quota arrival counts as a confirmed state transition.
    if not incoming_entries and terminal_id is None:
        return data

    db_exists = os.path.isfile(resolved_path)
    if not db_exists and terminal_id is None:
        return data

    inferred_ts = (
        _infer_sliding_fetch_ts(incoming_entries, now_ts, proc_start_ts)
        if incoming_entries
        else None
    )

    try:
        if not db_exists:
            parent = os.path.dirname(os.path.abspath(resolved_path))
            if parent:
                os.makedirs(parent, exist_ok=True)

        with closing(sqlite3.connect(resolved_path, timeout=0.05, isolation_level=None)) as con:
            con.execute("PRAGMA busy_timeout=50")

            schema_ready = db_exists
            term_row = None
            cached_buckets = {}

            if schema_ready:
                try:
                    if terminal_id is not None:
                        term_row = con.execute(
                            "SELECT last_fp, last_seen_at FROM terminal_state "
                            "WHERE account_key = ? AND terminal_id = ?",
                            (account_key, terminal_id),
                        ).fetchone()
                    if incoming_entries:
                        cached_buckets = _read_quota_cache_rows(con, account_key)
                except sqlite3.OperationalError as exc:
                    if "no such table" in str(exc).lower():
                        schema_ready = False
                    else:
                        raise

            if not schema_ready:
                if terminal_id is None:
                    return data
                _init_quota_cache_db(con)
                if incoming_entries:
                    cached_buckets = _read_quota_cache_rows(con, account_key)

            # Decide whether a write transaction is needed (99% of ticks are read-only).
            if not incoming_entries:
                need_write = terminal_id is not None and term_row is None
            else:
                need_write = terminal_id is not None and (
                    term_row is None
                    or term_row[0] != fp
                    or (now_ts - float(term_row[1])) >= _TERMINAL_TOUCH_INTERVAL_SECONDS
                )

            write_succeeded = not need_write
            if need_write:
                try:
                    updated_cache = _commit_quota_update(
                        con,
                        account_key,
                        terminal_id,
                        proc_start_ts,
                        fp,
                        incoming_entries,
                        inferred_ts,
                        now_ts,
                    )
                    if incoming_entries:
                        cached_buckets = updated_cache
                    write_succeeded = True
                except sqlite3.Error as exc:
                    if _is_corrupt_sqlite_error(exc):
                        raise
                    # Transient lock or read-only DB: fall back to comparing against
                    # cached_buckets without letting stale cache overwrite a fresh local refresh.

            if not incoming_entries or not cached_buckets:
                return data

            can_trust_cache_override = terminal_id is not None and (
                write_succeeded or (term_row is not None and term_row[0] == fp)
            )
            if can_trust_cache_override:
                incoming_for_merge = incoming_entries
            else:
                fallback_confidence = (
                    _classify_observation_confidence(term_row, proc_start_ts, now_ts)
                    if terminal_id is not None
                    else _QUOTA_CONFIDENCE_COLD
                )
                incoming_for_merge = {
                    bucket_key: {
                        **entry,
                        "confidence": fallback_confidence,
                        "observed_at": now_ts,
                        "inferred_fetch_ts": inferred_ts,
                    }
                    for bucket_key, entry in incoming_entries.items()
                }

            merged_quotas = _build_merged_quota_map(
                raw_quotas,
                incoming_for_merge,
                cached_buckets,
                now_ts,
                can_trust_terminal=can_trust_cache_override,
            )
            result = dict(data)
            result[quota_field] = merged_quotas
            return result
    except sqlite3.DatabaseError as exc:
        if _is_corrupt_sqlite_error(exc) and _is_safe_cache_unlink_target(resolved_path):
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(resolved_path + suffix)
                except OSError:
                    pass
        return data
    except Exception:
        return data


def render_statusline(data, _now=None):
    """Render the 2-line HUD status string from Antigravity state data."""
    # === 1. Line 1: Model, Directory, Git Branch, Agent State and Mode, Working Duration ===
    line1_items = []

    # (1) Model Name & Upstream Response Model
    model_info = data.get("model")
    reported_model_name = ""
    reported_model_id = ""
    explicit_resp_model = ""
    if isinstance(model_info, dict):
        raw_display = model_info.get("display_name")
        raw_id = model_info.get("id")
        if raw_display is not None and str(raw_display).strip():
            reported_model_name = str(raw_display).strip()
        if raw_id is not None and str(raw_id).strip():
            reported_model_id = str(raw_id).strip()
        if not reported_model_name:
            reported_model_name = reported_model_id
        if model_info.get("response_model"):
            explicit_resp_model = str(model_info["response_model"]).strip()
    elif isinstance(model_info, str) and model_info.strip():
        reported_model_name = model_info.strip()

    model_name = reported_model_name or "Gemini"

    # Simplify model name (e.g. Gemini 3.8 Flash (High) -> 3.8 Flash (High))
    clean_model = model_name.replace("Gemini", "").replace("Google", "").strip()
    if not clean_model:
        clean_model = model_name

    # Share a single read-only SQLite pass across model inspection and work duration
    json_turn, json_session = _extract_json_durations(data)
    need_model = not bool(explicit_resp_model)
    need_timing = (
        json_turn is None
        or json_turn <= 0
        or json_session is None
        or json_session <= 0
    )
    db_info = None
    if need_model or need_timing:
        db_path = _resolve_conversation_db_path(data)
        if db_path:
            db_info = _query_conversation_db(
                db_path,
                need_model=need_model,
                expected_display_name=reported_model_name,
                need_timing=need_timing,
            )

    response_model = explicit_resp_model or (db_info["response_model"] if db_info else "")
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

    # (4) Agent State and Execution Mode
    raw_state = data.get("agent_state")
    agent_state = raw_state.strip() if isinstance(raw_state, str) and raw_state.strip() else "Idle"
    is_state_active = agent_state.lower() not in _IDLE_AGENT_STATES
    state_color = YELLOW if is_state_active else GREEN
    state_item = f"⚡ {state_color}{agent_state}{RESET}"
    cycle_mode = data.get("cycle_mode")
    if cycle_mode in ("plan", "auto", "accept-edits"):
        state_item += f" {GRAY}·{RESET} {CYAN}[{cycle_mode}]{RESET}"
    line1_items.append(state_item)

    # (5) Working Duration: Current Turn / Total Session
    turn_sec, session_sec, is_turn_active = get_work_durations(
        data, is_state_active=is_state_active, _db_info=db_info, _now=_now
    )
    if turn_sec is not None and session_sec is not None:
        turn_color = YELLOW if is_turn_active else CYAN
        line1_items.append(
            f"⏱️ {turn_color}{format_duration(turn_sec)}{RESET} {GRAY}/ {format_duration(session_sec)}{RESET}"
        )
    elif session_sec is not None:
        line1_items.append(f"⏱️ {CYAN}{format_duration(session_sec)}{RESET}")
    elif turn_sec is not None:
        turn_color = YELLOW if is_turn_active else CYAN
        line1_items.append(f"⏱️ {turn_color}{format_duration(turn_sec)}{RESET}")

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

    # (2) Quotas: 5h & Weekly (scoped to the active model's quota group)
    quotas = data.get("quota") or data.get("quotas") or {}
    quota_model_hint = reported_model_name
    if reported_model_id and reported_model_id != reported_model_name:
        quota_model_hint = f"{reported_model_name} {reported_model_id}".strip()
    five_hour_info, weekly_info = select_model_quotas(
        quotas,
        model_name=quota_model_hint,
        response_model=response_model,
    )

    for icon, label, info in (
        ("⏳", "5h", five_hour_info),
        ("📅", "Wk", weekly_info),
    ):
        if isinstance(info, dict):
            rem_frac = info.get("remaining_fraction")
            if rem_frac is not None and not isinstance(rem_frac, bool):
                try:
                    pct = float(rem_frac) * 100
                    if not math.isfinite(pct):
                        continue
                    res_sec = info.get("reset_in_seconds", 0)
                    res_text = format_time_remaining(res_sec) if res_sec else ""
                    res_str = f" {GRAY}({res_text}){RESET}" if res_text else ""
                    color = GREEN if pct > 30 else (YELLOW if pct > 10 else RED)
                    line2_items.append(f"{icon} {label}: {color}{pct:.0f}%{RESET}{res_str}")
                except Exception:
                    pass

    line2 = f" {GRAY}│{RESET} ".join(line2_items)

    return f"{line1}\n{line2}"


_STDIN_READ_TIMEOUT_SECONDS = 1.0


def _read_stdin():
    """Collect the hook payload until EOF or the input deadline."""
    # Raw reads let Python finalize stdin even if the daemon thread is blocked.
    raw_stdin = sys.stdin.buffer.raw
    data = bytearray()
    data_lock = threading.Lock()
    stop_reading = threading.Event()

    def read_chunks():
        try:
            while True:
                chunk = raw_stdin.read(64 * 1024)
                if not chunk:
                    break
                with data_lock:
                    if stop_reading.is_set():
                        break
                    data.extend(chunk)
        except OSError:
            with data_lock:
                data.clear()
        finally:
            stop_reading.set()

    threading.Thread(target=read_chunks, daemon=True).start()
    stop_reading.wait(_STDIN_READ_TIMEOUT_SECONDS)
    with data_lock:
        stop_reading.set()
        return data.decode("utf-8", errors="replace")


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
            "cycle_mode": "plan",
            "session": {
                "turn_duration_seconds": 42,
                "duration_seconds": 1110,
            },
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
        raw = _read_stdin()
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
        data = sync_shared_quota(data)
    except Exception:
        pass

    try:
        print(render_statusline(data))
    except Exception:
        print("🧠 Context: Ready")


if __name__ == "__main__":
    main()
