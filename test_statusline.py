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
