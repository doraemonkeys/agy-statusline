"""Regression checks for read-only metadata access and installed launch commands."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
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


if __name__ == "__main__":
    unittest.main()
