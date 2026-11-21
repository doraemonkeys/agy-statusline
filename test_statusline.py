"""Regression checks for read-only metadata access and installed launch commands."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import venv

import install as installer
import statusline


def encode_varint(value):
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def proto_bytes(field_number, payload):
    return encode_varint((field_number << 3) | 2) + encode_varint(len(payload)) + payload


def proto_varint(field_number, value):
    return encode_varint((field_number << 3) | 0) + encode_varint(value)


def timestamp_blob(seconds, nanos=0):
    payload = proto_varint(1, int(seconds))
    if nanos:
        payload += proto_varint(2, int(nanos))
    return payload


def trajectory_blob(created_sec, nanos=0):
    return proto_bytes(2, timestamp_blob(created_sec, nanos))


def step_metadata_blob(created_sec, updated_sec=None, completed_sec=None):
    blob = proto_bytes(1, timestamp_blob(created_sec))
    if updated_sec is not None:
        blob += proto_bytes(6, timestamp_blob(updated_sec))
    if completed_sec is not None:
        blob += proto_bytes(32, timestamp_blob(completed_sec))
    return blob


def metadata(model):
    encoded = model.encode("utf-8")
    chat_model = b"\x9a\x01" + bytes([len(encoded)]) + encoded
    return b"\x0a" + bytes([len(chat_model)]) + chat_model


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="agy-db-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "conversations # with spaces"
        self.directory.mkdir()
        self.database = self.directory / "session.db"
        patcher = patch.object(
            statusline.os.path, "expanduser", return_value=str(self.directory)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.real_connect = sqlite3.connect
        self.connections = []

    def capture_connection(self, *args, **kwargs):
        connection = self.real_connect(*args, **kwargs)
        self.connections.append(connection)
        return connection

    def assert_connections_closed(self):
        self.assertTrue(self.connections)
        for connection in self.connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def test_reads_committed_wal_updates_and_closes_connections(self):
        with contextlib.closing(self.real_connect(self.database)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB)")
            writer.execute("INSERT INTO gen_metadata VALUES (1, ?)", (metadata("first"),))
            writer.commit()
            self.assertTrue(Path(str(self.database) + "-wal").exists())

            def read_only_connection(*args, **kwargs):
                connection = self.capture_connection(*args, **kwargs)
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("CREATE TABLE forbidden (id INTEGER)")
                return connection

            with patch.object(statusline.sqlite3, "connect", read_only_connection):
                self.assertEqual(
                    statusline.get_upstream_response_model({"session_id": "session"}),
                    "first",
                )
                writer.execute(
                    "INSERT INTO gen_metadata VALUES (2, ?)", (metadata("second"),)
                )
                writer.commit()
                writer.execute(
                    "INSERT INTO gen_metadata VALUES (3, ?)", (metadata("uncommitted"),)
                )
                self.assertEqual(
                    statusline.get_upstream_response_model({"session_id": "session"}),
                    "second",
                )
                self.assert_connections_closed()
                writer.rollback()

    def test_query_failure_closes_connection_and_falls_back(self):
        with contextlib.closing(self.real_connect(self.database)):
            pass
        with patch.object(statusline.sqlite3, "connect", self.capture_connection):
            self.assertEqual(
                statusline.get_upstream_response_model({"session_id": "session"}), ""
            )
        self.assert_connections_closed()

    def test_disappearing_database_is_not_recreated(self):
        with patch.object(statusline.os.path, "isfile", return_value=True):
            self.assertEqual(
                statusline.get_upstream_response_model({"session_id": "session"}), ""
            )
        self.assertFalse(self.database.exists())

    def test_single_connection_reads_model_and_multi_turn_durations(self):
        with contextlib.closing(self.real_connect(self.database)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB)")
            writer.execute("CREATE TABLE trajectory_metadata_blob (id TEXT, data BLOB)")
            writer.execute(
                "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, status INTEGER, metadata BLOB)"
            )
            writer.execute("INSERT INTO gen_metadata VALUES (1, ?)", (metadata("gemini-4-carbon-high"),))
            writer.execute(
                "INSERT INTO trajectory_metadata_blob VALUES ('main', ?)",
                (trajectory_blob(1000),),
            )
            # Turn 1: t=1000..1120 (2m 00s)
            writer.execute(
                "INSERT INTO steps VALUES (0, 14, 3, ?)",
                (step_metadata_blob(1000),),
            )
            writer.execute(
                "INSERT INTO steps VALUES (1, 15, 3, ?)",
                (step_metadata_blob(1001, updated_sec=1118, completed_sec=1120),),
            )
            writer.commit()

            # Idle at t=1500: Turn 1 duration freezes at 2m 00s; session elapsed is 8m 20s.
            self.connections.clear()
            with patch.object(statusline.sqlite3, "connect", self.capture_connection):
                rendered_idle = statusline.render_statusline(
                    {"session_id": "session", "agent_state": "idle"},
                    _now=1500,
                )
            self.assertEqual(len(self.connections), 1)
            self.assert_connections_closed()
            self.assertIn("[gemini-4-carbon-high]", rendered_idle)
            self.assertIn(f"⏱️ {statusline.CYAN}2m 00s{statusline.RESET}", rendered_idle)
            self.assertIn("/ 8m 20s", rendered_idle)

            # Turn 2 starts at t=1500; agent actively working at t=1542 -> 42s / 9m 02s.
            writer.execute(
                "INSERT INTO steps VALUES (2, 14, 3, ?)",
                (step_metadata_blob(1500),),
            )
            writer.commit()

            self.connections.clear()
            with patch.object(statusline.sqlite3, "connect", self.capture_connection):
                rendered_active = statusline.render_statusline(
                    {"session_id": "session", "agent_state": "working"},
                    _now=1542,
                )
            self.assertEqual(len(self.connections), 1)
            self.assert_connections_closed()
            self.assertIn(f"⏱️ {statusline.YELLOW}42s{statusline.RESET}", rendered_active)
            self.assertIn("/ 9m 02s", rendered_active)

            # Running step (status=2) keeps turn live even if agent_state is omitted.
            writer.execute(
                "INSERT INTO steps VALUES (3, 132, 2, ?)",
                (step_metadata_blob(1542, updated_sec=1545),),
            )
            writer.commit()

            self.connections.clear()
            with patch.object(statusline.sqlite3, "connect", self.capture_connection):
                turn_sec, session_sec, is_active = statusline.get_work_durations(
                    {"session_id": "session"},
                    is_state_active=False,
                    _now=1565,
                )
            self.assertEqual(len(self.connections), 1)
            self.assert_connections_closed()
            self.assertTrue(is_active)
            self.assertEqual(turn_sec, 65.0)
            self.assertEqual(session_sec, 565.0)

    def test_corrupted_blobs_and_path_traversal_fail_safely(self):
        with contextlib.closing(self.real_connect(self.database)) as writer:
            writer.execute("CREATE TABLE trajectory_metadata_blob (id TEXT, data BLOB)")
            writer.execute(
                "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, status INTEGER, metadata BLOB)"
            )
            writer.execute(
                "INSERT INTO trajectory_metadata_blob VALUES ('main', ?)",
                (b"\x12\xff\xff\xff",),
            )
            writer.execute(
                "INSERT INTO steps VALUES (0, 15, 3, ?)",
                (b"\x0a\x0c\x08\x80\x80\x80\x80\x80\x80\x80\x80\x80\x80\x01",),
            )
            writer.commit()

        turn_sec, session_sec, is_active = statusline.get_work_durations(
            {"session_id": "session"}, _now=2000
        )
        self.assertIsNone(turn_sec)
        self.assertIsNone(session_sec)
        self.assertFalse(is_active)

        with patch.object(statusline.sqlite3, "connect", self.capture_connection):
            self.assertEqual(
                statusline.get_upstream_response_model({"session_id": "../session"}), ""
            )
        self.assertEqual(self.connections, [])


class DurationFormattingTests(unittest.TestCase):
    def test_format_duration_tiers_and_invalid_inputs(self):
        self.assertEqual(statusline.format_duration(0), "0s")
        self.assertEqual(statusline.format_duration(42.9), "42s")
        self.assertEqual(statusline.format_duration(60), "1m 00s")
        self.assertEqual(statusline.format_duration(65), "1m 05s")
        self.assertEqual(statusline.format_duration(3600), "1h 00m")
        self.assertEqual(statusline.format_duration(3900), "1h 05m")
        self.assertEqual(statusline.format_duration(97200), "1d 3h")

        for invalid in (None, True, False, -1, float("nan"), float("inf"), "invalid"):
            with self.subTest(invalid=invalid):
                self.assertEqual(statusline.format_duration(invalid), "")

    def test_json_duration_payloads_and_empty_omission(self):
        empty_output = statusline.render_statusline({})
        self.assertNotIn("⏱️", empty_output)

        session_only_line1 = statusline.render_statusline(
            {"cost": {"total_duration_ms": 45000}}
        ).splitlines()[0]
        self.assertIn(f"⏱️ {statusline.CYAN}45s{statusline.RESET}", session_only_line1)
        self.assertNotIn("/", session_only_line1)

        with patch.object(statusline.sqlite3, "connect") as mock_connect:
            explicit_both = statusline.render_statusline(
                {
                    "model": {"display_name": "Gemini 4 Argon", "response_model": "argon"},
                    "agent_state": "reviewing",
                    "session": {"turn_duration_seconds": 42, "duration_seconds": 1110},
                }
            )
        mock_connect.assert_not_called()
        self.assertIn(f"⏱️ {statusline.YELLOW}42s{statusline.RESET}", explicit_both)
        self.assertIn("/ 18m 30s", explicit_both)


class ExecutionModeTests(unittest.TestCase):
    def test_displays_reported_modes_next_to_agent_state(self):
        for mode in ("plan", "auto", "accept-edits"):
            with self.subTest(mode=mode):
                line1, _ = statusline.render_statusline(
                    {"agent_state": "idle", "cycle_mode": mode}
                ).splitlines()
                self.assertIn(
                    f"{statusline.GREEN}idle{statusline.RESET} "
                    f"{statusline.GRAY}·{statusline.RESET} "
                    f"{statusline.CYAN}[{mode}]{statusline.RESET}",
                    line1,
                )

    def test_missing_or_unrecognized_mode_keeps_existing_layout(self):
        data = {"agent_state": "idle"}
        expected = statusline.render_statusline(data)
        for mode in (None, "", "unknown", ["plan"]):
            with self.subTest(mode=mode):
                self.assertEqual(
                    statusline.render_statusline({**data, "cycle_mode": mode}),
                    expected,
                )


class QuotaSelectionTests(unittest.TestCase):
    MULTI_GROUP_QUOTA = {
        "3p-5h": {"remaining_fraction": 1.0, "reset_in_seconds": 18000},
        "3p-weekly": {"remaining_fraction": 0.84, "reset_in_seconds": 604800},
        "gemini-5h": {"remaining_fraction": 0.9032, "reset_in_seconds": 10037},
        "gemini-weekly": {"remaining_fraction": 0.9667, "reset_in_seconds": 407635},
    }

    def test_matches_third_party_models_to_3p_buckets(self):
        for model in (
            {"id": "Claude Opus 5.5 (High)", "display_name": "Claude Opus 5.5 (High)"},
            {"id": "Claude Sonnet 5.5 (Low)", "display_name": "Claude Sonnet 5.5 (Low)"},
            {"id": "GPT-OSS 120B (Medium)", "display_name": "GPT-OSS 120B (Medium)"},
            {"id": "claude-opus-5-5-high", "display_name": "Custom Alias"},
        ):
            with self.subTest(model=model):
                _, line2 = statusline.render_statusline(
                    {"model": model, "quota": self.MULTI_GROUP_QUOTA}
                ).splitlines()
                self.assertIn(f"⏳ 5h: {statusline.GREEN}100%{statusline.RESET}", line2)
                self.assertIn("(5h 0m)", line2)
                self.assertIn(f"📅 Wk: {statusline.GREEN}84%{statusline.RESET}", line2)
                self.assertIn("(168h 0m)", line2)

    def test_matches_gemini_and_orchestrator_models_to_gemini_buckets(self):
        for model in (
            {"id": "Gemini 4 Neon (XHigh)", "display_name": "Gemini 4 Neon (XHigh)"},
            {"id": "Gemini 3.8 Flash (High)", "display_name": "Gemini 3.8 Flash (High)"},
            {"id": "Orchestrator (Neon)", "display_name": "Orchestrator (Neon)"},
        ):
            with self.subTest(model=model):
                _, line2 = statusline.render_statusline(
                    {"model": model, "quota": self.MULTI_GROUP_QUOTA}
                ).splitlines()
                self.assertIn(f"⏳ 5h: {statusline.GREEN}90%{statusline.RESET}", line2)
                self.assertIn("(2h 47m)", line2)
                self.assertIn(f"📅 Wk: {statusline.GREEN}97%{statusline.RESET}", line2)
                self.assertIn("(113h 13m)", line2)

    def test_prefers_specific_subgroups_when_available(self):
        split_quota = {
            "3p-5h": {"remaining_fraction": 1.0, "reset_in_seconds": 18000},
            "gemini-flash-5h": {"remaining_fraction": 0.72, "reset_in_seconds": 7200},
            "gemini-pro-5h": {"remaining_fraction": 0.41, "reset_in_seconds": 3600},
            "gemini-5h": {"remaining_fraction": 0.55, "reset_in_seconds": 5400},
        }
        flash_5h, _ = statusline.select_model_quotas(
            split_quota, model_name="Gemini 3.8 Flash (High)"
        )
        self.assertEqual(flash_5h["remaining_fraction"], 0.72)

        # Without a bare 'gemini' bucket, non-flash Gemini prefers 'gemini-pro'.
        no_bare = {k: v for k, v in split_quota.items() if k != "gemini-5h"}
        neon_5h, _ = statusline.select_model_quotas(
            no_bare, model_name="Gemini 4 Neon (XHigh)"
        )
        self.assertEqual(neon_5h["remaining_fraction"], 0.41)

    def test_disabled_buckets_and_cross_family_mismatch_do_not_leak(self):
        disabled_3p = {
            "3p-5h": {"remaining_fraction": 1.0, "disabled": True},
            "3p-weekly": {"remaining_fraction": 1.0, "disabled": True},
            "gemini-5h": {"remaining_fraction": 0.50, "reset_in_seconds": 3600},
            "gemini-weekly": {"remaining_fraction": 0.80, "reset_in_seconds": 7200},
        }
        _, line2 = statusline.render_statusline(
            {"model": {"display_name": "Claude Opus 5.5 (High)"}, "quota": disabled_3p}
        ).splitlines()
        self.assertNotIn("⏳ 5h:", line2)
        self.assertNotIn("📅 Wk:", line2)

        # Partial disable keeps the enabled window from the matching group only.
        partial_3p = {
            **disabled_3p,
            "3p-weekly": {"remaining_fraction": 0.75, "reset_in_seconds": 7200},
        }
        five_h, weekly = statusline.select_model_quotas(
            partial_3p, model_name="Claude Opus 5.5 (High)"
        )
        self.assertIsNone(five_h)
        self.assertEqual(weekly["remaining_fraction"], 0.75)

        # Single-group Gemini payload must not render under a 3p model.
        gemini_only = {
            "gemini-5h": {"remaining_fraction": 0.50, "reset_in_seconds": 3600},
            "gemini-weekly": {"remaining_fraction": 0.80, "reset_in_seconds": 7200},
        }
        self.assertEqual(
            statusline.select_model_quotas(gemini_only, model_name="Claude Opus 5.5 (High)"),
            (None, None),
        )

    def test_preserves_unprefixed_single_group_and_ambiguous_omission(self):
        legacy = {
            "five_hour": {"remaining_fraction": 0.95, "reset_in_seconds": 17760},
            "weekly": {"remaining_fraction": 0.99, "reset_in_seconds": 604560},
        }
        for model_name in ("", "Claude Opus 5.5 (High)", "Gemini 4 Argon (High)", "Unknown Model"):
            with self.subTest(model_name=model_name):
                five_h, weekly = statusline.select_model_quotas(legacy, model_name=model_name)
                self.assertEqual(five_h["remaining_fraction"], 0.95)
                self.assertEqual(weekly["remaining_fraction"], 0.99)

        # Omitted model with single prefixed group still renders (official doc shape).
        doc_example = {"gemini-weekly": {"remaining_fraction": 0.9378, "reset_in_seconds": 560580}}
        _, line2 = statusline.render_statusline({"quota": doc_example}).splitlines()
        self.assertIn(f"📅 Wk: {statusline.GREEN}94%{statusline.RESET}", line2)

        # Ambiguous multi-group payload with unknown or omitted model omits quota safely.
        for model_name in ("", "Mystery Model 1.0"):
            with self.subTest(ambiguous_model=model_name):
                self.assertEqual(
                    statusline.select_model_quotas(self.MULTI_GROUP_QUOTA, model_name=model_name),
                    (None, None),
                )

    def test_zero_remaining_and_non_positive_reset_seconds(self):
        exhausted = {
            "3p-5h": {"remaining_fraction": 0, "reset_in_seconds": -5},
            "3p-weekly": {"remaining_fraction": 0.15, "reset_in_seconds": 0},
        }
        _, line2 = statusline.render_statusline(
            {"model": {"display_name": "Claude Opus 5.5 (High)"}, "quota": exhausted}
        ).splitlines()
        self.assertIn(f"⏳ 5h: {statusline.RED}0%{statusline.RESET}", line2)
        self.assertNotIn("()", line2)
        self.assertIn(f"📅 Wk: {statusline.YELLOW}15%{statusline.RESET}", line2)


class SharedQuotaSyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="agy-quota-sync-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db_path = self.root / "statusline_quota.db"

    @staticmethod
    def _iso(epoch):
        from datetime import datetime, timezone

        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _payload(
        self,
        email="user@example.com",
        model_name="Gemini 4 Neon (XHigh)",
        g5=0.95,
        g5_reset=None,
        gw=0.98,
        gw_reset=None,
        p5=1.0,
        p5_reset=None,
        pw=1.0,
        pw_reset=None,
        now=1_800_000_000,
    ):
        g5_reset = (now + 10_000) if g5_reset is None else g5_reset
        gw_reset = (now + 400_000) if gw_reset is None else gw_reset
        p5_reset = (now + 18_000) if p5_reset is None else p5_reset
        pw_reset = (now + 604_800) if pw_reset is None else pw_reset
        payload = {
            "model": {"id": model_name, "display_name": model_name},
            "quota": {
                "3p-5h": {
                    "remaining_fraction": p5,
                    "reset_time": self._iso(p5_reset),
                    "reset_in_seconds": max(0, int(p5_reset - now)),
                },
                "3p-weekly": {
                    "remaining_fraction": pw,
                    "reset_time": self._iso(pw_reset),
                    "reset_in_seconds": max(0, int(pw_reset - now)),
                },
                "gemini-5h": {
                    "remaining_fraction": g5,
                    "reset_time": self._iso(g5_reset),
                    "reset_in_seconds": max(0, int(g5_reset - now)),
                },
                "gemini-weekly": {
                    "remaining_fraction": gw,
                    "reset_time": self._iso(gw_reset),
                    "reset_in_seconds": max(0, int(gw_reset - now)),
                },
            },
        }
        if email is not None:
            payload["email"] = email
        return payload

    @staticmethod
    def _frozen_tick(base_payload, now):
        """Simulate agy ticking reset_in_seconds locally without calling doRefreshQuota."""
        ticked = json.loads(json.dumps(base_payload))
        for bucket in ticked.get("quota", {}).values():
            if isinstance(bucket, dict) and bucket.get("reset_time"):
                reset_epoch = statusline._parse_iso_timestamp(bucket["reset_time"])
                if reset_epoch is not None:
                    bucket["reset_in_seconds"] = max(0, int(reset_epoch - now))
        return ticked

    def test_parse_iso_timestamp_handles_rfc3339_variants(self):
        expected = statusline._parse_iso_timestamp("2026-11-20T14:35:33Z")
        self.assertIsNotNone(expected)
        self.assertAlmostEqual(
            statusline._parse_iso_timestamp("2026-11-20t14:35:33z"), expected, places=6
        )
        self.assertAlmostEqual(
            statusline._parse_iso_timestamp("2026-11-20T22:35:33+08:00"), expected, places=6
        )
        self.assertAlmostEqual(
            statusline._parse_iso_timestamp("2026-11-20T14:35:33.5Z"), expected + 0.5, places=6
        )
        self.assertAlmostEqual(
            statusline._parse_iso_timestamp("2026-11-20T14:35:33.123456789Z"),
            expected + 0.123456,
            places=6,
        )
        for bad in (None, "", "   ", 123, "not-a-timestamp"):
            with self.subTest(bad=bad):
                self.assertIsNone(statusline._parse_iso_timestamp(bad))

    def test_terminal_identity_skips_shells_and_guards_roots_and_cycles(self):
        # Windows: python -> cmd.exe -> agy.exe
        win_tree = {
            200: (100, r"C:\Windows\System32\cmd.exe", "2000", 1005.0),
            100: (50, r"C:\Users\h\.gemini\bin\agy.exe", "1000", 900.0),
        }
        tid, start_ts = statusline._get_terminal_identity(
            _inspector=win_tree.get, _start_pid=200, _platform_tag="win"
        )
        self.assertEqual(tid, "win:100:1000")
        self.assertEqual(start_ts, 900.0)

        # Nested cmd.exe (.cmd launcher): python -> cmd -> cmd -> agy.exe
        nested_tree = {
            300: (200, "cmd.exe", "3000", 1006.0),
            200: (100, "cmd.exe", "2000", 1005.0),
            100: (50, "agy.exe", "1000", 900.0),
        }
        tid, start_ts = statusline._get_terminal_identity(
            _inspector=nested_tree.get, _start_pid=300, _platform_tag="win"
        )
        self.assertEqual(tid, "win:100:1000")
        self.assertEqual(start_ts, 900.0)

        # Unix: /bin/sh exec optimization -> direct agy parent
        unix_direct = {100: (50, "agy", "5555", 800.0)}
        tid, start_ts = statusline._get_terminal_identity(
            _inspector=unix_direct.get, _start_pid=100, _platform_tag="linux"
        )
        self.assertEqual(tid, "linux:100:5555")
        self.assertEqual(start_ts, 800.0)

        # Unreadable parent above cmd.exe must NOT misidentify transient cmd.exe
        wrapper_only = {200: (100, "cmd.exe", "2000", 1005.0)}
        self.assertEqual(
            statusline._get_terminal_identity(
                _inspector=wrapper_only.get, _start_pid=200, _platform_tag="win"
            ),
            (None, None),
        )

        # Orphan under init/systemd and cyclic parent chains fail safely
        orphan_tree = {200: (1, "sh", "2000", 1005.0), 1: (0, "systemd", "1", 0.0)}
        self.assertEqual(
            statusline._get_terminal_identity(
                _inspector=orphan_tree.get, _start_pid=200, _platform_tag="linux"
            ),
            (None, None),
        )
        cyclic_tree = {
            200: (300, "sh", "2000", 1005.0),
            300: (200, "bash", "3000", 1004.0),
        }
        self.assertEqual(
            statusline._get_terminal_identity(
                _inspector=cyclic_tree.get, _start_pid=200, _platform_tag="linux"
            ),
            (None, None),
        )

        # Linux /proc/<pid>/stat parsing with spaces and parentheses in comm
        fields_after_state = ["0"] * 19
        fields_after_state[0] = "1234"  # ppid (field 4)
        fields_after_state[18] = "987654"  # starttime (field 22)
        stat_line = f"5678 (agy (dev)) S {' '.join(fields_after_state)}\n"
        self.assertEqual(statusline._parse_linux_proc_stat(stat_line), (1234, "agy (dev)", 987654))

    def test_confirmed_refresh_syncs_and_handles_non_monotonic_jitter(self):
        t0 = 1_800_000_000.0
        g5_reset = t0 + 10_000
        gw_reset = t0 + 400_000
        term_a = ("win:101:1", t0 - 2.0)  # Fresh start -> CONFIRMED
        term_b = ("win:202:1", t0 - 1.0)

        # Terminal A starts with 97%
        p_a = self._payload(g5=0.97, g5_reset=g5_reset, gw=0.98, gw_reset=gw_reset, now=t0)
        p_a_orig = json.dumps(p_a, sort_keys=True)
        out_a = statusline.sync_shared_quota(
            p_a, db_path=self.db_path, _now=t0, _terminal_identity=term_a
        )
        self.assertEqual(json.dumps(p_a, sort_keys=True), p_a_orig)  # Input not mutated
        self.assertAlmostEqual(out_a["quota"]["gemini-5h"]["remaining_fraction"], 0.97)

        # Terminal A ticking with identical fingerprint performs zero write transactions
        real_connect = sqlite3.connect
        tx_starts = []

        class CountingConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if isinstance(sql, str) and sql.strip().upper().startswith("BEGIN"):
                    tx_starts.append(sql)
                return super().execute(sql, *args, **kwargs)

        with patch.object(
            statusline.sqlite3,
            "connect",
            lambda *a, **kw: real_connect(*a, factory=CountingConnection, **kw),
        ):
            p_a_tick = self._payload(
                g5=0.97, g5_reset=g5_reset, gw=0.98, gw_reset=gw_reset, now=t0 + 15, p5_reset=t0 + 18_000, pw_reset=t0 + 604_800
            )
            statusline.sync_shared_quota(
                p_a_tick, db_path=self.db_path, _now=t0 + 15, _terminal_identity=term_a
            )
        self.assertEqual(tx_starts, [])

        # Terminal B registers at 97%, then refreshes at t0+60 to 90.95%
        statusline.sync_shared_quota(
            self._payload(g5=0.97, g5_reset=g5_reset, gw=0.98, gw_reset=gw_reset, now=t0 + 5),
            db_path=self.db_path,
            _now=t0 + 5,
            _terminal_identity=term_b,
        )
        statusline.sync_shared_quota(
            self._payload(
                g5=0.9095374, g5_reset=g5_reset, gw=0.9682614, gw_reset=gw_reset, now=t0 + 60
            ),
            db_path=self.db_path,
            _now=t0 + 60,
            _terminal_identity=term_b,
        )

        # Busy Terminal A still sends frozen 97%, immediately receives 90.95% + live countdown
        synced_a = statusline.sync_shared_quota(
            self._payload(
                g5=0.97, g5_reset=g5_reset, gw=0.98, gw_reset=gw_reset, now=t0 + 75, p5_reset=t0 + 18_000, pw_reset=t0 + 604_800
            ),
            db_path=self.db_path,
            _now=t0 + 75,
            _terminal_identity=term_a,
        )
        self.assertAlmostEqual(synced_a["quota"]["gemini-5h"]["remaining_fraction"], 0.9095374)
        self.assertEqual(synced_a["quota"]["gemini-5h"]["reset_in_seconds"], int(g5_reset - (t0 + 75)))

        # Non-monotonic jitter: Terminal B refreshes upward to 91.51% -> newest CONFIRMED wins
        statusline.sync_shared_quota(
            self._payload(
                g5=0.9151157, g5_reset=g5_reset, gw=0.961269, gw_reset=gw_reset, now=t0 + 120
            ),
            db_path=self.db_path,
            _now=t0 + 120,
            _terminal_identity=term_b,
        )
        synced_a2 = statusline.sync_shared_quota(
            self._payload(
                g5=0.97, g5_reset=g5_reset, gw=0.98, gw_reset=gw_reset, now=t0 + 125, p5_reset=t0 + 18_000, pw_reset=t0 + 604_800
            ),
            db_path=self.db_path,
            _now=t0 + 125,
            _terminal_identity=term_a,
        )
        self.assertAlmostEqual(synced_a2["quota"]["gemini-5h"]["remaining_fraction"], 0.9151157)
        self.assertAlmostEqual(synced_a2["quota"]["gemini-weekly"]["remaining_fraction"], 0.961269)

        # Brand-new Terminal C starts at t0+200 -> CONFIRMED immediately updates all terminals
        term_c = ("win:303:1", t0 + 198.0)
        statusline.sync_shared_quota(
            self._payload(
                g5=0.90321046, g5_reset=g5_reset, gw=0.9666797, gw_reset=gw_reset, now=t0 + 200
            ),
            db_path=self.db_path,
            _now=t0 + 200,
            _terminal_identity=term_c,
        )
        synced_a3 = statusline.sync_shared_quota(
            self._payload(
                g5=0.97, g5_reset=g5_reset, gw=0.98, gw_reset=gw_reset, now=t0 + 205, p5_reset=t0 + 18_000, pw_reset=t0 + 604_800
            ),
            db_path=self.db_path,
            _now=t0 + 205,
            _terminal_identity=term_a,
        )
        self.assertAlmostEqual(synced_a3["quota"]["gemini-5h"]["remaining_fraction"], 0.90321046)

        with contextlib.closing(sqlite3.connect(self.db_path)) as con:
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")

    def test_cold_start_disambiguation_and_delayed_login_promotion(self):
        t_now = 1_800_010_000.0
        g5_reset = t_now + 8_000
        gw_reset = t_now + 400_000

        # Both orderings with a 100% sliding 3p bucket: newer sliding clock wins even with upward jitter
        for order_name, sequence in (
            ("stale_then_fresh", ("stale", "fresh")),
            ("fresh_then_stale", ("fresh", "stale")),
        ):
            with self.subTest(order=order_name):
                db = self.root / f"cold_sliding_{order_name}.db"
                old_a = ("win:11:1", t_now - 7200.0)
                old_b = ("win:22:1", t_now - 3600.0)
                stale_payload = self._payload(
                    g5=0.9095,
                    g5_reset=g5_reset,
                    gw=0.968,
                    gw_reset=gw_reset,
                    p5=1.0,
                    p5_reset=(t_now - 1800) + 18_000,  # fetched 30m ago
                    pw=1.0,
                    pw_reset=(t_now - 1800) + 604_800,
                    now=t_now,
                )
                fresh_payload = self._payload(
                    g5=0.9151,  # higher due to jitter, but fetched 1m ago!
                    g5_reset=g5_reset,
                    gw=0.961,
                    gw_reset=gw_reset,
                    p5=1.0,
                    p5_reset=(t_now - 60) + 18_000,  # fetched 1m ago
                    pw=1.0,
                    pw_reset=(t_now - 60) + 604_800,
                    now=t_now,
                )
                for tag in sequence:
                    if tag == "stale":
                        statusline.sync_shared_quota(
                            stale_payload, db_path=db, _now=t_now, _terminal_identity=old_a
                        )
                    else:
                        statusline.sync_shared_quota(
                            fresh_payload, db_path=db, _now=t_now, _terminal_identity=old_b
                        )
                out = statusline.sync_shared_quota(
                    stale_payload, db_path=db, _now=t_now + 1, _terminal_identity=old_a
                )
                self.assertAlmostEqual(out["quota"]["gemini-5h"]["remaining_fraction"], 0.9151)

        # When all buckets are <100% (no sliding clock), same-window lower remaining wins both orders
        for order_name, sequence in (
            ("high_then_low", ("high", "low")),
            ("low_then_high", ("low", "high")),
        ):
            with self.subTest(all_used_order=order_name):
                db = self.root / f"cold_used_{order_name}.db"
                old_a = ("win:33:1", t_now - 7200.0)
                old_b = ("win:44:1", t_now - 3600.0)
                high_p = self._payload(
                    g5=0.97, g5_reset=g5_reset, gw=0.98, gw_reset=gw_reset,
                    p5=0.80, p5_reset=g5_reset, pw=0.90, pw_reset=gw_reset, now=t_now,
                )
                low_p = self._payload(
                    g5=0.82, g5_reset=g5_reset, gw=0.94, gw_reset=gw_reset,
                    p5=0.75, p5_reset=g5_reset, pw=0.89, pw_reset=gw_reset, now=t_now,
                )
                for tag in sequence:
                    statusline.sync_shared_quota(
                        high_p if tag == "high" else low_p,
                        db_path=db,
                        _now=t_now,
                        _terminal_identity=old_a if tag == "high" else old_b,
                    )
                out = statusline.sync_shared_quota(
                    high_p, db_path=db, _now=t_now + 1, _terminal_identity=old_a
                )
                self.assertAlmostEqual(out["quota"]["gemini-5h"]["remaining_fraction"], 0.82)

        # Cold-start old terminal must never overwrite a CONFIRMED cache entry, while
        # delayed login (pre-quota registration followed by first quota >30s later) IS CONFIRMED.
        db_delay = self.root / "delayed_login.db"
        fresh_term = ("win:55:1", t_now - 3.0)
        statusline.sync_shared_quota(
            self._payload(g5=0.85, g5_reset=g5_reset, now=t_now),
            db_path=db_delay,
            _now=t_now,
            _terminal_identity=fresh_term,
        )
        old_term = ("win:66:1", t_now - 5000.0)
        old_payload = self._payload(g5=0.97, g5_reset=g5_reset, now=t_now + 2)
        out_old = statusline.sync_shared_quota(
            old_payload,
            db_path=db_delay,
            _now=t_now + 2,
            _terminal_identity=old_term,
        )
        self.assertAlmostEqual(out_old["quota"]["gemini-5h"]["remaining_fraction"], 0.85)

        delayed_term = ("win:77:1", t_now - 100.0)
        pre_quota = {"email": "user@example.com", "agent_state": "idle"}
        self.assertEqual(
            statusline.sync_shared_quota(
                pre_quota, db_path=db_delay, _now=t_now + 5, _terminal_identity=delayed_term
            ),
            pre_quota,
        )
        # First quota arrives later -> transition from "" -> fp is CONFIRMED
        statusline.sync_shared_quota(
            self._payload(g5=0.88, g5_reset=g5_reset, now=t_now + 20),
            db_path=db_delay,
            _now=t_now + 20,
            _terminal_identity=delayed_term,
        )
        out_after_delay = statusline.sync_shared_quota(
            self._frozen_tick(old_payload, t_now + 25),
            db_path=db_delay,
            _now=t_now + 25,
            _terminal_identity=old_term,
        )
        self.assertAlmostEqual(out_after_delay["quota"]["gemini-5h"]["remaining_fraction"], 0.88)

    def test_window_rollover_cross_model_and_account_isolation(self):
        t0 = 1_800_020_000.0
        old_reset = t0 + 600  # expires at t0+600
        new_reset = t0 + 600 + 18_000
        gw_reset = t0 + 400_000

        # Account A: Claude terminal refreshes -> Gemini terminal sees updated gemini-5h
        claude_term = ("win:10:1", t0 - 2.0)
        gemini_term = ("win:20:1", t0 - 5000.0)
        statusline.sync_shared_quota(
            self._payload(
                email="Alice@Example.com",
                model_name="Claude Opus 5.5 (High)",
                g5=0.64,
                g5_reset=old_reset,
                gw=0.91,
                gw_reset=gw_reset,
                p5=0.88,
                p5_reset=old_reset,
                now=t0,
            ),
            db_path=self.db_path,
            _now=t0,
            _terminal_identity=claude_term,
        )

        # Account B: completely isolated
        bob_term = ("win:30:1", t0 - 1.0)
        statusline.sync_shared_quota(
            self._payload(
                email="bob@example.com",
                model_name="Gemini 4 Neon (XHigh)",
                g5=0.99,
                g5_reset=old_reset,
                gw=0.99,
                gw_reset=gw_reset,
                now=t0 + 1,
            ),
            db_path=self.db_path,
            _now=t0 + 1,
            _terminal_identity=bob_term,
        )

        # Alice's Gemini terminal (lowercase email) gets 64% from Alice's Claude terminal, not Bob's 99%
        alice_gemini = statusline.sync_shared_quota(
            self._payload(
                email="alice@example.com",
                model_name="Gemini 4 Neon (XHigh)",
                g5=0.95,
                g5_reset=old_reset,
                gw=0.98,
                gw_reset=gw_reset,
                now=t0 + 10,
            ),
            db_path=self.db_path,
            _now=t0 + 10,
            _terminal_identity=gemini_term,
        )
        _, line2 = statusline.render_statusline(alice_gemini).splitlines()
        self.assertIn(f"⏳ 5h: {statusline.GREEN}64%{statusline.RESET}", line2)
        self.assertIn(f"📅 Wk: {statusline.GREEN}91%{statusline.RESET}", line2)

        # Window rollover: after old_reset expires, a new window (100%) supersedes expired 64%
        rollover_now = old_reset + 120
        statusline.sync_shared_quota(
            self._payload(
                email="alice@example.com",
                g5=1.0,
                g5_reset=new_reset,
                gw=0.91,
                gw_reset=gw_reset,
                now=rollover_now,
            ),
            db_path=self.db_path,
            _now=rollover_now,
            _terminal_identity=("win:40:1", rollover_now - 3600.0),  # even cold-start beats expired window
        )
        alice_after_rollover = statusline.sync_shared_quota(
            self._payload(
                email="alice@example.com",
                g5=0.95,
                g5_reset=old_reset,
                gw=0.98,
                gw_reset=gw_reset,
                now=rollover_now + 5,
            ),
            db_path=self.db_path,
            _now=rollover_now + 5,
            _terminal_identity=gemini_term,
        )
        self.assertAlmostEqual(alice_after_rollover["quota"]["gemini-5h"]["remaining_fraction"], 1.0)
        self.assertEqual(
            alice_after_rollover["quota"]["gemini-5h"]["reset_in_seconds"],
            int(new_reset - (rollover_now + 5)),
        )

        # Missing email or opt-out env var skips shared sync cleanly
        no_email = self._payload(email=None, g5=0.50, g5_reset=new_reset, now=rollover_now)
        self.assertIs(
            statusline.sync_shared_quota(
                no_email, db_path=self.db_path, _now=rollover_now, _terminal_identity=gemini_term
            ),
            no_email,
        )
        with patch.dict(os.environ, {"AGY_STATUSLINE_SHARED_QUOTA": "0"}):
            self.assertEqual(statusline._resolve_quota_cache_db_path(), "")

    def test_resilience_write_lock_corrupt_db_pruning_and_concurrency(self):
        import threading

        t0 = 1_800_030_000.0
        g5_reset = t0 + 10_000
        term_a = ("win:501:1", t0 - 2.0)
        term_b = ("win:502:1", t0 - 2.0)

        # Seed cache with 90% from Terminal A
        term_a_payload = self._payload(g5=0.90, g5_reset=g5_reset, now=t0)
        statusline.sync_shared_quota(
            term_a_payload,
            db_path=self.db_path,
            _now=t0,
            _terminal_identity=term_a,
        )
        # Seed Terminal B with 90%
        statusline.sync_shared_quota(
            self._payload(g5=0.90, g5_reset=g5_reset, now=t0 + 1),
            db_path=self.db_path,
            _now=t0 + 1,
            _terminal_identity=term_b,
        )

        # Simulate transient write lock when Terminal B refreshes to 72%:
        # must keep Terminal B's fresh 72% instead of reverting to stale cached 90%.
        with patch.object(
            statusline, "_commit_quota_update", side_effect=sqlite3.OperationalError("database is locked")
        ):
            locked_refresh = statusline.sync_shared_quota(
                self._payload(g5=0.72, g5_reset=g5_reset, now=t0 + 30),
                db_path=self.db_path,
                _now=t0 + 30,
                _terminal_identity=term_b,
            )
        self.assertAlmostEqual(locked_refresh["quota"]["gemini-5h"]["remaining_fraction"], 0.72)

        # Update cache to 72% normally, then fail heartbeat write for unchanged Terminal A (>30m later):
        # unchanged Terminal A must STILL display fresher cached 72%.
        statusline.sync_shared_quota(
            self._payload(g5=0.72, g5_reset=g5_reset, now=t0 + 31),
            db_path=self.db_path,
            _now=t0 + 31,
            _terminal_identity=term_b,
        )
        with patch.object(
            statusline, "_commit_quota_update", side_effect=sqlite3.OperationalError("database is locked")
        ):
            locked_heartbeat = statusline.sync_shared_quota(
                self._frozen_tick(term_a_payload, t0 + 2000),
                db_path=self.db_path,
                _now=t0 + 2000,
                _terminal_identity=term_a,
            )
        self.assertAlmostEqual(locked_heartbeat["quota"]["gemini-5h"]["remaining_fraction"], 0.72)

        # Corrupt .db file auto-recovers safely; non-.db file is NEVER unlinked
        corrupt_db = self.root / "corrupt.db"
        corrupt_db.write_bytes(b"not a valid sqlite database payload!!!")
        p = self._payload(g5=0.80, g5_reset=g5_reset, now=t0)
        fallback = statusline.sync_shared_quota(
            p, db_path=corrupt_db, _now=t0, _terminal_identity=term_a
        )
        self.assertAlmostEqual(fallback["quota"]["gemini-5h"]["remaining_fraction"], 0.80)
        self.assertFalse(corrupt_db.exists())
        # Next call recreates clean DB
        statusline.sync_shared_quota(
            p, db_path=corrupt_db, _now=t0 + 1, _terminal_identity=term_a
        )
        self.assertTrue(corrupt_db.exists())

        precious_json = self.root / "settings.json"
        precious_json.write_text('{"theme":"dark"}', encoding="utf-8")
        statusline.sync_shared_quota(
            p, db_path=precious_json, _now=t0, _terminal_identity=term_a
        )
        self.assertTrue(precious_json.exists())

        # Concurrent multi-thread writers + readers on WAL DB complete cleanly without errors
        concurrent_db = self.root / "concurrent.db"
        errors = []

        def worker(idx):
            try:
                tid = (f"win:{1000 + idx}:1", t0 - 1.0)
                for step in range(5):
                    ts = t0 + step * 10 + idx * 0.1
                    frac = max(0.1, 0.95 - step * 0.05 - idx * 0.001)
                    statusline.sync_shared_quota(
                        self._payload(g5=frac, g5_reset=g5_reset, now=ts),
                        db_path=concurrent_db,
                        _now=ts,
                        _terminal_identity=tid,
                    )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=10)
        self.assertEqual(errors, [])

        # Pruning removes >24h old terminal rows on the next write
        later = t0 + 90_000.0
        statusline.sync_shared_quota(
            self._payload(g5=0.99, g5_reset=later + 18_000, now=later),
            db_path=concurrent_db,
            _now=later,
            _terminal_identity=("win:9999:1", later - 1.0),
        )
        with contextlib.closing(sqlite3.connect(concurrent_db)) as con:
            remaining_terms = con.execute("SELECT terminal_id FROM terminal_state").fetchall()
        self.assertEqual(remaining_terms, [("win:9999:1",)])


class StatuslineInputTests(unittest.TestCase):
    command = [sys.executable, "-S", statusline.__file__]

    def test_reads_complete_utf8_payload_across_chunks(self):
        data = {"padding": "x" * 160_000, "model": "Gemini 輸入"}
        completed = subprocess.run(
            self.command,
            input=json.dumps(data, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            check=True,
            timeout=4,
        )
        self.assertEqual(completed.stderr, b"")
        self.assertEqual(
            completed.stdout.decode("utf-8").splitlines(),
            statusline.render_statusline(data).splitlines(),
        )

    def test_exits_when_stdin_stays_open(self):
        data = {"model": "Gemini 輸入"}
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        ready = ["🧠 Context: Ready"]
        for label, raw, expected in (
            ("complete JSON", payload, statusline.render_statusline(data).splitlines()),
            ("empty input", b"", ready),
            ("incomplete JSON", b'{"model":', ready),
        ):
            with self.subTest(label=label):
                with subprocess.Popen(
                    self.command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ) as process:
                    try:
                        process.stdin.write(raw)
                        process.stdin.flush()
                        self.assertEqual(process.wait(timeout=4), 0)
                        self.assertEqual(process.stderr.read(), b"")
                        output = process.stdout.read().decode("utf-8")
                        self.assertEqual(output.splitlines(), expected)
                    finally:
                        if process.poll() is None:
                            process.kill()


class InstallationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="agy-install-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "Config # & (space) 中文"
        self.config.mkdir()
        self.settings = self.config / "settings.json"
        self.original = {"theme": "dark", "other": {"preserve": ["value"]}}
        self.settings.write_text(json.dumps(self.original), encoding="utf-8")
        patcher = patch.object(
            installer, "get_default_config_dir", return_value=str(self.config)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_installed_command_pins_interpreter_and_skips_site_with_hostile_path(self):
        environment = self.root / "Python & runtime"
        venv.EnvBuilder(with_pip=False).create(environment)
        executable = environment / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        with patch.object(installer.sys, "executable", str(executable)):
            with contextlib.redirect_stdout(io.StringIO()):
                installer.install()

        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(
            {key: value for key, value in settings.items() if key != "statusLine"},
            self.original,
        )
        backups = list(self.config.glob("settings.json.bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8")), self.original)

        # Use the installed command to observe interpreter, flags, and stdin.
        script = self.config / "statusline.py"
        script.write_text(
            "import json, sys\n"
            "print(json.dumps({'executable': sys.executable, "
            "'no_site': sys.flags.no_site, 'site_loaded': 'site' in sys.modules, "
            "'input': sys.stdin.read()}))\n",
            encoding="utf-8",
        )
        fake_path = self.root / "wrong-python"
        fake_path.mkdir()
        (fake_path / "python.cmd").write_text("@exit /b 99\n", encoding="ascii")
        env = dict(os.environ, PATH=str(fake_path))
        payload = '{"check":"stdin reaches the script"}'
        completed = installer.run_statusline_command(
            settings["statusLine"]["command"],
            input=payload, encoding="utf-8", capture_output=True,
            env=env, check=True, timeout=5,
        )
        observed = json.loads(completed.stdout)
        self.assertTrue(os.path.samefile(observed["executable"], executable.resolve()))
        self.assertEqual(observed["no_site"], 1)
        self.assertFalse(observed["site_loaded"])
        self.assertEqual(observed["input"], payload)

        script.write_text("raise SystemExit(7)\n", encoding="utf-8")
        completed = installer.run_statusline_command(
            settings["statusLine"]["command"],
            input="", capture_output=True, timeout=5, env=env,
        )
        self.assertEqual(completed.returncode, 7)

    def test_invalid_settings_are_preserved_before_copying_script(self):
        target = self.config / "statusline.py"
        target.write_text("original script", encoding="utf-8")
        for content in ("{invalid", "[]"):
            with self.subTest(content=content):
                self.settings.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    installer.install()
                self.assertEqual(self.settings.read_text(encoding="utf-8"), content)
                self.assertEqual(target.read_text(encoding="utf-8"), "original script")

    def test_failed_launch_does_not_publish_settings(self):
        with patch.object(
            installer, "run_statusline_command",
            side_effect=subprocess.CalledProcessError(1, "failed command"),
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(subprocess.CalledProcessError):
                    installer.install()
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8")), self.original)

    def test_windows_cmd_token_safety_rules(self):
        for safe in (
            r"C:\Users\h\AppData\Local\mise\installs\python\3.12.13\python.exe",
            r"C:\Users\h\.gemini\antigravity-cli\statusline.py",
            r"C:\Users\张三\.gemini\antigravity-cli\statusline.py",
            r"C:\Users\alice@example.com\statusline.py",
        ):
            with self.subTest(safe=safe):
                self.assertTrue(installer._is_windows_cmd_token_safe(safe))

        for unsafe in (
            "",
            r"@C:\Users\h\python.exe",
            "C:\\Users\\h\\",
            r"C:\Program Files\Python\python.exe",
            r"C:\Users\a&b\statusline.py",
            r"C:\Users\a(b)\statusline.py",
            r"C:\Users\a,b\statusline.py",
            r"C:\Users\a;b\statusline.py",
            r"C:\Users\a=b\statusline.py",
            r"C:\Users\a%OS%b\statusline.py",
            r"C:\Users\a!OS!b\statusline.py",
            r"C:\Users\a^b\statusline.py",
            "C:\\Users\\a\tb\\statusline.py",
        ):
            with self.subTest(unsafe=unsafe):
                self.assertFalse(installer._is_windows_cmd_token_safe(unsafe))

    @unittest.skipUnless(os.name == "nt", "Windows cmd.exe direct-launch behavior")
    def test_safe_windows_paths_launch_python_directly_without_cmd_wrapper(self):
        for label, dir_name, env_name in (
            ("ascii", "antigravity-cli", "python-runtime"),
            ("unicode", "antigravity_中文", "python_中文"),
        ):
            with self.subTest(label=label):
                safe_config = self.root / dir_name
                safe_config.mkdir()
                safe_settings = safe_config / "settings.json"
                safe_settings.write_text(json.dumps(self.original), encoding="utf-8")

                environment = self.root / env_name
                venv.EnvBuilder(with_pip=False).create(environment)
                executable = environment / "Scripts" / "python.exe"

                with patch.object(
                    installer, "get_default_config_dir", return_value=str(safe_config)
                ), patch.object(installer.sys, "executable", str(executable)):
                    with contextlib.redirect_stdout(io.StringIO()):
                        installer.install()

                settings = json.loads(safe_settings.read_text(encoding="utf-8"))
                command = settings["statusLine"]["command"]
                expected_exe = os.path.realpath(executable)
                expected_script = os.path.abspath(safe_config / "statusline.py")
                self.assertEqual(command, f"{expected_exe} -S {expected_script}")
                self.assertFalse((safe_config / "statusline.cmd").exists())
                self.assertEqual(list(safe_config.glob("*.tmp")), [])

                script = safe_config / "statusline.py"
                script.write_text(
                    "import json, sys\n"
                    "print(json.dumps({'executable': sys.executable, "
                    "'no_site': sys.flags.no_site, 'site_loaded': 'site' in sys.modules, "
                    "'input': sys.stdin.read()}))\n",
                    encoding="utf-8",
                )
                fake_path = self.root / f"wrong-python-{label}"
                fake_path.mkdir()
                (fake_path / "python.cmd").write_text("@exit /b 99\n", encoding="ascii")
                env = dict(os.environ, PATH=str(fake_path))
                payload = '{"check":"direct launch stdin"}'
                completed = installer.run_statusline_command(
                    command,
                    input=payload, encoding="utf-8", capture_output=True,
                    env=env, check=True, timeout=5,
                )
                self.assertEqual(completed.stderr, "")
                observed = json.loads(completed.stdout)
                self.assertTrue(os.path.samefile(observed["executable"], executable.resolve()))
                self.assertEqual(observed["no_site"], 1)
                self.assertFalse(observed["site_loaded"])
                self.assertEqual(observed["input"], payload)

                script.write_text("raise SystemExit(7)\n", encoding="utf-8")
                completed = installer.run_statusline_command(
                    command, input="", capture_output=True, timeout=5, env=env,
                )
                self.assertEqual(completed.returncode, 7)

    @unittest.skipUnless(os.name == "nt", "Windows cmd.exe fallback launcher behavior")
    def test_windows_cmd_fallback_handles_delimiters_and_delayed_expansion(self):
        delimiter_config = self.root / "dir,comma;semi=eq & 中文"
        delimiter_config.mkdir()
        delimiter_settings = delimiter_config / "settings.json"
        delimiter_settings.write_text(json.dumps(self.original), encoding="utf-8")

        environment = self.root / "py!OS! & 中文"
        venv.EnvBuilder(with_pip=False).create(environment)
        executable = environment / "Scripts" / "python.exe"

        with patch.object(
            installer, "get_default_config_dir", return_value=str(delimiter_config)
        ), patch.object(installer.sys, "executable", str(executable)):
            with contextlib.redirect_stdout(io.StringIO()):
                installer.install()

        settings = json.loads(delimiter_settings.read_text(encoding="utf-8"))
        command = settings["statusLine"]["command"]
        launcher = delimiter_config / "statusline.cmd"
        self.assertTrue(launcher.is_file())
        self.assertEqual(len(launcher.read_text(encoding="utf-8").splitlines()), 2)

        script = delimiter_config / "statusline.py"
        script.write_text(
            "import sys; print('OK:' + sys.stdin.read().strip())\n",
            encoding="utf-8",
        )
        shell = os.environ.get("COMSPEC", "cmd.exe")
        completed = subprocess.run(
            [shell, "/v:on", "/c", command],
            input='{"ok":1}', encoding="utf-8", capture_output=True,
            check=True, timeout=5,
        )
        self.assertEqual(completed.stderr, "")
        self.assertEqual(completed.stdout.splitlines(), ['OK:{"ok":1}'])

    @unittest.skipUnless(os.name == "nt", "Windows upgrade preserves legacy launcher for live sessions")
    def test_upgrade_to_direct_command_preserves_existing_legacy_cmd_file(self):
        safe_config = self.root / "upgrade-cli"
        safe_config.mkdir()
        safe_settings = safe_config / "settings.json"
        legacy_launcher = safe_config / "statusline.cmd"
        legacy_launcher.write_bytes(b"@echo legacy\r\n")
        safe_settings.write_text(
            json.dumps(
                {
                    **self.original,
                    "statusLine": {
                        "type": "command",
                        "command": str(legacy_launcher),
                        "enabled": True,
                    },
                }
            ),
            encoding="utf-8",
        )

        environment = self.root / "upgrade-python"
        venv.EnvBuilder(with_pip=False).create(environment)
        executable = environment / "Scripts" / "python.exe"

        with patch.object(
            installer, "get_default_config_dir", return_value=str(safe_config)
        ), patch.object(installer.sys, "executable", str(executable)):
            with contextlib.redirect_stdout(io.StringIO()):
                installer.install()

        settings = json.loads(safe_settings.read_text(encoding="utf-8"))
        self.assertEqual(
            settings["statusLine"]["command"],
            f"{os.path.realpath(executable)} -S {os.path.abspath(safe_config / 'statusline.py')}",
        )
        self.assertEqual(legacy_launcher.read_bytes(), b"@echo legacy\r\n")

    def test_atomic_write_retries_transient_lock_and_cleans_up_on_failure(self):
        target = self.config / "atomic-target.txt"
        target.write_bytes(b"old")

        real_replace = os.replace
        attempts = {"count": 0}

        def flaky_replace(src, dst):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise PermissionError("transient lock")
            return real_replace(src, dst)

        with patch.object(installer.os, "replace", side_effect=flaky_replace), patch.object(
            installer.time, "sleep"
        ):
            installer._atomic_write_bytes(str(target), b"new")
        self.assertEqual(attempts["count"], 2)
        self.assertEqual(target.read_bytes(), b"new")
        self.assertEqual(list(self.config.glob("*.tmp")), [])

        with patch.object(
            installer.os, "replace", side_effect=PermissionError("persistent lock")
        ), patch.object(installer.time, "sleep"):
            with self.assertRaises(PermissionError):
                installer._atomic_write_bytes(str(target), b"newer")
        self.assertEqual(target.read_bytes(), b"new")
        self.assertEqual(list(self.config.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
