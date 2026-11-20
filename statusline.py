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
        print(render_statusline(data))
    except Exception:
        print("🧠 Context: Ready")


if __name__ == "__main__":
    main()
