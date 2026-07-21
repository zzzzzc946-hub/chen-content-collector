import contextlib
import http.client
import http.server
import importlib
import importlib.util
import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.parse
import urllib.request
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent


def load_collector():
    path = Path(__file__).with_name("content_link_collector.py")
    spec = importlib.util.spec_from_file_location("content_link_collector", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeCloudPublishJobs:
    def __init__(self):
        self.started = []
        self.start_error = None

    def start(self, daily_date):
        if self.start_error is not None:
            raise self.start_error
        self.started.append(daily_date)
        return {
            "state": "running",
            "stage": "preparing",
            "message": "正在准备日报",
            "dailyDate": daily_date,
        }

    def snapshot(self):
        return {"state": "running", "stage": "uploading", "message": "正在上传视频 1/1"}

    def latest(self):
        return {
            "dailyDate": "2026-07-13",
            "reportId": "report-1",
            "reportUrl": "https://worker.example.com/r/report-1",
        }


class FakeBinaryHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.headers[name] = value

    def end_headers(self):
        return None


def request_json(url, method="GET", payload=None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    return request_bytes(url, method=method, body=body)


def request_bytes(url, method="GET", body=None):
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def request_raw_json(url, content_length, body=b""):
    parsed = urllib.parse.urlparse(url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
    try:
        connection.putrequest("POST", parsed.path)
        connection.putheader("Content-Type", "application/json")
        if content_length is not None:
            connection.putheader("Content-Length", content_length)
        connection.endheaders(body)
        response = connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()


def request_setup_with_headers(url, headers, body=b'{"local_only":true}'):
    parsed = urllib.parse.urlparse(url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
    supplied = {str(name).lower() for name in headers}
    try:
        connection.putrequest("POST", parsed.path, skip_host="host" in supplied)
        for name, value in headers.items():
            connection.putheader(name, value)
        if "content-length" not in supplied:
            connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        response = connection.getresponse()
        response_headers = {name.lower(): value for name, value in response.getheaders()}
        return (
            response.status,
            json.loads(response.read().decode("utf-8")),
            response_headers,
        )
    finally:
        connection.close()


@contextlib.contextmanager
def desktop_http_server(handler):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        if thread.is_alive():
            raise RuntimeError("desktop test server did not stop")


def handler_with_client_address(handler, client_address):
    class AddressedHandler(handler):
        def do_GET(self):
            self.client_address = client_address
            super().do_GET()

        def do_POST(self):
            self.client_address = client_address
            super().do_POST()

    return AddressedHandler


class WebhookHelperTests(unittest.TestCase):
    def test_setup_status_marks_clean_install_incomplete(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            cfg = collector.load_config(Path(tmp) / "missing.json")

        status = collector.setup_status(cfg)

        self.assertFalse(status["complete"])
        self.assertIn("feishu", status["missing"])
        self.assertFalse(status["groups"]["feishu"]["configured"])

    def test_save_setup_never_persists_secrets_and_uses_stable_keychain_accounts(self):
        collector = load_collector()
        keychain = {}
        writes = []

        def read_keychain(service, account):
            return keychain.get((service, account), "")

        def write_keychain(service, account, value):
            writes.append((service, account, value))
            keychain[(service, account)] = value

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"

            result = collector.save_setup(
                {
                    "feishu_app_id": "cli_test",
                    "feishu_app_secret": "secret-value",
                    "feishu_app_token": "base-token",
                    "feishu_table_id": "tbl-test",
                    "publisher_worker_url": "https://worker.example.com",
                    "publisher_device_token": "publisher-secret",
                },
                config_path,
                keychain_reader=read_keychain,
                keychain_writer=write_keychain,
                keychain_deleter=lambda service, account: keychain.pop((service, account), None),
            )

            raw = config_path.read_text(encoding="utf-8")
            publisher_raw = (
                Path(tmp) / "max_daily_cloud" / "publisher" / "config.json"
            ).read_text(encoding="utf-8")
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                (
                    Path(tmp) / "max_daily_cloud" / "publisher" / "config.json"
                ).stat().st_mode
                & 0o777,
                0o600,
            )

        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("secret-value", raw)
        self.assertNotIn("publisher-secret", raw)
        self.assertNotIn("secret-value", publisher_raw)
        self.assertNotIn("publisher-secret", publisher_raw)
        self.assertNotIn("secret-value", serialized)
        self.assertNotIn("publisher-secret", serialized)
        self.assertEqual(
            writes,
            [
                (
                    collector.FEISHU_KEYCHAIN_SERVICE,
                    collector.FEISHU_KEYCHAIN_ACCOUNT,
                    "secret-value",
                ),
                (
                    collector.PUBLISHER_KEYCHAIN_SERVICE,
                    collector.PUBLISHER_KEYCHAIN_ACCOUNT,
                    "publisher-secret",
                ),
            ],
        )

    def test_save_setup_validates_entire_payload_before_writing_keychain_or_config(self):
        collector = load_collector()
        invalid_payloads = (
            None,
            [],
            {"unexpected": "value"},
            {"local_only": "yes"},
            {"feishu_app_id": 123},
            {"feishu_app_id": "cli_test"},
            {
                "feishu_app_id": "cli_test",
                "feishu_app_secret": "secret-value",
                "feishu_app_token": "base-token",
                "feishu_table_id": "tbl-test",
                "publisher_worker_url": "https://worker.example.com",
            },
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                writes = []
                config_path = Path(tmp) / "config.json"
                with self.assertRaises(ValueError):
                    collector.save_setup(
                        payload,
                        config_path,
                        keychain_writer=lambda *args: writes.append(args),
                    )
                self.assertEqual(writes, [])
                self.assertFalse(config_path.exists())

    def test_save_setup_atomic_failure_preserves_config_and_cleans_temp_file(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            original = '{"existing": true}\n'
            config_path.write_text(original, encoding="utf-8")
            keychain = {}

            with patch.object(collector.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(RuntimeError, "原有配置已恢复"):
                    collector.save_setup(
                        {
                            "feishu_app_id": "cli_test",
                            "feishu_app_secret": "secret-value",
                            "feishu_app_token": "base-token",
                            "feishu_table_id": "tbl-test",
                        },
                        config_path,
                        keychain_reader=lambda service, account: keychain.get(
                            (service, account), ""
                        ),
                        keychain_writer=lambda service, account, value: keychain.__setitem__(
                            (service, account), value
                        ),
                        keychain_deleter=lambda service, account: keychain.pop(
                            (service, account), None
                        ),
                    )

            self.assertEqual(config_path.read_text(encoding="utf-8"), original)
            self.assertEqual(list(Path(tmp).glob(".config.json.*.tmp")), [])
            self.assertEqual(keychain, {})

    def test_load_config_prefers_keychain_reference_and_uses_plaintext_only_without_one(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "feishu": {
                            "app_secret": "legacy-secret",
                            "app_secret_keychain_service": collector.FEISHU_KEYCHAIN_SERVICE,
                            "app_secret_keychain_account": collector.FEISHU_KEYCHAIN_ACCOUNT,
                        }
                    }
                ),
                encoding="utf-8",
            )
            calls = []

            referenced = collector.load_config(
                config_path,
                keychain_reader=lambda service, account: calls.append((service, account))
                or "keychain-secret",
            )
            config_path.write_text(
                json.dumps({"feishu": {"app_secret": "legacy-secret"}}),
                encoding="utf-8",
            )
            legacy = collector.load_config(
                config_path,
                keychain_reader=lambda *args: self.fail("unexpected Keychain read"),
            )

        self.assertEqual(referenced["feishu"]["app_secret"], "keychain-secret")
        self.assertEqual(
            calls,
            [(collector.FEISHU_KEYCHAIN_SERVICE, collector.FEISHU_KEYCHAIN_ACCOUNT)],
        )
        self.assertEqual(legacy["feishu"]["app_secret"], "legacy-secret")

    def test_load_config_requires_real_publisher_keychain_value_for_status(self):
        collector = load_collector()
        keychain = {
            (
                collector.FEISHU_KEYCHAIN_SERVICE,
                collector.FEISHU_KEYCHAIN_ACCOUNT,
            ): "feishu-secret",
        }
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "feishu": {
                            "app_id": "cli_test",
                            "app_token": "base-token",
                            "table_id": "tbl-test",
                            "app_secret_keychain_service": collector.FEISHU_KEYCHAIN_SERVICE,
                            "app_secret_keychain_account": collector.FEISHU_KEYCHAIN_ACCOUNT,
                        },
                        "publisher": {
                            "cloud_api_base": "https://worker.example.com",
                            "device_token_keychain_service": collector.PUBLISHER_KEYCHAIN_SERVICE,
                            "device_token_keychain_account": collector.PUBLISHER_KEYCHAIN_ACCOUNT,
                        },
                    }
                ),
                encoding="utf-8",
            )

            loaded = collector.load_config(
                config_path,
                keychain_reader=lambda service, account: keychain.get((service, account), ""),
            )

        self.assertEqual(loaded["publisher"].get("device_token"), "")
        self.assertFalse(collector.setup_status(loaded)["groups"]["publisher"]["configured"])

    def test_save_setup_reuses_only_non_empty_keychain_values_before_writes(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            original = json.dumps(
                {
                    "feishu": {
                        "app_id": "old-id",
                        "app_token": "old-token",
                        "table_id": "old-table",
                        "app_secret_keychain_service": collector.FEISHU_KEYCHAIN_SERVICE,
                        "app_secret_keychain_account": collector.FEISHU_KEYCHAIN_ACCOUNT,
                    }
                },
                indent=2,
            ).encode("utf-8")
            config_path.write_bytes(original)
            writes = []

            with self.assertRaisesRegex(ValueError, "Keychain"):
                collector.save_setup(
                    {
                        "feishu_app_id": "new-id",
                        "feishu_app_token": "new-token",
                        "feishu_table_id": "new-table",
                    },
                    config_path,
                    keychain_reader=lambda *args: "",
                    keychain_writer=lambda *args: writes.append(args),
                    keychain_deleter=lambda *args: None,
                )

            self.assertEqual(writes, [])
            self.assertEqual(config_path.read_bytes(), original)
            self.assertEqual(list(Path(tmp).rglob("*.tmp")), [])

    def test_save_setup_reuses_existing_real_keychain_values_and_reloads_status(self):
        collector = load_collector()
        keychain = {
            (
                collector.FEISHU_KEYCHAIN_SERVICE,
                collector.FEISHU_KEYCHAIN_ACCOUNT,
            ): "existing-feishu-secret",
            (
                collector.PUBLISHER_KEYCHAIN_SERVICE,
                collector.PUBLISHER_KEYCHAIN_ACCOUNT,
            ): "existing-publisher-secret",
        }
        reads = []
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "feishu": {
                            "app_secret_keychain_service": collector.FEISHU_KEYCHAIN_SERVICE,
                            "app_secret_keychain_account": collector.FEISHU_KEYCHAIN_ACCOUNT,
                        },
                        "publisher": {
                            "device_token_keychain_service": collector.PUBLISHER_KEYCHAIN_SERVICE,
                            "device_token_keychain_account": collector.PUBLISHER_KEYCHAIN_ACCOUNT,
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = collector.save_setup(
                {
                    "feishu_app_id": "new-id",
                    "feishu_app_token": "new-token",
                    "feishu_table_id": "new-table",
                    "publisher_worker_url": "https://worker.example.com",
                },
                config_path,
                keychain_reader=lambda service, account: reads.append((service, account))
                or keychain.get((service, account), ""),
                keychain_writer=lambda *args: self.fail("unexpected Keychain write"),
                keychain_deleter=lambda *args: self.fail("unexpected Keychain delete"),
            )
            saved = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertTrue(result["complete"])
        self.assertTrue(result["groups"]["feishu"]["configured"])
        self.assertTrue(result["groups"]["publisher"]["configured"])
        self.assertEqual(len(reads), 4)
        self.assertEqual(
            saved["feishu"]["app_secret_keychain_account"],
            collector.FEISHU_KEYCHAIN_ACCOUNT,
        )
        self.assertEqual(
            saved["publisher"]["device_token_keychain_account"],
            collector.PUBLISHER_KEYCHAIN_ACCOUNT,
        )
        self.assertNotIn("app_secret", saved["feishu"])
        self.assertNotIn("device_token", saved["publisher"])

    def test_save_setup_rolls_back_when_reloaded_secret_is_missing_without_sentinel(self):
        collector = load_collector()
        writes = []
        deletes = []
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"

            with self.assertRaisesRegex(RuntimeError, "原有配置已恢复") as raised:
                collector.save_setup(
                    {
                        "feishu_app_id": "new-id",
                        "feishu_app_secret": "new-feishu-secret",
                        "feishu_app_token": "new-token",
                        "feishu_table_id": "new-table",
                    },
                    config_path,
                    keychain_reader=lambda *args: "",
                    keychain_writer=lambda *args: writes.append(args),
                    keychain_deleter=lambda *args: deletes.append(args),
                )

            self.assertNotIn("new-feishu-secret", str(raised.exception))
            self.assertNotIn("stored-in-keychain", str(raised.exception))
            self.assertEqual(len(writes), 1)
            self.assertEqual(
                deletes,
                [(collector.FEISHU_KEYCHAIN_SERVICE, collector.FEISHU_KEYCHAIN_ACCOUNT)],
            )
            self.assertFalse(config_path.exists())
            self.assertEqual(list(Path(tmp).rglob("*.tmp")), [])

    def test_save_setup_local_only_skip_completes_without_keychain_writes(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            writes = []

            result = collector.save_setup(
                {"local_only": True},
                config_path,
                keychain_reader=lambda *args: self.fail("unexpected Keychain read"),
                keychain_writer=lambda *args: writes.append(args),
                keychain_deleter=lambda *args: self.fail("unexpected Keychain delete"),
            )

            saved = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertTrue(result["complete"])
        self.assertTrue(result["local_only"])
        self.assertTrue(saved["setup"]["local_only"])
        self.assertEqual(writes, [])

    def test_runtime_diagnostics_returns_only_redacted_capabilities(self):
        collector = load_collector()
        with patch.object(collector.shutil, "which", return_value="/private/tools/bin/tool"), patch.object(
            collector, "default_browser_executable_path", return_value="/Applications/Browser"
        ), patch.object(collector.importlib.util, "find_spec", return_value=object()):
            diagnostics = collector.runtime_diagnostics()

        self.assertEqual(
            set(diagnostics),
            {"python", "playwright", "yt_dlp", "ffmpeg", "browser"},
        )
        self.assertTrue(all(set(value) <= {"ok", "version", "name"} for value in diagnostics.values()))
        serialized = json.dumps(diagnostics, ensure_ascii=False)
        self.assertNotIn("/private/", serialized)
        self.assertNotIn("/Applications/", serialized)
        self.assertNotIn("argv", serialized.lower())
        self.assertNotIn("environment", serialized.lower())

        with patch.object(collector.shutil, "which", return_value=None), patch.object(
            collector, "default_browser_executable_path", return_value=""
        ), patch.object(
            collector.importlib.util,
            "find_spec",
            side_effect=ValueError("module has no spec"),
        ):
            unavailable = collector.runtime_diagnostics()
        self.assertFalse(unavailable["playwright"]["ok"])
        self.assertFalse(unavailable["browser"]["ok"])

    def test_runtime_diagnostics_and_setup_routes_work_in_clean_python_process(self):
        script = r'''
import http.server
import json
import tempfile
import threading
import urllib.request
from pathlib import Path

import content_link_collector as collector

diagnostics = collector.runtime_diagnostics()
assert set(diagnostics) == {"python", "playwright", "yt_dlp", "ffmpeg", "browser"}

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    db_path = root / "desktop.sqlite3"
    config_path = root / "config.json"
    collector.desktop_db_init(db_path)
    keychain_writes = []
    base_handler = collector.make_desktop_app_handler(
        {},
        db_path,
        setup_config_path=config_path,
        setup_keychain_writer=lambda *args: keychain_writes.append(args),
    )

    class QuietHandler(base_handler):
        def log_message(self, fmt, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(base_url + "/api/setup/status", timeout=2) as response:
            status_payload = json.loads(response.read().decode("utf-8"))
            assert response.status == 200
            assert status_payload["complete"] is False
        request = urllib.request.Request(
            base_url + "/api/setup/save",
            data=b'{"local_only":true}',
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            saved_payload = json.loads(response.read().decode("utf-8"))
            assert response.status == 200
            assert saved_payload["complete"] is True
            assert saved_payload["local_only"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert keychain_writes == []

print("OK")
'''
        env = dict(os.environ)
        env["PYTHONPYCACHEPREFIX"] = "/tmp/chen-dmg-task3-clean-process-pycache"

        result = subprocess.run(
            [sys.executable, "-S", "-c", script],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "OK")

    def test_keychain_writer_uses_stable_security_command_without_output(self):
        collector = load_collector()
        with patch.object(collector.subprocess, "run") as run:
            collector.write_keychain_password(
                collector.FEISHU_KEYCHAIN_SERVICE,
                collector.FEISHU_KEYCHAIN_ACCOUNT,
                "secret-value",
            )

        run.assert_called_once_with(
            [
                "security",
                "add-generic-password",
                "-U",
                "-s",
                collector.FEISHU_KEYCHAIN_SERVICE,
                "-a",
                collector.FEISHU_KEYCHAIN_ACCOUNT,
                "-w",
                "secret-value",
            ],
            check=True,
            stdout=collector.subprocess.DEVNULL,
            stderr=collector.subprocess.PIPE,
            text=True,
        )

    def test_strict_keychain_snapshot_distinguishes_missing_and_empty_value(self):
        collector = load_collector()
        found_empty = types.SimpleNamespace(returncode=0, stdout="\n", stderr="")
        missing = types.SimpleNamespace(returncode=44, stdout="", stderr="not found")

        with patch.object(
            collector.subprocess,
            "run",
            side_effect=[found_empty, missing],
        ):
            empty_snapshot = collector.read_keychain_password_snapshot(
                collector.FEISHU_KEYCHAIN_SERVICE,
                collector.FEISHU_KEYCHAIN_ACCOUNT,
            )
            missing_snapshot = collector.read_keychain_password_snapshot(
                collector.PUBLISHER_KEYCHAIN_SERVICE,
                collector.PUBLISHER_KEYCHAIN_ACCOUNT,
            )

        self.assertTrue(empty_snapshot.found)
        self.assertEqual(empty_snapshot.value, "")
        self.assertFalse(missing_snapshot.found)
        self.assertEqual(missing_snapshot.value, "")

    def test_strict_keychain_snapshot_redacts_unexpected_security_failure(self):
        collector = load_collector()
        failed = types.SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="security leaked super-secret-value",
        )

        with patch.object(collector.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(
                RuntimeError,
                f"^{re.escape(collector.KEYCHAIN_READ_FAILED_ERROR)}$",
            ) as raised:
                collector.read_keychain_password_snapshot(
                    collector.FEISHU_KEYCHAIN_SERVICE,
                    collector.FEISHU_KEYCHAIN_ACCOUNT,
                )

        self.assertNotIn("super-secret-value", str(raised.exception))

    def test_save_setup_default_keychain_read_error_precedes_all_mutations(self):
        collector = load_collector()
        commands = []

        def fail_read(command, **kwargs):
            commands.append(command)
            if command[1] == "find-generic-password":
                return types.SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="security leaked read-secret-value",
                )
            raise AssertionError("setup mutated Keychain after snapshot read failure")

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            collector.subprocess,
            "run",
            side_effect=fail_read,
        ):
            config_path = Path(tmp) / "config.json"
            with self.assertRaisesRegex(
                RuntimeError,
                f"^{re.escape(collector.KEYCHAIN_READ_FAILED_ERROR)}$",
            ) as raised:
                collector.save_setup(
                    {
                        "feishu_app_id": "cli_test",
                        "feishu_app_secret": "new-secret-value",
                        "feishu_app_token": "base-token",
                        "feishu_table_id": "tbl-test",
                    },
                    config_path,
                )

            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0][1], "find-generic-password")
            self.assertFalse(config_path.exists())
            self.assertNotIn("read-secret-value", str(raised.exception))

    def test_save_setup_rollback_restores_found_empty_and_deletes_only_missing(self):
        collector = load_collector()
        feishu_key = (
            collector.FEISHU_KEYCHAIN_SERVICE,
            collector.FEISHU_KEYCHAIN_ACCOUNT,
        )
        publisher_key = (
            collector.PUBLISHER_KEYCHAIN_SERVICE,
            collector.PUBLISHER_KEYCHAIN_ACCOUNT,
        )
        snapshots = {
            feishu_key: collector.KeychainPasswordSnapshot(found=True, value=""),
            publisher_key: collector.KeychainPasswordSnapshot(found=False, value=""),
        }
        keychain = {feishu_key: ""}
        writes = []
        deletes = []

        def writer(service, account, value):
            key = (service, account)
            writes.append((*key, value))
            if key == publisher_key:
                raise OSError("publisher write failed with secret text")
            keychain[key] = value

        def deleter(service, account):
            key = (service, account)
            deletes.append(key)
            keychain.pop(key, None)

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            with self.assertRaisesRegex(RuntimeError, "原有配置已恢复"):
                collector.save_setup(
                    {
                        "feishu_app_id": "new-id",
                        "feishu_app_secret": "new-feishu-secret",
                        "feishu_app_token": "new-token",
                        "feishu_table_id": "new-table",
                        "publisher_worker_url": "https://worker.example.com",
                        "publisher_device_token": "new-publisher-secret",
                    },
                    config_path,
                    keychain_reader=lambda service, account: snapshots[(service, account)],
                    keychain_writer=writer,
                    keychain_deleter=deleter,
                )

            self.assertFalse(config_path.exists())

        self.assertEqual(keychain, {feishu_key: ""})
        self.assertEqual(deletes, [publisher_key])
        self.assertEqual(writes[-1], (*feishu_key, ""))

    def test_keychain_deleter_uses_stable_security_command_without_output(self):
        collector = load_collector()
        completed = types.SimpleNamespace(returncode=0)
        with patch.object(collector.subprocess, "run", return_value=completed) as run:
            collector.delete_keychain_password(
                collector.FEISHU_KEYCHAIN_SERVICE,
                collector.FEISHU_KEYCHAIN_ACCOUNT,
            )

        run.assert_called_once_with(
            [
                "security",
                "delete-generic-password",
                "-s",
                collector.FEISHU_KEYCHAIN_SERVICE,
                "-a",
                collector.FEISHU_KEYCHAIN_ACCOUNT,
            ],
            check=False,
            stdout=collector.subprocess.DEVNULL,
            stderr=collector.subprocess.PIPE,
            text=True,
        )

    def test_save_setup_redacts_keychain_writer_failures(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"

            with self.assertRaises(RuntimeError) as raised:
                collector.save_setup(
                    {
                        "feishu_app_id": "cli_test",
                        "feishu_app_secret": "secret-value",
                        "feishu_app_token": "base-token",
                        "feishu_table_id": "tbl-test",
                    },
                    config_path,
                    keychain_reader=lambda *args: "",
                    keychain_writer=lambda *args: (_ for _ in ()).throw(
                        RuntimeError("security command leaked secret-value")
                    ),
                    keychain_deleter=lambda *args: None,
                )

            self.assertNotIn("secret-value", str(raised.exception))
            self.assertFalse(config_path.exists())

    def test_setup_api_handles_status_save_malformed_json_and_local_skip(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "desktop.sqlite3"
            config_path = Path(tmp) / "config.json"
            collector.desktop_db_init(db_path)
            writes = []
            handler = collector.make_desktop_app_handler(
                {},
                db_path,
                setup_config_path=config_path,
                setup_keychain_writer=lambda *args: writes.append(args),
            )
            with desktop_http_server(handler) as base_url:
                status, initial = request_json(base_url + "/api/setup/status")
                self.assertEqual(status, 200)
                self.assertFalse(initial["complete"])

                for body in (b'{"local_only":', b"\xff", b"null", b"[]", b'"skip"'):
                    status, invalid = request_bytes(
                        base_url + "/api/setup/save",
                        method="POST",
                        body=body,
                    )
                    self.assertEqual(status, 400)
                    self.assertEqual(invalid, {"error": "请求内容必须是 JSON 对象"})

                for content_length in ("not-an-integer", "-1", None, "999999999"):
                    status, invalid = request_raw_json(
                        base_url + "/api/setup/save",
                        content_length,
                        body=b"{}",
                    )
                    self.assertEqual(status, 400)
                    self.assertEqual(invalid, {"error": "请求内容必须是 JSON 对象"})

                status, skipped = request_json(
                    base_url + "/api/setup/save",
                    method="POST",
                    payload={"local_only": True},
                )
                self.assertEqual(status, 200)
                self.assertTrue(skipped["complete"])
                self.assertTrue(skipped["local_only"])
                self.assertNotIn("secret", json.dumps(skipped).lower())

            self.assertEqual(writes, [])

    def test_setup_api_uses_single_load_and_updates_runtime_before_success(self):
        collector = load_collector()
        keychain = {}
        runtime_cfg = {"runtime_marker": "before"}
        load_calls = 0
        original_load_config = collector.load_config

        def load_once(*args, **kwargs):
            nonlocal load_calls
            load_calls += 1
            if load_calls > 1:
                raise OSError("unexpected second config reload")
            return original_load_config(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            collector,
            "load_config",
            side_effect=load_once,
        ):
            db_path = Path(tmp) / "desktop.sqlite3"
            config_path = Path(tmp) / "config.json"
            collector.desktop_db_init(db_path)
            handler = collector.make_desktop_app_handler(
                runtime_cfg,
                db_path,
                setup_config_path=config_path,
                setup_keychain_reader=lambda service, account: keychain.get(
                    (service, account), ""
                ),
                setup_keychain_writer=lambda service, account, value: keychain.__setitem__(
                    (service, account), value
                ),
                setup_keychain_deleter=lambda service, account: keychain.pop(
                    (service, account), None
                ),
            )
            with desktop_http_server(handler) as base_url:
                status, saved = request_json(
                    base_url + "/api/setup/save",
                    method="POST",
                    payload={
                        "feishu_app_id": "new-id",
                        "feishu_app_secret": "new-secret",
                        "feishu_app_token": "new-token",
                        "feishu_table_id": "new-table",
                    },
                )

        self.assertEqual(status, 200)
        self.assertTrue(saved["complete"])
        self.assertEqual(load_calls, 1)
        self.assertEqual(runtime_cfg["feishu"]["app_id"], "new-id")
        self.assertNotIn("runtime_marker", runtime_cfg)

    def test_save_setup_runtime_update_failure_rolls_back_memory_files_and_keychain(self):
        collector = load_collector()
        runtime_cfg = {"runtime_marker": {"state": "before"}}
        keychain = {}

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            collector,
            "setup_status",
            side_effect=RuntimeError("status decision failed"),
        ):
            config_path = Path(tmp) / "config.json"
            with self.assertRaisesRegex(RuntimeError, "原有配置已恢复"):
                collector.save_setup(
                    {
                        "feishu_app_id": "new-id",
                        "feishu_app_secret": "new-secret",
                        "feishu_app_token": "new-token",
                        "feishu_table_id": "new-table",
                    },
                    config_path,
                    keychain_reader=lambda service, account: keychain.get(
                        (service, account), ""
                    ),
                    keychain_writer=lambda service, account, value: keychain.__setitem__(
                        (service, account), value
                    ),
                    keychain_deleter=lambda service, account: keychain.pop(
                        (service, account), None
                    ),
                    runtime_config_updater=lambda loaded: collector._runtime_config_update(
                        runtime_cfg,
                        loaded,
                    ),
                )

            self.assertFalse(config_path.exists())

        self.assertEqual(runtime_cfg, {"runtime_marker": {"state": "before"}})
        self.assertEqual(keychain, {})

    def test_setup_api_serializes_rollback_before_concurrent_success(self):
        collector = load_collector()
        runtime_cfg = {}
        keychain = {}
        keychain_lock = threading.Lock()
        transaction_owner = {}
        a_waiting_in_reload = threading.Event()
        release_a_reload = threading.Event()
        responses = []
        responses_lock = threading.Lock()

        def reader(service, account):
            owner = transaction_owner.get(threading.get_ident())
            if owner == "A" and not a_waiting_in_reload.is_set():
                a_waiting_in_reload.set()
                if not release_a_reload.wait(timeout=3):
                    raise RuntimeError("timed out waiting to release transaction A")
                raise OSError("transaction A reload failed")
            with keychain_lock:
                return keychain.get((service, account), "")

        def writer(service, account, value):
            owner = "A" if value.startswith("a-") else "B"
            transaction_owner[threading.get_ident()] = owner
            with keychain_lock:
                keychain[(service, account)] = value

        def deleter(service, account):
            with keychain_lock:
                keychain.pop((service, account), None)

        def submit(name, base_url, payload):
            status, body = request_json(
                base_url + "/api/setup/save",
                method="POST",
                payload=payload,
            )
            with responses_lock:
                responses.append((name, status, body))

        payload_a = {
            "feishu_app_id": "a-id",
            "feishu_app_secret": "a-feishu-secret",
            "feishu_app_token": "a-token",
            "feishu_table_id": "a-table",
            "publisher_worker_url": "https://a-worker.example.com",
            "publisher_device_token": "a-publisher-secret",
        }
        payload_b = {
            "feishu_app_id": "b-id",
            "feishu_app_secret": "b-feishu-secret",
            "feishu_app_token": "b-token",
            "feishu_table_id": "b-table",
            "publisher_worker_url": "https://b-worker.example.com",
            "publisher_device_token": "b-publisher-secret",
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "desktop.sqlite3"
            config_path = root / "config.json"
            collector.desktop_db_init(db_path)
            handler = collector.make_desktop_app_handler(
                runtime_cfg,
                db_path,
                setup_config_path=config_path,
                setup_keychain_reader=reader,
                setup_keychain_writer=writer,
                setup_keychain_deleter=deleter,
            )
            with desktop_http_server(handler) as base_url:
                thread_a = threading.Thread(
                    target=submit,
                    args=("A", base_url, payload_a),
                    daemon=True,
                )
                thread_a.start()
                self.assertTrue(a_waiting_in_reload.wait(timeout=2))

                thread_b = threading.Thread(
                    target=submit,
                    args=("B", base_url, payload_b),
                    daemon=True,
                )
                thread_b.start()
                thread_b.join(timeout=0.35)
                release_a_reload.set()
                thread_a.join(timeout=3)
                thread_b.join(timeout=3)

                self.assertFalse(thread_a.is_alive())
                self.assertFalse(thread_b.is_alive())

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            publisher_saved = json.loads(
                (root / "max_daily_cloud" / "publisher" / "config.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(
            [(name, status) for name, status, _body in responses],
            [("A", 500), ("B", 200)],
        )
        self.assertTrue(responses[-1][2]["complete"])
        self.assertEqual(saved["feishu"]["app_id"], "b-id")
        self.assertEqual(publisher_saved["cloud_api_base"], "https://b-worker.example.com")
        self.assertEqual(runtime_cfg["feishu"]["app_id"], "b-id")
        self.assertEqual(
            keychain,
            {
                (
                    collector.FEISHU_KEYCHAIN_SERVICE,
                    collector.FEISHU_KEYCHAIN_ACCOUNT,
                ): "b-feishu-secret",
                (
                    collector.PUBLISHER_KEYCHAIN_SERVICE,
                    collector.PUBLISHER_KEYCHAIN_ACCOUNT,
                ): "b-publisher-secret",
            },
        )

    def test_setup_api_rejects_non_loopback_clients(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "desktop.sqlite3"
            config_path = Path(tmp) / "config.json"
            collector.desktop_db_init(db_path)
            writes = []
            handler = collector.make_desktop_app_handler(
                {},
                db_path,
                setup_config_path=config_path,
                setup_keychain_writer=lambda *args: writes.append(args),
            )
            remote_handler = handler_with_client_address(handler, ("192.0.2.42", 53210))
            with desktop_http_server(remote_handler) as base_url:
                status, blocked = request_json(base_url + "/api/setup/status")
                self.assertEqual(status, 403)
                self.assertEqual(blocked, {"error": "首次设置接口只允许本机访问"})

                status, blocked, response_headers = request_setup_with_headers(
                    base_url + "/api/setup/save",
                    {
                        "Content-Type": "text/plain",
                        "Host": "rebind.attacker.example",
                        "Origin": "https://attacker.example",
                        "Sec-Fetch-Site": "cross-site",
                    },
                )
                self.assertEqual(status, 403)
                self.assertEqual(blocked, {"error": "首次设置接口只允许本机访问"})
                self.assertNotIn("access-control-allow-origin", response_headers)

            self.assertEqual(writes, [])
            self.assertFalse(config_path.exists())

    def test_setup_api_rejects_attacker_origin_and_text_plain_before_mutation(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "desktop.sqlite3"
            config_path = Path(tmp) / "config.json"
            collector.desktop_db_init(db_path)
            writes = []
            handler = collector.make_desktop_app_handler(
                {},
                db_path,
                setup_config_path=config_path,
                setup_keychain_writer=lambda *args: writes.append(args),
            )
            with desktop_http_server(handler) as base_url:
                status, blocked, headers = request_setup_with_headers(
                    base_url + "/api/setup/save",
                    {
                        "Content-Type": "text/plain",
                        "Origin": "https://attacker.example",
                        "Sec-Fetch-Site": "cross-site",
                    },
                )

            self.assertEqual(status, 415)
            self.assertEqual(blocked, {"error": "首次设置只接受同源 JSON 请求"})
            self.assertNotIn("access-control-allow-origin", headers)
            self.assertEqual(writes, [])
            self.assertFalse(config_path.exists())

    def test_setup_api_rejects_foreign_host_null_origin_and_cross_site_fetch(self):
        collector = load_collector()
        attacks = (
            {
                "Content-Type": "application/json",
                "Host": "rebind.attacker.example",
            },
            {
                "Content-Type": "application/json",
                "Origin": "null",
            },
            {
                "Content-Type": "application/json",
                "Origin": "{base_url}",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        for attack in attacks:
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as tmp:
                db_path = Path(tmp) / "desktop.sqlite3"
                config_path = Path(tmp) / "config.json"
                collector.desktop_db_init(db_path)
                writes = []
                handler = collector.make_desktop_app_handler(
                    {},
                    db_path,
                    setup_config_path=config_path,
                    setup_keychain_writer=lambda *args: writes.append(args),
                )
                with desktop_http_server(handler) as base_url:
                    headers = {
                        name: value.format(base_url=base_url)
                        for name, value in attack.items()
                    }
                    status, blocked, response_headers = request_setup_with_headers(
                        base_url + "/api/setup/save",
                        headers,
                    )

                self.assertEqual(status, 403)
                self.assertEqual(blocked, {"error": "首次设置请求来源不受信任"})
                self.assertNotIn("access-control-allow-origin", response_headers)
                self.assertEqual(writes, [])
                self.assertFalse(config_path.exists())

    def test_setup_api_accepts_same_origin_and_trusted_non_browser_json(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "desktop.sqlite3"
            config_path = Path(tmp) / "config.json"
            collector.desktop_db_init(db_path)
            handler = collector.make_desktop_app_handler(
                {},
                db_path,
                setup_config_path=config_path,
                setup_keychain_writer=lambda *args: self.fail("unexpected Keychain write"),
            )
            with desktop_http_server(handler) as base_url:
                status, same_origin, same_origin_headers = request_setup_with_headers(
                    base_url + "/api/setup/save",
                    {
                        "Content-Type": "application/json; charset=utf-8",
                        "Origin": base_url,
                        "Sec-Fetch-Site": "same-origin",
                    },
                )
                trusted_status, trusted, trusted_headers = request_setup_with_headers(
                    base_url + "/api/setup/save",
                    {"Content-Type": "application/json"},
                )

            self.assertEqual(status, 200)
            self.assertTrue(same_origin["complete"])
            self.assertEqual(trusted_status, 200)
            self.assertTrue(trusted["complete"])
            self.assertNotIn("access-control-allow-origin", same_origin_headers)
            self.assertNotIn("access-control-allow-origin", trusted_headers)

    def test_setup_api_returns_fixed_redacted_error_when_rollback_fails(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "desktop.sqlite3"
            config_path = Path(tmp) / "config.json"
            collector.desktop_db_init(db_path)
            keychain = {}
            write_calls = 0

            def reader(service, account):
                return keychain.get((service, account), "")

            def writer(service, account, value):
                nonlocal write_calls
                write_calls += 1
                if write_calls == 2:
                    raise OSError("forward failure leaked api-publisher-secret")
                keychain[(service, account)] = value

            def deleter(service, account):
                if service == collector.PUBLISHER_KEYCHAIN_SERVICE:
                    raise OSError("rollback failure leaked api-publisher-secret")
                keychain.pop((service, account), None)

            handler = collector.make_desktop_app_handler(
                {},
                db_path,
                setup_config_path=config_path,
                setup_keychain_reader=reader,
                setup_keychain_writer=writer,
                setup_keychain_deleter=deleter,
            )
            with desktop_http_server(handler) as base_url:
                status, failed, headers = request_setup_with_headers(
                    base_url + "/api/setup/save",
                    {
                        "Content-Type": "application/json",
                        "Origin": base_url,
                        "Sec-Fetch-Site": "same-origin",
                    },
                    body=json.dumps(
                        {
                            "feishu_app_id": "new-id",
                            "feishu_app_secret": "api-feishu-secret",
                            "feishu_app_token": "new-token",
                            "feishu_table_id": "new-table",
                            "publisher_worker_url": "https://worker.example.com",
                            "publisher_device_token": "api-publisher-secret",
                        }
                    ).encode("utf-8"),
                )

            self.assertEqual(status, 500)
            self.assertEqual(failed, {"error": collector.SETUP_ROLLBACK_FAILED_ERROR})
            serialized = json.dumps(failed, ensure_ascii=False)
            self.assertNotIn("api-feishu-secret", serialized)
            self.assertNotIn("api-publisher-secret", serialized)
            self.assertNotIn("access-control-allow-origin", headers)
            self.assertEqual(keychain, {})
            self.assertFalse(config_path.exists())

    def test_save_setup_rolls_back_second_keychain_write_failure(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            publisher_path = root / "max_daily_cloud" / "publisher" / "config.json"
            publisher_path.parent.mkdir(parents=True)
            main_before = b'{\n  "existing": "main-before"\n}\n'
            publisher_before = b'{"existing":"publisher-before"}\n'
            config_path.write_bytes(main_before)
            publisher_path.write_bytes(publisher_before)
            os.chmod(config_path, 0o640)
            os.chmod(publisher_path, 0o600)
            keychain = {
                (collector.FEISHU_KEYCHAIN_SERVICE, collector.FEISHU_KEYCHAIN_ACCOUNT): "old-feishu",
                (
                    collector.PUBLISHER_KEYCHAIN_SERVICE,
                    collector.PUBLISHER_KEYCHAIN_ACCOUNT,
                ): "old-publisher",
            }
            write_calls = 0

            def reader(service, account):
                return keychain.get((service, account), "")

            def writer(service, account, value):
                nonlocal write_calls
                write_calls += 1
                if write_calls == 2:
                    raise OSError("second write failed with new-publisher-secret")
                keychain[(service, account)] = value

            def deleter(service, account):
                keychain.pop((service, account), None)

            with self.assertRaisesRegex(RuntimeError, "原有配置已恢复") as raised:
                collector.save_setup(
                    {
                        "feishu_app_id": "new-id",
                        "feishu_app_secret": "new-feishu-secret",
                        "feishu_app_token": "new-token",
                        "feishu_table_id": "new-table",
                        "publisher_worker_url": "https://worker.example.com",
                        "publisher_device_token": "new-publisher-secret",
                    },
                    config_path,
                    keychain_reader=reader,
                    keychain_writer=writer,
                    keychain_deleter=deleter,
                )

            self.assertNotIn("new-feishu-secret", str(raised.exception))
            self.assertNotIn("new-publisher-secret", str(raised.exception))
            self.assertEqual(
                keychain,
                {
                    (
                        collector.FEISHU_KEYCHAIN_SERVICE,
                        collector.FEISHU_KEYCHAIN_ACCOUNT,
                    ): "old-feishu",
                    (
                        collector.PUBLISHER_KEYCHAIN_SERVICE,
                        collector.PUBLISHER_KEYCHAIN_ACCOUNT,
                    ): "old-publisher",
                },
            )
            self.assertEqual(config_path.read_bytes(), main_before)
            self.assertEqual(publisher_path.read_bytes(), publisher_before)
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(publisher_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(root.rglob("*.tmp")), [])

    def test_save_setup_rolls_back_publisher_config_replace_failure(self):
        self._assert_setup_file_replace_failure_rolls_back("publisher")

    def test_save_setup_rolls_back_main_config_replace_failure(self):
        self._assert_setup_file_replace_failure_rolls_back("main")

    def _assert_setup_file_replace_failure_rolls_back(self, failure_target):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            publisher_path = root / "max_daily_cloud" / "publisher" / "config.json"
            publisher_path.parent.mkdir(parents=True)
            main_before = b'{"existing":"main-before"}\n'
            publisher_before = b'{\n  "existing": "publisher-before"\n}\n'
            config_path.write_bytes(main_before)
            publisher_path.write_bytes(publisher_before)
            os.chmod(config_path, 0o640)
            os.chmod(publisher_path, 0o600)
            keychain_before = {
                (collector.FEISHU_KEYCHAIN_SERVICE, collector.FEISHU_KEYCHAIN_ACCOUNT): "old-feishu",
                (
                    collector.PUBLISHER_KEYCHAIN_SERVICE,
                    collector.PUBLISHER_KEYCHAIN_ACCOUNT,
                ): "old-publisher",
            }
            keychain = dict(keychain_before)

            def reader(service, account):
                return keychain.get((service, account), "")

            def writer(service, account, value):
                keychain[(service, account)] = value

            def deleter(service, account):
                keychain.pop((service, account), None)

            real_replace = collector.os.replace
            failed = False
            selected_path = publisher_path if failure_target == "publisher" else config_path

            def fail_selected_replace(source, destination):
                nonlocal failed
                if not failed and Path(destination) == selected_path:
                    failed = True
                    raise OSError(f"{failure_target} replace failed with secret text")
                return real_replace(source, destination)

            with patch.object(collector.os, "replace", side_effect=fail_selected_replace):
                with self.assertRaisesRegex(RuntimeError, "原有配置已恢复") as raised:
                    collector.save_setup(
                        {
                            "feishu_app_id": "new-id",
                            "feishu_app_secret": "new-feishu-secret",
                            "feishu_app_token": "new-token",
                            "feishu_table_id": "new-table",
                            "publisher_worker_url": "https://worker.example.com",
                            "publisher_device_token": "new-publisher-secret",
                        },
                        config_path,
                        keychain_reader=reader,
                        keychain_writer=writer,
                        keychain_deleter=deleter,
                    )

            self.assertNotIn("new-feishu-secret", str(raised.exception))
            self.assertNotIn("new-publisher-secret", str(raised.exception))
            self.assertEqual(keychain, keychain_before)
            self.assertEqual(config_path.read_bytes(), main_before)
            self.assertEqual(publisher_path.read_bytes(), publisher_before)
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(publisher_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(root.rglob("*.tmp")), [])

    def test_save_setup_reports_redacted_rollback_failure_without_success(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            main_before = b'{"existing":"main-before"}\n'
            config_path.write_bytes(main_before)
            keychain = {
                (collector.FEISHU_KEYCHAIN_SERVICE, collector.FEISHU_KEYCHAIN_ACCOUNT): "old-feishu",
            }
            write_calls = 0

            def reader(service, account):
                return keychain.get((service, account), "")

            def writer(service, account, value):
                nonlocal write_calls
                write_calls += 1
                if write_calls == 2:
                    raise OSError("forward failure leaked new-publisher-secret")
                keychain[(service, account)] = value

            def deleter(service, account):
                if service == collector.PUBLISHER_KEYCHAIN_SERVICE:
                    raise OSError("rollback failure leaked new-publisher-secret")
                keychain.pop((service, account), None)

            with self.assertRaisesRegex(RuntimeError, "自动恢复未完成") as raised:
                collector.save_setup(
                    {
                        "feishu_app_id": "new-id",
                        "feishu_app_secret": "new-feishu-secret",
                        "feishu_app_token": "new-token",
                        "feishu_table_id": "new-table",
                        "publisher_worker_url": "https://worker.example.com",
                        "publisher_device_token": "new-publisher-secret",
                    },
                    config_path,
                    keychain_reader=reader,
                    keychain_writer=writer,
                    keychain_deleter=deleter,
                )

            message = str(raised.exception)
            self.assertNotIn("new-feishu-secret", message)
            self.assertNotIn("new-publisher-secret", message)
            self.assertNotIn("success", message.lower())
            self.assertEqual(
                keychain,
                {
                    (
                        collector.FEISHU_KEYCHAIN_SERVICE,
                        collector.FEISHU_KEYCHAIN_ACCOUNT,
                    ): "old-feishu",
                },
            )
            self.assertEqual(config_path.read_bytes(), main_before)
            self.assertFalse(
                (root / "max_daily_cloud" / "publisher" / "config.json").exists()
            )
            self.assertEqual(list(root.rglob("*.tmp")), [])

    def test_first_run_html_has_ordered_transitions_and_same_origin_fetch(self):
        collector = load_collector()
        html = collector.DESKTOP_APP_HTML

        for token in (
            'id="setupPage"',
            '<main class="home-page" id="homePage" hidden>',
            'type="password"',
            'id="setupProgress"',
            'id="setupLocalOnly"',
            "验证并继续",
            "暂时跳过飞书与云端配置",
            "initializeSetup",
            "/api/setup/status",
            "/api/setup/save",
            "openSettings()",
        ):
            self.assertIn(token, html)
        api_script = html.split("async function api(path,opts={})", 1)[1].split(
            "function renderSetupProgress", 1
        )[0]
        self.assertIn("new URL(path,window.location.href)", api_script)
        self.assertIn("url.origin!==window.location.origin", api_script)
        self.assertIn("mode:'same-origin'", api_script)
        self.assertIn("credentials:'same-origin'", api_script)
        self.assertNotIn("'Origin'", api_script)

        save_script = html.split("async function saveFirstRunSetup(localOnly)", 1)[1].split(
            "function enterWorkbench", 1
        )[0]
        self.assertLess(save_script.index("submit.disabled=true"), save_script.index("await api("))
        self.assertLess(
            save_script.index("localOnlyButton.disabled=true"),
            save_script.index("await api("),
        )
        self.assertLess(
            save_script.index("if(!status.complete)throw"),
            save_script.index("setTimeout(enterWorkbench,250)"),
        )
        self.assertIn(
            "if(!saved){submit.disabled=false;localOnlyButton.disabled=false}",
            save_script,
        )

        initialize_script = html.split("async function initializeSetup()", 1)[1].split(
            "async function saveFirstRunSetup", 1
        )[0]
        self.assertIn("qs('#setupPage').hidden=false", initialize_script)
        self.assertIn("qs('#homePage').hidden=true", initialize_script)
        enter_script = html.split("function enterWorkbench()", 1)[1].split(
            "async function initializeSetup", 1
        )[0]
        self.assertIn("qs('#setupPage').hidden=true", enter_script)
        self.assertIn("qs('#homePage').hidden=false", enter_script)

    def test_collector_import_rejects_preloaded_publisher_alias_identity(self):
        alias_name = "publisher.max_daily_publisher"
        canonical_prefix = "max_daily_cloud"
        alias_prefix = "publisher"
        saved_modules = {
            name: module
            for name, module in sys.modules.items()
            if name == canonical_prefix
            or name.startswith(f"{canonical_prefix}.")
            or name == alias_prefix
            or name.startswith(f"{alias_prefix}.")
        }
        original_sys_path = sys.path[:]
        try:
            for name in saved_modules:
                sys.modules.pop(name)
            sys.path.insert(0, str(ROOT / "max_daily_cloud"))
            alias_module = importlib.import_module(alias_name)

            self.assertEqual(
                Path(alias_module.__file__).resolve(),
                ROOT / "max_daily_cloud" / "publisher" / "max_daily_publisher.py",
            )
            with self.assertRaisesRegex(
                ImportError,
                r"publisher.*alias.*max_daily_cloud\.publisher",
            ):
                load_collector()

            self.assertIs(sys.modules[alias_name], alias_module)
            self.assertNotIn("max_daily_cloud.publisher.max_daily_publisher", sys.modules)
            self.assertNotIn("max_daily_cloud.publisher.local_publish_jobs", sys.modules)
        finally:
            sys.path[:] = original_sys_path
            for name in list(sys.modules):
                if (
                    name == canonical_prefix
                    or name.startswith(f"{canonical_prefix}.")
                    or name == alias_prefix
                    or name.startswith(f"{alias_prefix}.")
                ):
                    sys.modules.pop(name)
            sys.modules.update(saved_modules)

    def test_collector_import_rejects_foreign_publisher_dependency_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            module_name = "max_daily_cloud.publisher.max_daily_publisher"
            foreign_path = Path(tmp) / "max_daily_cloud" / "publisher" / "max_daily_publisher.py"
            foreign_path.parent.mkdir(parents=True)
            foreign_path.write_text("# foreign publisher module\n", encoding="utf-8")
            foreign_module = types.ModuleType(module_name)
            foreign_module.__file__ = str(foreign_path)
            source_modules = {
                name: module
                for name, module in sys.modules.items()
                if name == "max_daily_cloud" or name.startswith("max_daily_cloud.")
            }
            try:
                for name in source_modules:
                    sys.modules.pop(name)
                sys.modules[module_name] = foreign_module

                with self.assertRaisesRegex(
                    ImportError,
                    r"Publisher namespace collision.*max_daily_cloud\.publisher\.max_daily_publisher",
                ):
                    load_collector()

                self.assertIs(sys.modules[module_name], foreign_module)
                self.assertNotIn("max_daily_cloud.publisher.local_publish_jobs", sys.modules)
            finally:
                for name in list(sys.modules):
                    if name == "max_daily_cloud" or name.startswith("max_daily_cloud."):
                        sys.modules.pop(name)
                sys.modules.update(source_modules)

    def test_collector_import_uses_data_root_and_source_publisher_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp).resolve()
            source_modules = {
                name: module
                for name, module in sys.modules.items()
                if name == "max_daily_cloud" or name.startswith("max_daily_cloud.")
            }
            original_sys_path = sys.path[:]
            original_cwd = Path.cwd()
            try:
                for name in source_modules:
                    sys.modules.pop(name)
                sys.path[:] = [
                    entry for entry in sys.path if Path(entry or ".").resolve() != ROOT
                ]
                os.chdir(data_root)
                with patch.dict(
                    os.environ,
                    {
                        "CHEN_COLLECTOR_DATA_ROOT": str(data_root),
                        "PUBLISHER_DEVICE_TOKEN": "publisher-test-token",
                    },
                ):
                    collector = load_collector()
                    self.assertEqual(collector.DATA_ROOT, data_root)
                    self.assertEqual(collector.HERE, data_root)
                    self.assertEqual(collector.CONFIG_PATH, data_root / "config.json")
                    self.assertEqual(collector.ENV_PATH, data_root / ".env")
                    self.assertEqual(collector.TOKEN_CACHE, data_root / ".feishu_token_cache.json")
                    self.assertEqual(collector.DESKTOP_DB_PATH, data_root / "desktop_collector.sqlite3")
                    self.assertEqual(
                        collector.DEPLOYMENT_MANIFEST_PATH,
                        data_root / "deployment_manifest.json",
                    )
                    self.assertEqual(collector.CODE_ROOT, ROOT)
                    self.assertEqual(collector.publisher_code_root(), ROOT / "max_daily_cloud" / "publisher")
                    self.assertEqual(
                        Path(sys.modules[collector.PublishJobManager.__module__].__file__).resolve(),
                        ROOT / "max_daily_cloud" / "publisher" / "local_publish_jobs.py",
                    )

                    captured = {}

                    class FakePublishJobManager:
                        def __init__(self, config_path, latest_path, **kwargs):
                            captured["config_path"] = config_path
                            captured["latest_path"] = latest_path

                    class FakeServer:
                        def serve_forever(self):
                            return None

                        def server_close(self):
                            return None

                    with patch.object(collector, "load_config", return_value={}), patch.object(
                        collector, "desktop_db_init"
                    ), patch.object(collector, "desktop_start_queue_worker"), patch.object(
                        collector, "desktop_start_mobile_inbox_worker"
                    ), patch.object(
                        collector, "PublishJobManager", FakePublishJobManager
                    ), patch.object(
                        collector, "make_desktop_app_handler", return_value=object()
                    ), patch.object(
                        collector.http.server, "ThreadingHTTPServer", return_value=FakeServer()
                    ):
                        collector.cmd_desktop_app(
                            Namespace(db=None, host="127.0.0.1", port=51216, open=False)
                        )

                    publisher_config_path = data_root / "max_daily_cloud" / "publisher" / "config.json"
                    publisher_config_path.parent.mkdir(parents=True)
                    publisher_config_path.write_text(
                        json.dumps({"cloud_api_base": "https://worker.example.com"}),
                        encoding="utf-8",
                    )
                    publisher_config_module = sys.modules[
                        "max_daily_cloud.publisher.max_daily_publisher"
                    ]
                    config = publisher_config_module.PublisherConfig.load(publisher_config_path)
                    self.assertEqual(captured["config_path"], publisher_config_path)
                    self.assertEqual(
                        captured["latest_path"],
                        data_root / "max_daily_cloud" / ".publisher-state" / "latest-report.json",
                    )
                    self.assertEqual(
                        config.state_path,
                        data_root / ".publisher-state" / "state.json",
                    )
            finally:
                os.chdir(original_cwd)
                sys.path[:] = original_sys_path
                for name in list(sys.modules):
                    if name == "max_daily_cloud" or name.startswith("max_daily_cloud."):
                        sys.modules.pop(name)
                sys.modules.update(source_modules)

    def test_collector_data_root_uses_explicit_environment(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            env = {"CHEN_COLLECTOR_DATA_ROOT": tmp}
            self.assertEqual(collector.collector_data_root(env), Path(tmp).resolve())

    def test_collector_code_and_data_roots_are_separate(self):
        collector = load_collector()
        self.assertEqual(collector.collector_code_root(), ROOT)
        self.assertEqual(collector.publisher_code_root(), ROOT / "max_daily_cloud" / "publisher")

    def test_video_download_settings_default_to_desktop_and_persist_independently(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            default = str(Path.home() / "Desktop" / "CHEN内容采集助手视频")
            state = collector.desktop_video_download_settings(db)
            self.assertEqual(state["single_directory"], default)
            self.assertEqual(state["batch_directory"], default)

            single = Path(tmp) / "single"
            batch = Path(tmp) / "batch"
            collector.desktop_set_video_download_directory(db, "single", single)
            collector.desktop_set_video_download_directory(db, "batch", batch)

            state = collector.desktop_video_download_settings(db)
            self.assertEqual(state["single_directory"], str(single.resolve()))
            self.assertEqual(state["batch_directory"], str(batch.resolve()))

    def test_video_download_directory_rejects_a_file(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            target = Path(tmp) / "not-a-directory"
            target.write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "不是文件夹"):
                collector.desktop_set_video_download_directory(db, "single", target)

    def test_download_batch_snapshots_directory_and_deduplicates_items(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            table = collector.desktop_create_table(db, "素材")
            item = collector.desktop_save_item(
                db,
                table["id"],
                {"platform": "抖音", "source_url": "https://www.douyin.com/video/1"},
            )
            first_dir = Path(tmp) / "batch-a"
            collector.desktop_set_video_download_directory(db, "batch", first_dir)
            batch = collector.desktop_create_download_batch(db, [item["id"], item["id"]], "batch")
            self.assertEqual(batch["directory"], str(first_dir.resolve()))
            self.assertEqual(len(batch["tasks"]), 1)

            collector.desktop_set_video_download_directory(db, "batch", Path(tmp) / "batch-b")
            payload = collector.desktop_download_queue_payload(db)
            self.assertEqual(payload["batches"][0]["directory"], str(first_dir.resolve()))
            self.assertEqual(payload["tasks"][0]["status"], "queued")

    def test_direct_video_download_reports_progress_and_uses_part_file(self):
        collector = load_collector()

        class Headers:
            def get_content_type(self):
                return "video/mp4"

            def get(self, key, default=None):
                return "6" if key.lower() == "content-length" else default

        class Response:
            headers = Headers()

            def __init__(self):
                self.data = b"abcdef"

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size):
                chunk, self.data = self.data[:3], self.data[3:]
                return chunk

        original = collector.urllib.request.urlopen
        events = []
        try:
            collector.urllib.request.urlopen = lambda request, timeout=180: Response()
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "video.mp4"
                collector.download_media_url_to_file(
                    "https://cdn.example/video.mp4",
                    {},
                    "抖音",
                    target,
                    progress_callback=events.append,
                )
                self.assertEqual(target.read_bytes(), b"abcdef")
                self.assertFalse(Path(str(target) + ".part").exists())
                self.assertEqual(events[-1]["downloaded_bytes"], 6)
                self.assertEqual(events[-1]["progress"], 100.0)
        finally:
            collector.urllib.request.urlopen = original

    def test_desktop_html_contains_settings_progress_and_batch_queue(self):
        collector = load_collector()
        html = collector.DESKTOP_APP_HTML + collector.DESKTOP_DAILY_HTML
        for token in (
            "软件设置",
            "单条视频保存位置",
            "批量队列保存位置",
            "批量下载",
            "下载队列",
            "downloadSelectedVideos",
            "pollDownloadQueue",
            "下载完成",
        ):
            self.assertIn(token, html)

    def test_download_ui_uses_inline_progress_and_dedicated_table_page(self):
        collector = load_collector()
        html = collector.DESKTOP_APP_HTML
        for token in (
            'id="downloadQueuePage"',
            "downloadActionHtml",
            "download-progress-track",
            "视频标题",
            "文件大小",
            "完成时间",
            "打开视频",
            "打开文件夹",
            "返回主页",
            "installDownloadBatchButtons",
        ):
            self.assertIn(token, html)
        save_fn = html.split("async function saveVideoFile", 1)[1].split("function downloadSelectedVideos", 1)[0]
        self.assertNotIn("openDownloadQueue()", save_fn)
        self.assertIn("pollDownloadQueue(false)", save_fn)

    def test_canonical_build_contains_browser_cloud_and_download_features(self):
        collector = load_collector()

        self.assertTrue(hasattr(collector, "BROWSER_NOT_READY_STATUS"))
        self.assertTrue(hasattr(collector, "PublishJobManager"))
        self.assertTrue(hasattr(collector, "desktop_create_download_batch"))
        self.assertIn('id="downloadQueuePage"', collector.DESKTOP_APP_HTML)
        self.assertIn("function downloadActionHtml", collector.DESKTOP_APP_HTML)
        self.assertNotIn('id="downloadQueueModal"', collector.DESKTOP_APP_HTML)

    def test_desktop_cloud_publish_endpoints(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db_path)
            fake = FakeCloudPublishJobs()
            with desktop_http_server(
                collector.make_desktop_app_handler({}, db_path, fake)
            ) as base_url:
                status, started = request_json(
                    base_url + "/api/cloud/publish",
                    method="POST",
                    payload={"date": "2026-07-13"},
                )
                self.assertEqual(status, 202)
                self.assertEqual(started["dailyDate"], "2026-07-13")
                self.assertEqual(fake.started, ["2026-07-13"])

                _, progress = request_json(base_url + "/api/cloud/publish/status")
                self.assertEqual(progress["stage"], "uploading")

                _, latest = request_json(base_url + "/api/cloud/latest")
                self.assertEqual(latest["reportUrl"], "https://worker.example.com/r/report-1")

                fake.start_error = collector.InvalidPublishDate("日报日期格式必须为 YYYY-MM-DD")
                status, invalid = request_json(
                    base_url + "/api/cloud/publish",
                    method="POST",
                    payload={"date": "not-a-date"},
                )
                self.assertEqual(status, 400)
                self.assertEqual(invalid["error"], "日报日期格式必须为 YYYY-MM-DD")

                fake.start_error = collector.PublishAlreadyRunning("已有日报正在发布")
                status, running = request_json(
                    base_url + "/api/cloud/publish",
                    method="POST",
                    payload={"date": "2026-07-13"},
                )
                self.assertEqual(status, 409)
                self.assertEqual(running["error"], "已有日报正在发布")

                for body in (b'{"date":', b"\xff", b"null", b"[]", b'"2026-07-13"'):
                    status, invalid_payload = request_bytes(
                        base_url + "/api/cloud/publish",
                        method="POST",
                        body=body,
                    )
                    self.assertEqual(status, 400)
                    self.assertEqual(invalid_payload, {"error": "请求内容必须是 JSON 对象"})

                for content_length in ("not-an-integer", "-1", None, "999999999"):
                    status, invalid_length = request_raw_json(
                        base_url + "/api/cloud/publish",
                        content_length,
                        body=b"{}",
                    )
                    self.assertEqual(status, 400)
                    self.assertEqual(invalid_length, {"error": "请求内容必须是 JSON 对象"})

                fake.start_error = None
                status, started = request_json(
                    base_url + "/api/cloud/publish",
                    method="POST",
                    payload={"date": "2026-07-13"},
                )
                self.assertEqual(status, 202)
                self.assertEqual(started["dailyDate"], "2026-07-13")

            with desktop_http_server(
                collector.make_desktop_app_handler({}, db_path)
            ) as base_url:
                status, unavailable = request_json(
                    base_url + "/api/cloud/publish",
                    method="POST",
                    payload={"date": "2026-07-13"},
                )
                self.assertEqual(status, 503)
                self.assertEqual(unavailable["error"], "云端发布器未配置")

                for body in (b'{"date":', b"\xff", b"null", b"[]", b'"2026-07-13"'):
                    status, unavailable = request_bytes(
                        base_url + "/api/cloud/publish",
                        method="POST",
                        body=body,
                    )
                    self.assertEqual(status, 503)
                    self.assertEqual(unavailable, {"error": "云端发布器未配置"})

                status, unavailable = request_json(base_url + "/api/cloud/publish/status")
                self.assertEqual(status, 503)
                self.assertEqual(unavailable, {"error": "云端发布器未配置"})

                status, unavailable = request_json(base_url + "/api/cloud/latest")
                self.assertEqual(status, 503)
                self.assertEqual(unavailable, {"error": "云端发布器未配置"})

    def test_desktop_cloud_endpoints_reject_non_loopback_clients(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db_path)
            fake = FakeCloudPublishJobs()
            handler = collector.make_desktop_app_handler({}, db_path, fake)
            remote_handler = handler_with_client_address(handler, ("192.168.1.50", 53210))
            with desktop_http_server(remote_handler) as base_url:
                for path in (
                    "/api/cloud/publish/status",
                    "/api/cloud/latest",
                    "/api/cloud/unknown",
                ):
                    status, blocked = request_json(base_url + path)
                    self.assertEqual(status, 403)
                    self.assertEqual(blocked, {"error": "云端发布接口只允许本机访问"})

                status, blocked = request_json(
                    base_url + "/api/cloud/publish",
                    method="POST",
                    payload={"date": "2026-07-13"},
                )
                self.assertEqual(status, 403)
                self.assertEqual(blocked, {"error": "云端发布接口只允许本机访问"})
                self.assertEqual(fake.started, [])

            unavailable_handler = collector.make_desktop_app_handler({}, db_path)
            remote_unavailable_handler = handler_with_client_address(
                unavailable_handler,
                ("203.0.113.10", 53210),
            )
            with desktop_http_server(remote_unavailable_handler) as base_url:
                status, blocked = request_bytes(
                    base_url + "/api/cloud/publish",
                    method="POST",
                    body=b'{"date":',
                )
                self.assertEqual(status, 403)
                self.assertEqual(blocked, {"error": "云端发布接口只允许本机访问"})

                status, blocked = request_json(base_url + "/api/cloud/latest")
                self.assertEqual(status, 403)
                self.assertEqual(blocked, {"error": "云端发布接口只允许本机访问"})

    def test_desktop_cloud_endpoints_accept_loopback_address_variants(self):
        collector = load_collector()
        for client_host in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            with self.subTest(client_host=client_host):
                with tempfile.TemporaryDirectory() as tmp:
                    db_path = Path(tmp) / "desktop.sqlite3"
                    collector.desktop_db_init(db_path)
                    fake = FakeCloudPublishJobs()
                    handler = collector.make_desktop_app_handler({}, db_path, fake)
                    loopback_handler = handler_with_client_address(
                        handler,
                        (client_host, 53210),
                    )
                    with desktop_http_server(loopback_handler) as base_url:
                        status, started = request_json(
                            base_url + "/api/cloud/publish",
                            method="POST",
                            payload={"date": "2026-07-13"},
                        )
                        self.assertEqual(status, 202)
                        self.assertEqual(started["dailyDate"], "2026-07-13")
                        self.assertEqual(fake.started, ["2026-07-13"])

    def test_extract_record_ids_from_nested_event(self):
        collector = load_collector()
        payload = {
            "schema": "2.0",
            "header": {"event_type": "bitable.record.changed"},
            "event": {
                "app_token": "apptoken",
                "table_id": "tbl123",
                "record_id": "recAaBbCcDd123",
                "changes": [{"record_id": "recEeFfGgHh456"}],
            },
        }

        self.assertEqual(
            collector.extract_record_ids(payload),
            ["recAaBbCcDd123", "recEeFfGgHh456"],
        )

    def test_challenge_response_payload(self):
        collector = load_collector()

        self.assertEqual(
            collector.challenge_response({"type": "url_verification", "challenge": "abc123"}),
            {"challenge": "abc123"},
        )

    def test_extract_bitable_action_record_ids_from_sdk_event(self):
        collector = load_collector()

        class Action:
            def __init__(self, record_id):
                self.record_id = record_id

        class EventData:
            action_list = [Action("recSdkRecord123"), Action("recSdkRecord456")]

        class SdkEvent:
            event = EventData()

        self.assertEqual(
            collector.extract_bitable_action_record_ids(SdkEvent()),
            ["recSdkRecord123", "recSdkRecord456"],
        )

    def test_extract_bitable_action_jobs_keeps_table_id(self):
        collector = load_collector()

        class Action:
            def __init__(self, record_id, table_id=""):
                self.record_id = record_id
                self.table_id = table_id

        class EventData:
            table_id = "tblDefault"
            action_list = [Action("recSdkRecord123", "tblA"), Action("recSdkRecord456")]

        class SdkEvent:
            event = EventData()

        self.assertEqual(
            collector.extract_bitable_action_jobs(SdkEvent()),
            [("tblA", "recSdkRecord123"), ("tblDefault", "recSdkRecord456")],
        )

    def test_feishu_table_ids_dedupes_primary_and_extra_tables(self):
        collector = load_collector()
        cfg = {
            "feishu": {
                "table_id": "tblPrimary",
                "table_ids": ["tblPrimary", "tblCopy", "", "tblCopy"],
            }
        }

        self.assertEqual(collector.feishu_table_ids(cfg), ["tblPrimary", "tblCopy"])

    def test_discover_feishu_table_ids_merges_auto_discovered_tables(self):
        collector = load_collector()
        cfg = {
            "feishu": {
                "app_id": "cli_x",
                "app_secret": "secret",
                "app_token": "app",
                "table_id": "tblPrimary",
                "table_ids": ["tblCopy"],
                "auto_discover_tables": True,
            }
        }

        collector.list_tables = lambda cfg: [{"table_id": "tblPrimary"}, {"table_id": "tblNew"}]

        self.assertEqual(collector.discover_feishu_table_ids(cfg), ["tblPrimary", "tblCopy", "tblNew"])

    def test_with_table_id_overrides_table_without_mutating_source(self):
        collector = load_collector()
        cfg = {"feishu": {"table_id": "tblPrimary", "app_token": "app"}, "fields": collector.DEFAULT_FIELDS}

        table_cfg = collector.with_table_id(cfg, "tblCopy")

        self.assertEqual(table_cfg["feishu"]["table_id"], "tblCopy")
        self.assertEqual(cfg["feishu"]["table_id"], "tblPrimary")

    def test_event_worker_count_defaults_and_clamps(self):
        collector = load_collector()

        self.assertEqual(collector.event_worker_count({}), 1)
        self.assertEqual(collector.event_worker_count({"event": {"worker_count": 0}}), 1)
        self.assertEqual(collector.event_worker_count({"event": {"worker_count": 99}}), 8)

    def test_scan_missing_records_continues_after_one_table_times_out(self):
        collector = load_collector()
        cfg = {"feishu": {"table_id": "bad"}, "fields": collector.DEFAULT_FIELDS}
        jobs = queue.Queue()
        pending = set()
        pending_lock = threading.Lock()

        collector.discover_feishu_table_ids = lambda cfg: ["bad", "good"]

        def fake_list_records(table_cfg):
            if table_cfg["feishu"]["table_id"] == "bad":
                raise TimeoutError("first table timed out")
            return [
                {
                    "record_id": "recGoodRecord123",
                    "fields": {"作品链接": "https://www.douyin.com/video/123456789"},
                }
            ]

        collector.list_records = fake_list_records

        collector.scan_missing_records_once(cfg, jobs, pending, pending_lock)

        self.assertEqual(jobs.get_nowait(), ("good", "recGoodRecord123"))

    def test_scanner_heartbeat_marks_started_and_finished(self):
        collector = load_collector()
        state = {}

        collector.mark_scanner_heartbeat(state, "running")
        first_seen = state["last_seen"]

        collector.mark_scanner_heartbeat(state, "finished")

        self.assertEqual(state["status"], "finished")
        self.assertGreaterEqual(state["last_seen"], first_seen)

    def test_scanner_watchdog_exits_when_heartbeat_is_stale(self):
        collector = load_collector()
        state = {"last_seen": 100.0, "status": "running"}

        self.assertTrue(collector.scanner_heartbeat_stale(state, interval=15, now=161.0))
        self.assertFalse(collector.scanner_heartbeat_stale(state, interval=15, now=120.0))

    def test_login_url_for_supported_platforms(self):
        collector = load_collector()

        self.assertEqual(collector.login_url_for_platform("抖音"), "https://www.douyin.com/")
        self.assertEqual(collector.login_url_for_platform("小红书"), "https://www.xiaohongshu.com/")
        self.assertEqual(collector.login_url_for_platform("B站"), "https://www.bilibili.com/")
        self.assertEqual(collector.login_url_for_platform("YouTube"), "https://www.youtube.com/")
        self.assertEqual(collector.login_url_for_platform("Instagram"), "https://www.instagram.com/")
        self.assertEqual(collector.login_url_for_platform("未知"), "")

    def test_detect_platform_for_bilibili_links(self):
        collector = load_collector()

        self.assertEqual(collector.detect_platform("https://www.bilibili.com/video/BV1xx411c7mD/"), "B站")
        self.assertEqual(collector.detect_platform("https://m.bilibili.com/video/av123456"), "B站")
        self.assertEqual(collector.detect_platform("https://b23.tv/abc123"), "B站")

    def test_detect_platform_for_youtube_and_instagram_links(self):
        collector = load_collector()

        self.assertEqual(collector.detect_platform("https://www.youtube.com/watch?v=abc123xyz90"), "YouTube")
        self.assertEqual(collector.detect_platform("https://youtu.be/abc123xyz90"), "YouTube")
        self.assertEqual(collector.detect_platform("https://www.youtube.com/shorts/abc123xyz90"), "YouTube")
        self.assertEqual(collector.detect_platform("https://www.instagram.com/reel/ABCdef123/"), "Instagram")
        self.assertEqual(collector.detect_platform("https://instagram.com/p/ABCdef123/"), "Instagram")

    def test_bilibili_ids_from_url_reads_bv_and_av(self):
        collector = load_collector()

        self.assertEqual(collector.bilibili_bvid_from_url("https://www.bilibili.com/video/BV1xx411c7mD/"), "BV1xx411c7mD")
        self.assertEqual(collector.bilibili_aid_from_url("https://www.bilibili.com/video/av123456"), "123456")

    def test_bilibili_view_to_meta_extracts_core_fields(self):
        collector = load_collector()
        payload = {
            "title": "测试 B站视频",
            "desc": "这是简介，不应该当逐字稿",
            "pic": "//i0.hdslb.com/bfs/archive/cover.jpg",
            "duration": 95,
            "pubdate": 1710000000,
            "stat": {"like": 12, "reply": 3, "share": 4},
        }

        meta = collector.bilibili_view_to_meta("https://www.bilibili.com/video/BVabc", "https://www.bilibili.com/video/BVabc", payload)

        self.assertEqual(meta["platform"], "B站")
        self.assertEqual(meta["content_type"], "video")
        self.assertEqual(meta["title"], "测试 B站视频")
        self.assertEqual(meta["caption"], "")
        self.assertEqual(meta["cover_url"], "https://i0.hdslb.com/bfs/archive/cover.jpg")
        self.assertEqual(meta["duration"], "01:35")
        self.assertEqual(meta["likes"], 12)
        self.assertEqual(meta["comments"], 3)
        self.assertEqual(meta["shares"], 4)
        self.assertTrue(meta["published_at"])

    def test_bilibili_extract_from_page_uses_api_then_ytdlp_for_media(self):
        collector = load_collector()
        cfg = {"yt_dlp": {"enabled": True}, "platforms": {}}

        collector.fetch_text = lambda url, cfg, platform: ("<html></html>", url)
        collector.extract_bilibili_api = lambda url, cfg: {
            "source_url": url,
            "final_url": url,
            "platform": "B站",
            "content_type": "video",
            "title": "API标题",
            "caption": "",
            "cover_url": "https://example.com/cover.jpg",
            "duration": "02:00",
            "likes": 1,
            "comments": 2,
            "shares": 3,
            "published_at": "2026年01月01日00时00分00秒",
            "media_url": "",
        }
        collector.extract_with_ytdlp = lambda url, cfg: {"media_url": "https://example.com/video.mp4", "title": "yt标题"}

        meta = collector.extract_from_page("https://www.bilibili.com/video/BVabc123", cfg)

        self.assertEqual(meta["platform"], "B站")
        self.assertEqual(meta["title"], "API标题")
        self.assertEqual(meta["media_url"], "https://example.com/video.mp4")

    def test_webhook_worker_transcribes_new_blank_records(self):
        collector = load_collector()
        jobs = queue.Queue()
        jobs.put(("tblA", "recNew"))
        stop_event = threading.Event()
        captured = []
        cfg = {"fields": collector.DEFAULT_FIELDS, "feishu": {"table_id": "tblA"}}

        collector.list_fields = lambda table_cfg: [{"field_name": name, "type": 1} for name in collector.DEFAULT_FIELDS.values()]
        collector.get_record = lambda table_cfg, record_id: {
            "record_id": record_id,
            "fields": {"作品链接": "https://www.bilibili.com/video/BV1xx411c7mD/"},
        }

        def fake_process(record, table_cfg, field_types, transcribe=True):
            captured.append(transcribe)
            stop_event.set()
            return "transcribed"

        collector.process_record = fake_process

        worker = threading.Thread(target=collector.webhook_worker, args=(cfg, jobs, stop_event), daemon=True)
        worker.start()
        jobs.join()
        stop_event.set()
        worker.join(timeout=2)

        self.assertEqual(captured, [True])

    def test_should_trigger_login_gate_for_login_statuses(self):
        collector = load_collector()

        self.assertTrue(collector.should_trigger_login_gate("需登录"))
        self.assertTrue(collector.should_trigger_login_gate("需Cookie"))
        self.assertFalse(collector.should_trigger_login_gate("网络异常"))

    def test_login_gate_cooldown_allows_one_open_per_window(self):
        collector = load_collector()
        state = {}

        self.assertTrue(collector.login_gate_cooldown_allows("抖音", state, cooldown=300, now=1000.0))
        self.assertFalse(collector.login_gate_cooldown_allows("抖音", state, cooldown=300, now=1100.0))
        self.assertTrue(collector.login_gate_cooldown_allows("抖音", state, cooldown=300, now=1301.0))

    def test_login_gate_defaults_are_not_high_frequency(self):
        collector = load_collector()

        gate = collector.login_gate_config({})

        self.assertGreaterEqual(gate["retry_interval"], 180)
        self.assertLessEqual(gate["max_retry_attempts"], 10)

    def test_health_config_defaults_are_low_frequency(self):
        collector = load_collector()

        health = collector.health_config({})

        self.assertTrue(health["enabled"])
        self.assertGreaterEqual(health["interval"], 300)
        self.assertEqual(health["listener_label"], "com.chen.content-link-collector.event-listener")

    def test_table_health_summary_counts_waiting_login_and_blank_rows(self):
        collector = load_collector()
        cfg = {"feishu": {"table_id": "tblA"}, "fields": collector.DEFAULT_FIELDS}

        collector.discover_feishu_table_ids = lambda cfg: ["tblA"]
        collector.list_records = lambda table_cfg: [
            {"record_id": "rec1", "fields": {"作品链接": "https://www.douyin.com/video/1", "抓取状态": "成功"}},
            {"record_id": "rec2", "fields": {"作品链接": "https://www.douyin.com/video/2", "抓取状态": "等待登录"}},
            {"record_id": "rec3", "fields": {"作品链接": "https://www.xiaohongshu.com/explore/abc"}},
            {"record_id": "rec4", "fields": {}},
        ]

        summary = collector.table_health_summary(cfg)

        self.assertEqual(summary["tables"][0]["table_id"], "tblA")
        self.assertEqual(summary["tables"][0]["status_counts"]["成功"], 1)
        self.assertEqual(summary["tables"][0]["status_counts"]["等待登录"], 1)
        self.assertEqual(summary["tables"][0]["blank_link_rows"], 1)
        self.assertEqual(summary["waiting_login"], [{"table_id": "tblA", "record_id": "rec2", "platform": "抖音"}])
        self.assertEqual(summary["blank_jobs"], [("tblA", "rec3")])

    def test_health_check_repairs_listener_and_browser(self):
        collector = load_collector()
        calls = []
        cfg = {
            "feishu": {"table_id": "tblA"},
            "fields": collector.DEFAULT_FIELDS,
            "browser_fallback": {"enabled": True},
            "health": {"listener_label": "listener.test"},
        }

        collector.launchctl_service_running = lambda label: False
        collector.launchctl_kickstart = lambda label: calls.append(("kickstart", label)) or True
        collector.browser_fallback_config = lambda cfg: {"enabled": True}
        collector.cdp_browser_available = lambda fallback_cfg: False
        collector.launch_cdp_browser = lambda fallback_cfg, start_url="about:blank": calls.append(("browser", start_url))
        collector.table_health_summary = lambda cfg: {"tables": [], "waiting_login": [], "blank_jobs": []}

        result = collector.run_health_check(cfg, repair=True)

        self.assertEqual(result["listener"]["status"], "restarted")
        self.assertEqual(result["browser"]["status"], "started")
        self.assertIn(("kickstart", "listener.test"), calls)
        self.assertIn(("browser", "https://www.douyin.com/"), calls)

    def test_should_process_blank_record_only_for_new_link_rows(self):
        collector = load_collector()
        cfg = {"fields": collector.DEFAULT_FIELDS}

        self.assertTrue(
            collector.should_process_blank_record(
                {"fields": {"作品链接": "https://www.douyin.com/video/123456789"}},
                cfg,
            )
        )
        self.assertFalse(
            collector.should_process_blank_record(
                {"fields": {"作品链接": "https://www.douyin.com/video/123456789", "抓取状态": "成功"}},
                cfg,
            )
        )
        self.assertFalse(
            collector.should_process_blank_record({"fields": {"作品标题": "已有标题"}}, cfg)
        )

    def test_should_process_retry_transcript_rows(self):
        collector = load_collector()
        cfg = {"fields": collector.DEFAULT_FIELDS}

        self.assertTrue(
            collector.should_process_blank_record(
                {
                    "fields": {
                        "作品链接": "https://www.douyin.com/video/123456789",
                        "抓取状态": "需登录",
                    }
                },
                cfg,
            )
        )
        self.assertFalse(
            collector.should_process_blank_record(
                {
                    "fields": {
                        "作品链接": "https://www.douyin.com/video/123456789",
                        "抓取状态": "等待登录",
                        "抓取时间": "2026-06-27 10:00:00",
                    }
                },
                cfg,
                now=collector.dt.datetime(2026, 6, 27, 10, 1, 0),
            )
        )
        self.assertTrue(
            collector.should_process_blank_record(
                {
                    "fields": {
                        "作品链接": "https://www.douyin.com/video/123456789",
                        "抓取状态": "等待登录",
                        "抓取时间": "2026-06-27 10:00:00",
                    }
                },
                cfg,
                now=collector.dt.datetime(2026, 6, 27, 10, 4, 0),
            )
        )
        self.assertTrue(
            collector.should_process_blank_record(
                {
                    "fields": {
                        "作品链接": "https://www.douyin.com/video/123456789",
                        "抓取状态": "浏览器未就绪",
                        "抓取时间": "2026-06-27 10:00:00",
                    }
                },
                cfg,
                now=collector.dt.datetime(2026, 6, 27, 10, 4, 0),
            )
        )
        self.assertTrue(
            collector.should_process_blank_record(
                {
                    "fields": {
                        "作品链接": "https://www.douyin.com/video/123456789",
                        "作品标题": "已有标题",
                        "抓取状态": "待转写",
                    }
                },
                cfg,
            )
        )
        self.assertTrue(
            collector.should_process_blank_record(
                {
                    "fields": {
                        "作品链接": "https://www.douyin.com/video/123456789",
                        "作品标题": "已有标题",
                        "抓取状态": "网络异常",
                    }
                },
                cfg,
            )
        )
        self.assertFalse(
            collector.should_process_blank_record(
                {
                    "fields": {
                        "作品链接": "https://www.douyin.com/video/123456789",
                        "作品标题": "已有标题",
                        "抓取状态": "无音频",
                    }
                },
                cfg,
            )
        )

    def test_should_process_rows_with_browser_placeholder_title(self):
        collector = load_collector()
        cfg = {"fields": collector.DEFAULT_FIELDS}

        self.assertTrue(
            collector.should_process_blank_record(
                {
                    "fields": {
                        "作品链接": "https://www.douyin.com/video/123456789",
                        "作品标题": "PC Tab",
                        "抓取状态": "成功",
                    }
                },
                cfg,
            )
        )

    def test_should_process_rows_with_old_browser_launch_error(self):
        collector = load_collector()
        cfg = {"fields": collector.DEFAULT_FIELDS}

        self.assertTrue(
            collector.should_process_blank_record(
                {
                    "fields": {
                        "作品链接": "https://www.douyin.com/video/123456789",
                        "文案": "{'cache_switch': False, 'enable': 1}",
                        "抓取状态": "待人工确认",
                        "错误信息": "BrowserType.launch_persistent_context: Target page closed",
                    }
                },
                cfg,
            )
        )

    def test_classify_transient_tls_error_as_retryable(self):
        collector = load_collector()

        self.assertEqual(
            collector.classify_processing_error(
                RuntimeError("<urlopen error EOF occurred in violation of protocol (_ssl.c:1129)>")
            ),
            "网络异常",
        )

    def test_tencent_rec_task_payload_uses_local_audio_data(self):
        collector = load_collector()

        payload = collector.tencent_create_rec_task_payload(b"abc123", {"engine_model_type": "16k_zh"})

        self.assertEqual(payload["EngineModelType"], "16k_zh")
        self.assertEqual(payload["ChannelNum"], 1)
        self.assertEqual(payload["ResTextFormat"], 3)
        self.assertEqual(payload["SourceType"], 1)
        self.assertEqual(payload["Data"], "YWJjMTIz")
        self.assertEqual(payload["DataLen"], 6)

    def test_clean_tencent_transcript_removes_timestamps(self):
        collector = load_collector()

        self.assertEqual(
            collector.clean_tencent_transcript("[0:0.020,0:2.380]  腾讯云语音识别欢迎您。\n[0:2.4,0:3.0] 第二句。"),
            "腾讯云语音识别欢迎您。\n第二句。",
        )

    def test_tencent_audio_size_guard_falls_back_to_local(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "audio.mp3"
            audio.write_bytes(b"x" * (5 * 1024 * 1024 + 1))

            with self.assertRaisesRegex(RuntimeError, "腾讯云本地音频上传限制"):
                collector.tencent_transcribe_file({"tencent_asr": {}}, audio)

    def test_should_transcribe_record_for_new_and_pending_rows(self):
        collector = load_collector()
        cfg = {"fields": collector.DEFAULT_FIELDS}

        self.assertTrue(
            collector.should_transcribe_record(
                {"fields": {"作品链接": "https://www.douyin.com/video/123456789"}},
                cfg,
            )
        )
        self.assertTrue(
            collector.should_transcribe_record(
                {
                    "fields": {
                        "作品链接": "https://www.douyin.com/video/123456789",
                        "作品标题": "已有标题",
                        "抓取状态": "待转写",
                    }
                },
                cfg,
            )
        )

    def test_first_url_prefers_full_url_list_before_douyin_uri(self):
        collector = load_collector()

        self.assertEqual(
            collector.first_url(
                {
                    "uri": "tos-cn-i-dy/relative-cover",
                    "url_list": ["https://p3-sign.douyinpic.com/full-cover.webp"],
                }
            ),
            "https://p3-sign.douyinpic.com/full-cover.webp",
        )

    def test_desktop_db_creates_default_and_custom_tables(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)

            tables = collector.desktop_list_tables(db)
            self.assertEqual(tables[0]["name"], "默认采集表")

            custom = collector.desktop_create_table(db, "抖音选题库", "抖音")

            self.assertEqual(custom["name"], "抖音选题库")
            self.assertEqual(custom["default_platform"], "抖音")

            renamed = collector.desktop_rename_table(db, custom["id"], "改名后的表")

            self.assertEqual(renamed["name"], "改名后的表")

    def test_desktop_table_names_are_unique_for_blank_create_and_rename(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)

            first = collector.desktop_create_table(db, "", "抖音")
            second = collector.desktop_create_table(db, "", "抖音")

            self.assertNotEqual(first["name"], second["name"])
            with self.assertRaises(ValueError):
                collector.desktop_rename_table(db, second["id"], first["name"])

    def test_mobile_inbox_table_is_created_once_and_cannot_be_renamed_or_deleted(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            first = collector.desktop_ensure_mobile_inbox_table(db)
            second = collector.desktop_ensure_mobile_inbox_table(db)

            self.assertEqual(first["id"], second["id"])
            self.assertEqual(first["name"], "手机待扒取")
            self.assertEqual(first["system_key"], "mobile_inbox")

            listed = [table for table in collector.desktop_list_tables(db) if table["id"] == first["id"]][0]
            self.assertEqual(listed["system_key"], "mobile_inbox")
            self.assertIn("function desktop_table_is_system", collector.DESKTOP_APP_HTML)

            with self.assertRaisesRegex(ValueError, "固定表"):
                collector.desktop_rename_table(db, first["id"], "别的名字")
            with self.assertRaisesRegex(ValueError, "固定表"):
                collector.desktop_delete_table(db, first["id"])

    def test_mobile_inbox_sync_is_idempotent(self):
        collector = load_collector()
        records = [
            {
                "record_id": "recMobile001",
                "last_modified_time": "1784286000000",
                "fields": {
                    "作品链接": "参考 https://v.douyin.com/abc ",
                    "平台": "抖音",
                    "抓取状态": "待扒取",
                    "提交时间": "2026-07-17 19:00:00",
                    "手机备注": "开头可参考",
                    "来源": "手机收集箱",
                },
            }
        ]
        cfg = {
            "fields": collector.DEFAULT_FIELDS,
            "feishu": {
                "app_id": "cli_xxx",
                "app_secret": "secret",
                "app_token": "app_token",
                "table_id": "tblMain",
                "mobile_inbox_table_id": "tblMobile",
                "base_url": "https://open.feishu.cn",
            },
            "mobile_inbox": {"enabled": True, "poll_interval": 10},
        }
        seen_table_ids = []
        original_list_records = collector.list_records

        def fake_list_records(table_cfg, page_size=100):
            seen_table_ids.append(table_cfg["feishu"]["table_id"])
            return list(records)

        collector.list_records = fake_list_records
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "desktop.sqlite3"
                table = collector.desktop_ensure_mobile_inbox_table(db)

                first = collector.desktop_sync_mobile_inbox_once(db, cfg)
                second = collector.desktop_sync_mobile_inbox_once(db, cfg)

                self.assertEqual(first["created"], 1)
                self.assertEqual(second["created"], 0)
                self.assertEqual(seen_table_ids[:2], ["tblMobile", "tblMobile"])

                items = collector.desktop_list_items(db, table["id"])
                self.assertEqual(len(items), 1)
                item = items[0]
                self.assertEqual(item["source_url"], "https://v.douyin.com/abc")
                self.assertEqual(item["status"], "待扒取")
                self.assertEqual(item["error"], "开头可参考")
                self.assertIn("recMobile001", item["raw_metadata_json"])
                metadata = json.loads(item["raw_metadata_json"])
                self.assertEqual(metadata["mobile_inbox_record_id"], "recMobile001")
                self.assertEqual(metadata["mobile_note"], "开头可参考")
                self.assertEqual(metadata["mobile_submitted_at"], "2026-07-17 19:00:00")
                self.assertEqual(metadata["mobile_remote_modified_at"], "1784286000000")
                self.assertEqual(metadata["source"], "手机收集箱")

                records.extend(
                    {
                        "record_id": f"recOffline{index:03d}",
                        "last_modified_time": str(1784286000000 + index),
                        "fields": {
                            "作品链接": f"https://example.com/offline-{index}",
                            "平台": "" if index == 2 else "抖音",
                            "抓取状态": "待扒取",
                            "手机备注": f"离线 {index}",
                            "来源": "手机收集箱",
                        },
                    }
                    for index in range(2, 51)
                )
                offline = collector.desktop_sync_mobile_inbox_once(db, cfg)
                self.assertEqual(offline["created"], 49)

                items = collector.desktop_list_items(db, table["id"])
                self.assertEqual(len(items), 50)
                unknown = [row for row in items if row["source_url"] == "https://example.com/offline-2"][0]
                self.assertEqual(unknown["platform"], "未知")

                collector.desktop_update_item(db, item["id"], {"status": "成功", "error": "已人工完成"})
                collector.desktop_sync_mobile_inbox_once(db, cfg)
                refreshed = collector.desktop_get_item(db, item["id"])
                self.assertEqual(refreshed["status"], "成功")
                self.assertEqual(refreshed["error"], "已人工完成")
                self.assertIn("开头可参考", refreshed["raw_metadata_json"])

                records[0]["last_modified_time"] = "1784285999999"
                records[0]["fields"]["手机备注"] = "旧备注"
                collector.desktop_sync_mobile_inbox_once(db, cfg)
                refreshed = collector.desktop_get_item(db, item["id"])
                self.assertEqual(refreshed["status"], "成功")
                self.assertEqual(refreshed["error"], "已人工完成")
                self.assertIn("开头可参考", refreshed["raw_metadata_json"])
                self.assertNotIn("旧备注", refreshed["raw_metadata_json"])
        finally:
            collector.list_records = original_list_records

    def test_mobile_inbox_pending_items_do_not_auto_scrape(self):
        collector = load_collector()
        cfg = {"fields": collector.DEFAULT_FIELDS}
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            table = collector.desktop_ensure_mobile_inbox_table(db)
            mobile = collector.desktop_save_item(
                db,
                table["id"],
                {
                    "platform": "抖音",
                    "source_url": "https://v.douyin.com/pending",
                    "status": "待扒取",
                    "raw_metadata_json": "{}",
                },
            )

            self.assertEqual(collector.desktop_queue_pending_items(db), [])
            self.assertFalse(
                collector.should_process_blank_record(
                    {"fields": {"作品链接": mobile["source_url"], "抓取状态": "待扒取"}},
                    cfg,
                )
            )

    def test_mobile_inbox_worker_retries_after_system_exit(self):
        collector = load_collector()
        attempts = []
        original_sync = collector.desktop_sync_mobile_inbox_once

        class FastStopEvent:
            def is_set(self):
                return len(attempts) >= 2

            def wait(self, interval):
                return False

        def flaky_sync(db_path, cfg):
            attempts.append(str(db_path))
            if len(attempts) == 1:
                raise SystemExit("feishu network failed")
            return {"ok": True, "created": 0, "updated": 0, "skipped": 0, "seen": 0}

        collector.desktop_sync_mobile_inbox_once = flaky_sync
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "desktop.sqlite3"
                collector.desktop_mobile_inbox_worker(
                    db,
                    {"mobile_inbox": {"poll_interval": 5}},
                    FastStopEvent(),
                )
            self.assertEqual(len(attempts), 2)
        finally:
            collector.desktop_sync_mobile_inbox_once = original_sync

    def test_mobile_inbox_stale_terminal_status_does_not_overwrite_local_success(self):
        collector = load_collector()
        cfg = {
            "fields": collector.DEFAULT_FIELDS,
            "feishu": {
                "app_id": "cli_xxx",
                "app_secret": "secret",
                "app_token": "app_token",
                "table_id": "tblMain",
                "mobile_inbox_table_id": "tblMobile",
            },
            "mobile_inbox": {"enabled": True, "poll_interval": 10},
        }
        records = [
            {
                "record_id": "recMobileStale",
                "last_modified_time": "1784285999000",
                "fields": {
                    "作品链接": "https://v.douyin.com/stale",
                    "平台": "抖音",
                    "抓取状态": "下载失败",
                    "手机备注": "旧失败",
                    "来源": "手机收集箱",
                },
            }
        ]
        original_list_records = collector.list_records
        collector.list_records = lambda table_cfg, page_size=100: list(records)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "desktop.sqlite3"
                table = collector.desktop_ensure_mobile_inbox_table(db)
                saved = collector.desktop_save_item(
                    db,
                    table["id"],
                    {
                        "platform": "抖音",
                        "source_url": "https://v.douyin.com/stale",
                        "source_type": "mobile_inbox",
                        "status": "成功",
                        "error": "本地已完成",
                        "raw_metadata_json": json.dumps(
                            {
                                "mobile_inbox_record_id": "recMobileStale",
                                "mobile_note": "新备注",
                                "mobile_remote_modified_at": "1784286000000",
                                "source": "手机收集箱",
                            },
                            ensure_ascii=False,
                        ),
                    },
                )

                result = collector.desktop_sync_mobile_inbox_once(db, cfg)

                self.assertEqual(result["created"], 0)
                refreshed = collector.desktop_get_item(db, saved["id"])
                self.assertEqual(refreshed["status"], "成功")
                self.assertEqual(refreshed["error"], "本地已完成")
                self.assertIn("1784286000000", refreshed["raw_metadata_json"])
                self.assertNotIn("旧失败", refreshed["raw_metadata_json"])
        finally:
            collector.list_records = original_list_records

    def test_mobile_inbox_matches_record_id_when_url_changes_and_merges_conflict(self):
        collector = load_collector()
        cfg = {
            "fields": collector.DEFAULT_FIELDS,
            "feishu": {
                "app_id": "cli_xxx",
                "app_secret": "secret",
                "app_token": "app_token",
                "table_id": "tblMain",
                "mobile_inbox_table_id": "tblMobile",
            },
            "mobile_inbox": {"enabled": True, "poll_interval": 10},
        }
        records = [
            {
                "record_id": "recMove",
                "last_modified_time": "1784286001000",
                "fields": {
                    "作品链接": "https://example.com/b",
                    "平台": "抖音",
                    "抓取状态": "待扒取",
                    "手机备注": "改到 b",
                    "来源": "手机收集箱",
                },
            }
        ]
        original_list_records = collector.list_records
        collector.list_records = lambda table_cfg, page_size=100: list(records)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "desktop.sqlite3"
                table = collector.desktop_ensure_mobile_inbox_table(db)
                moved = collector.desktop_save_item(
                    db,
                    table["id"],
                    {
                        "platform": "抖音",
                        "source_url": "https://example.com/a",
                        "source_type": "mobile_inbox",
                        "status": "待扒取",
                        "error": "旧 a",
                        "raw_metadata_json": json.dumps(
                            {
                                "mobile_inbox_record_id": "recMove",
                                "mobile_note": "旧 a",
                                "mobile_remote_modified_at": "1784286000000",
                                "source": "手机收集箱",
                            },
                            ensure_ascii=False,
                        ),
                    },
                )

                first = collector.desktop_sync_mobile_inbox_once(db, cfg)
                changed = collector.desktop_get_item(db, moved["id"])
                self.assertEqual(first["created"], 0)
                self.assertEqual(changed["source_url"], "https://example.com/b")
                self.assertIn("recMove", changed["raw_metadata_json"])
                self.assertEqual(len(collector.desktop_list_items(db, table["id"])), 1)

                conflict_db = Path(tmp) / "desktop-conflict.sqlite3"
                conflict_table = collector.desktop_ensure_mobile_inbox_table(conflict_db)
                conflict_moved = collector.desktop_save_item(
                    conflict_db,
                    conflict_table["id"],
                    {
                        "platform": "抖音",
                        "source_url": "https://example.com/a",
                        "source_type": "mobile_inbox",
                        "status": "待扒取",
                        "error": "旧 a",
                        "raw_metadata_json": json.dumps(
                            {
                                "mobile_inbox_record_id": "recMove",
                                "mobile_note": "旧 a",
                                "mobile_remote_modified_at": "1784286000000",
                                "source": "手机收集箱",
                            },
                            ensure_ascii=False,
                        ),
                    },
                )
                conflict = collector.desktop_save_item(
                    conflict_db,
                    conflict_table["id"],
                    {
                        "platform": "抖音",
                        "source_url": "https://example.com/b",
                        "source_type": "mobile_inbox",
                        "status": "待扒取",
                        "error": "已有 b",
                        "raw_metadata_json": json.dumps(
                            {
                                "mobile_inbox_record_id": "recOther",
                                "mobile_note": "已有 b",
                                "mobile_remote_modified_at": "1784286000000",
                                "source": "手机收集箱",
                            },
                            ensure_ascii=False,
                        ),
                    },
                )

                second = collector.desktop_sync_mobile_inbox_once(conflict_db, cfg)

                items = collector.desktop_list_items(conflict_db, conflict_table["id"])
                urls = [item["source_url"] for item in items]
                record_ids = [
                    json.loads(item["raw_metadata_json"]).get("mobile_inbox_record_id")
                    for item in items
                ]
                self.assertEqual(second["created"], 0)
                self.assertEqual(urls.count("https://example.com/b"), 1)
                self.assertNotIn("https://example.com/a", urls)
                self.assertEqual(record_ids.count("recMove"), 1)
                self.assertNotIn("recOther", record_ids)
                self.assertTrue(any(item["id"] == conflict["id"] for item in items))
                self.assertFalse(any(item["id"] == conflict_moved["id"] for item in items))
        finally:
            collector.list_records = original_list_records

    def test_mobile_inbox_conflict_merge_preserves_local_results_and_download_tasks(self):
        collector = load_collector()
        cfg = {
            "fields": collector.DEFAULT_FIELDS,
            "feishu": {
                "app_id": "cli_xxx",
                "app_secret": "secret",
                "app_token": "app_token",
                "table_id": "tblMain",
                "mobile_inbox_table_id": "tblMobile",
            },
            "mobile_inbox": {"enabled": True, "poll_interval": 10},
        }

        def remote_record(mtime):
            return {
                "record_id": "recRichMove",
                "last_modified_time": str(mtime),
                "fields": {
                    "作品链接": "https://example.com/rich-b",
                    "平台": "抖音",
                    "抓取状态": "待扒取",
                    "手机备注": "手机改到 b",
                    "来源": "手机收集箱",
                },
            }

        def seed_conflict(db):
            table = collector.desktop_ensure_mobile_inbox_table(db)
            record_item = collector.desktop_save_item(
                db,
                table["id"],
                {
                    "platform": "抖音",
                    "source_url": "https://example.com/rich-a",
                    "source_type": "mobile_inbox",
                    "title": "本地成功标题",
                    "caption": "本地成功文案",
                    "cover_url": "https://example.com/cover.jpg",
                    "duration": "01:23",
                    "likes": 12,
                    "comments": 3,
                    "shares": 4,
                    "published_at": "2026-07-18 10:00:00",
                    "status": "成功",
                    "error": "本地成功备注",
                    "raw_metadata_json": json.dumps(
                        {
                            "mobile_inbox_record_id": "recRichMove",
                            "mobile_note": "旧 a",
                            "mobile_remote_modified_at": "1784286000000",
                            "source": "手机收集箱",
                        },
                        ensure_ascii=False,
                    ),
                },
            )
            target_item = collector.desktop_save_item(
                db,
                table["id"],
                {
                    "platform": "抖音",
                    "source_url": "https://example.com/rich-b",
                    "source_type": "mobile_inbox",
                    "title": "目标旧标题",
                    "status": "待扒取",
                    "error": "目标旧备注",
                    "raw_metadata_json": json.dumps(
                        {
                            "mobile_inbox_record_id": "recOtherRich",
                            "mobile_note": "已有 b",
                            "mobile_remote_modified_at": "1784286000000",
                            "source": "手机收集箱",
                        },
                        ensure_ascii=False,
                    ),
                },
            )
            with collector.desktop_connect(db) as conn:
                conn.execute(
                    """
                    UPDATE collected_items
                    SET video_path = ?, max_daily_card = ?, daily_selected = 1,
                        daily_date = ?, daily_sort = ?, max_feedback = ?
                    WHERE id = ?
                    """,
                    (
                        "/tmp/local-success.mp4",
                        "本地成功 MAX 卡片",
                        "2026-07-18",
                        7,
                        "Max 已看",
                        record_item["id"],
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO video_download_batches(id, mode, directory, created_at, updated_at)
                    VALUES ('batch-record', 'single', '/tmp/downloads', '2026-07-18 10:00:00', '2026-07-18 10:00:00')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO video_download_batches(id, mode, directory, created_at, updated_at)
                    VALUES ('batch-target', 'single', '/tmp/downloads', '2026-07-18 10:00:01', '2026-07-18 10:00:01')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO video_download_tasks(id, batch_id, item_id, status, stage, created_at, updated_at)
                    VALUES ('task-record-active', 'batch-record', ?, 'queued', '等待下载', '2026-07-18 10:00:02', '2026-07-18 10:00:02')
                    """,
                    (record_item["id"],),
                )
                conn.execute(
                    """
                    INSERT INTO video_download_tasks(
                        id, batch_id, item_id, status, stage, output_path, method, completed_at, created_at, updated_at
                    )
                    VALUES (
                        'task-record-completed', 'batch-record', ?, 'completed', '下载完成',
                        '/tmp/local-success.mp4', 'yt-dlp', '2026-07-18 10:05:00',
                        '2026-07-18 10:04:00', '2026-07-18 10:05:00'
                    )
                    """,
                    (record_item["id"],),
                )
                conn.execute(
                    """
                    INSERT INTO video_download_tasks(id, batch_id, item_id, status, stage, created_at, updated_at)
                    VALUES ('task-target-active', 'batch-target', ?, 'preparing', '获取视频地址', '2026-07-18 10:06:00', '2026-07-18 10:06:00')
                    """,
                    (target_item["id"],),
                )
            return table, record_item, target_item

        original_list_records = collector.list_records
        try:
            with tempfile.TemporaryDirectory() as tmp:
                stale_db = Path(tmp) / "stale.sqlite3"
                stale_table, stale_record, stale_target = seed_conflict(stale_db)
                collector.list_records = lambda table_cfg, page_size=100: [remote_record(1784285999999)]

                stale_result = collector.desktop_sync_mobile_inbox_once(stale_db, cfg)

                stale_items = collector.desktop_list_items(stale_db, stale_table["id"])
                stale_urls = {item["id"]: item["source_url"] for item in stale_items}
                self.assertEqual(stale_result["created"], 0)
                self.assertEqual(stale_result["updated"], 0)
                self.assertEqual(len(stale_items), 2)
                self.assertEqual(stale_urls[stale_record["id"]], "https://example.com/rich-a")
                self.assertEqual(stale_urls[stale_target["id"]], "https://example.com/rich-b")
                with collector.desktop_connect(stale_db) as conn:
                    stale_tasks = [collector.desktop_row_to_dict(row) for row in conn.execute(
                        "SELECT id, item_id, status FROM video_download_tasks ORDER BY id"
                    ).fetchall()]
                self.assertEqual(
                    {task["id"]: task["item_id"] for task in stale_tasks},
                    {
                        "task-record-active": stale_record["id"],
                        "task-record-completed": stale_record["id"],
                        "task-target-active": stale_target["id"],
                    },
                )

                merge_db = Path(tmp) / "merge.sqlite3"
                merge_table, merge_record, merge_target = seed_conflict(merge_db)
                collector.list_records = lambda table_cfg, page_size=100: [remote_record(1784286001000)]

                deferred_result = collector.desktop_sync_mobile_inbox_once(merge_db, cfg)

                deferred_items = collector.desktop_list_items(merge_db, merge_table["id"])
                self.assertEqual(deferred_result["created"], 0)
                self.assertEqual(deferred_result["updated"], 0)
                self.assertEqual(deferred_result["skipped"], 1)
                self.assertEqual(len(deferred_items), 2)
                with collector.desktop_connect(merge_db) as conn:
                    conn.execute(
                        """
                        UPDATE video_download_tasks
                        SET status = 'cancelled', stage = '测试结束活动任务', completed_at = ?, updated_at = ?
                        WHERE status IN ('queued', 'preparing', 'downloading', 'merging')
                        """,
                        ("2026-07-18 10:07:00", "2026-07-18 10:07:00"),
                    )

                merge_result = collector.desktop_sync_mobile_inbox_once(merge_db, cfg)

                merged_items = collector.desktop_list_items(merge_db, merge_table["id"])
                self.assertEqual(merge_result["created"], 0)
                self.assertEqual(len(merged_items), 1)
                merged = merged_items[0]
                self.assertEqual(merged["id"], merge_target["id"])
                self.assertEqual(merged["source_url"], "https://example.com/rich-b")
                self.assertEqual(merged["status"], "成功")
                self.assertEqual(merged["error"], "本地成功备注")
                self.assertEqual(merged["title"], "本地成功标题")
                self.assertEqual(merged["caption"], "本地成功文案")
                self.assertEqual(merged["cover_url"], "https://example.com/cover.jpg")
                self.assertEqual(merged["duration"], "01:23")
                self.assertEqual(merged["likes"], 12)
                self.assertEqual(merged["comments"], 3)
                self.assertEqual(merged["shares"], 4)
                self.assertEqual(merged["published_at"], "2026-07-18 10:00:00")
                self.assertEqual(merged["video_path"], "/tmp/local-success.mp4")
                self.assertEqual(merged["max_daily_card"], "本地成功 MAX 卡片")
                self.assertEqual(merged["daily_selected"], 1)
                self.assertEqual(merged["daily_date"], "2026-07-18")
                self.assertEqual(merged["daily_sort"], 7)
                self.assertEqual(merged["max_feedback"], "Max 已看")
                merged_meta = json.loads(merged["raw_metadata_json"])
                self.assertEqual(merged_meta["mobile_inbox_record_id"], "recRichMove")
                self.assertNotEqual(merged_meta["mobile_inbox_record_id"], "recOtherRich")
                with collector.desktop_connect(merge_db) as conn:
                    merged_tasks = [collector.desktop_row_to_dict(row) for row in conn.execute(
                        "SELECT id, item_id, status, stage, output_path FROM video_download_tasks ORDER BY id"
                    ).fetchall()]
                self.assertEqual({task["item_id"] for task in merged_tasks}, {merge_target["id"]})
                task_statuses = {task["id"]: task["status"] for task in merged_tasks}
                self.assertEqual(task_statuses["task-target-active"], "cancelled")
                self.assertEqual(task_statuses["task-record-completed"], "completed")
                self.assertEqual(task_statuses["task-record-active"], "cancelled")
                active_count = sum(
                    1
                    for task in merged_tasks
                    if task["status"] in {"queued", "preparing", "downloading", "merging"}
                )
                self.assertEqual(active_count, 0)
                self.assertFalse(any(item["id"] == merge_record["id"] for item in merged_items))
        finally:
            collector.list_records = original_list_records

    def test_running_download_uses_migrated_item_after_mobile_inbox_conflict_merge(self):
        collector = load_collector()
        cfg = {
            "fields": collector.DEFAULT_FIELDS,
            "feishu": {
                "app_id": "cli_xxx",
                "app_secret": "secret",
                "app_token": "app_token",
                "table_id": "tblMain",
                "mobile_inbox_table_id": "tblMobile",
            },
            "mobile_inbox": {"enabled": True, "poll_interval": 10},
        }
        records = [
            {
                "record_id": "recDownloadMove",
                "last_modified_time": "1784286001000",
                "fields": {
                    "作品链接": "https://example.com/download-b",
                    "平台": "抖音",
                    "抓取状态": "待扒取",
                    "手机备注": "下载中改到 b",
                    "来源": "手机收集箱",
                },
            }
        ]
        started = threading.Event()
        release = threading.Event()
        output_path = ""
        original_save_video = collector.desktop_save_video_file
        original_list_records = collector.list_records

        def fake_save_video(db_path, item_id, cfg_arg, downloads_dir, progress_callback=None):
            if progress_callback:
                progress_callback({"stage": "下载中", "downloaded_bytes": 1, "total_bytes": 2, "progress": 50})
            started.set()
            self.assertTrue(release.wait(3), "download worker did not get released")
            collector.desktop_get_item(db_path, item_id)
            return {"path": output_path, "method": "fake"}

        collector.desktop_save_video_file = fake_save_video
        collector.list_records = lambda table_cfg, page_size=100: list(records)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                output_path = str(Path(tmp) / "merged-download.mp4")
                db = Path(tmp) / "desktop.sqlite3"
                table = collector.desktop_ensure_mobile_inbox_table(db)
                record_item = collector.desktop_save_item(
                    db,
                    table["id"],
                    {
                        "platform": "抖音",
                        "source_url": "https://example.com/download-a",
                        "source_type": "mobile_inbox",
                        "status": "成功",
                        "error": "本地已下载",
                        "raw_metadata_json": json.dumps(
                            {
                                "mobile_inbox_record_id": "recDownloadMove",
                                "mobile_note": "旧 a",
                                "mobile_remote_modified_at": "1784286000000",
                                "source": "手机收集箱",
                            },
                            ensure_ascii=False,
                        ),
                    },
                )
                survivor = collector.desktop_save_item(
                    db,
                    table["id"],
                    {
                        "platform": "抖音",
                        "source_url": "https://example.com/download-b",
                        "source_type": "mobile_inbox",
                        "status": "待扒取",
                        "raw_metadata_json": json.dumps(
                            {
                                "mobile_inbox_record_id": "recOtherDownload",
                                "mobile_remote_modified_at": "1784286000000",
                                "source": "手机收集箱",
                            },
                            ensure_ascii=False,
                        ),
                    },
                )
                batch = collector.desktop_create_download_batch(db, [record_item["id"]], "single")
                task_id = batch["tasks"][0]["id"]
                worker = threading.Thread(target=collector.desktop_run_download_task, args=(db, task_id, {}), daemon=True)
                worker.start()
                self.assertTrue(started.wait(3), "download worker did not enter fake downloader")

                deferred = collector.desktop_sync_mobile_inbox_once(db, cfg)
                self.assertEqual(deferred["updated"], 0)
                self.assertEqual(deferred["skipped"], 1)
                self.assertEqual(len(collector.desktop_list_items(db, table["id"])), 2)
                release.set()
                worker.join(3)

                self.assertFalse(worker.is_alive())
                merged = collector.desktop_sync_mobile_inbox_once(db, cfg)
                task = collector.desktop_download_queue_payload(db)["tasks"][0]
                survivor_after = collector.desktop_get_item(db, survivor["id"])
                self.assertGreaterEqual(merged["updated"], 1)
                self.assertEqual(task["id"], task_id)
                self.assertEqual(task["status"], "completed")
                self.assertEqual(task["item_id"], survivor["id"])
                self.assertEqual(survivor_after["video_path"], output_path)
                self.assertEqual(len(collector.desktop_list_items(db, table["id"])), 1)
        finally:
            collector.desktop_save_video_file = original_save_video
            collector.list_records = original_list_records
            release.set()

    def test_cancelled_running_download_is_not_marked_completed_after_release(self):
        collector = load_collector()
        started = threading.Event()
        release = threading.Event()
        output_path = ""
        original_save_video = collector.desktop_save_video_file

        def fake_save_video(db_path, item_id, cfg_arg, downloads_dir, progress_callback=None):
            if progress_callback:
                progress_callback({"stage": "下载中", "downloaded_bytes": 1, "total_bytes": 2, "progress": 50})
            started.set()
            self.assertTrue(release.wait(3), "download worker did not get released")
            return {"path": output_path, "method": "fake"}

        collector.desktop_save_video_file = fake_save_video
        try:
            with tempfile.TemporaryDirectory() as tmp:
                output_path = str(Path(tmp) / "cancelled-download.mp4")
                db = Path(tmp) / "desktop.sqlite3"
                collector.desktop_db_init(db)
                table = collector.desktop_list_tables(db)[0]
                item = collector.desktop_save_item(
                    db,
                    table["id"],
                    {
                        "platform": "抖音",
                        "source_url": "https://example.com/cancelled-download",
                        "source_type": "single",
                        "status": "成功",
                        "raw_metadata_json": "{}",
                    },
                )
                batch = collector.desktop_create_download_batch(db, [item["id"]], "single")
                task_id = batch["tasks"][0]["id"]
                worker = threading.Thread(target=collector.desktop_run_download_task, args=(db, task_id, {}), daemon=True)
                worker.start()
                self.assertTrue(started.wait(3), "download worker did not enter fake downloader")

                collector.desktop_update_download_task(
                    db,
                    task_id,
                    status="cancelled",
                    stage="用户取消",
                    completed_at="2026-07-18 12:00:00",
                )
                release.set()
                worker.join(3)

                self.assertFalse(worker.is_alive())
                task = collector.desktop_download_queue_payload(db)["tasks"][0]
                item_after = collector.desktop_get_item(db, item["id"])
                self.assertEqual(task["status"], "cancelled")
                self.assertEqual(task["stage"], "用户取消")
                self.assertEqual(task["output_path"], "")
                self.assertEqual(item_after["video_path"], "")
        finally:
            collector.desktop_save_video_file = original_save_video
            release.set()

    def test_cancel_between_final_state_read_and_completion_update_stays_cancelled(self):
        collector = load_collector()
        output_path = ""
        original_save_video = collector.desktop_save_video_file
        original_get_task = collector.desktop_get_download_task
        cancel_on_next_read = False

        def fake_save_video(db_path, item_id, cfg_arg, downloads_dir, progress_callback=None):
            nonlocal cancel_on_next_read
            cancel_on_next_read = True
            return {"path": output_path, "method": "fake"}

        def racing_get_task(db_path, task_id):
            nonlocal cancel_on_next_read
            task = original_get_task(db_path, task_id)
            if cancel_on_next_read:
                cancel_on_next_read = False
                collector.desktop_update_download_task(
                    db_path,
                    task_id,
                    status="cancelled",
                    stage="最终写入前取消",
                    completed_at="2026-07-18 12:30:00",
                )
            return task

        collector.desktop_save_video_file = fake_save_video
        collector.desktop_get_download_task = racing_get_task
        try:
            with tempfile.TemporaryDirectory() as tmp:
                output_path = str(Path(tmp) / "cancelled-at-finish.mp4")
                db = Path(tmp) / "desktop.sqlite3"
                collector.desktop_db_init(db)
                table = collector.desktop_list_tables(db)[0]
                item = collector.desktop_save_item(
                    db,
                    table["id"],
                    {
                        "platform": "抖音",
                        "source_url": "https://example.com/cancelled-at-finish",
                        "source_type": "single",
                        "status": "成功",
                        "raw_metadata_json": "{}",
                    },
                )
                batch = collector.desktop_create_download_batch(db, [item["id"]], "single")
                task_id = batch["tasks"][0]["id"]

                collector.desktop_run_download_task(db, task_id, {})

                task = collector.desktop_download_queue_payload(db)["tasks"][0]
                item_after = collector.desktop_get_item(db, item["id"])
                self.assertEqual(task["status"], "cancelled")
                self.assertEqual(task["stage"], "最终写入前取消")
                self.assertEqual(task["output_path"], "")
                self.assertEqual(item_after["video_path"], "")
        finally:
            collector.desktop_save_video_file = original_save_video
            collector.desktop_get_download_task = original_get_task

    def test_cancelled_task_before_worker_claim_releases_worker_slot(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]
            item = collector.desktop_save_item(
                db,
                table["id"],
                {
                    "platform": "抖音",
                    "source_url": "https://example.com/cancelled-before-claim",
                    "source_type": "single",
                    "status": "成功",
                    "raw_metadata_json": "{}",
                },
            )
            batch = collector.desktop_create_download_batch(db, [item["id"]], "single")
            task_id = batch["tasks"][0]["id"]
            collector.desktop_update_download_task(db, task_id, status="cancelled", stage="抢占前取消")
            worker_key = str(db.resolve())
            with collector.DESKTOP_DOWNLOAD_WORKERS_LOCK:
                collector.DESKTOP_DOWNLOAD_WORKERS[worker_key] = {task_id}

            collector.desktop_run_download_task(db, task_id, {})

            with collector.DESKTOP_DOWNLOAD_WORKERS_LOCK:
                self.assertNotIn(task_id, collector.DESKTOP_DOWNLOAD_WORKERS[worker_key])

    def test_desktop_db_saves_and_lists_items(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]

            saved = collector.desktop_save_item(
                db,
                table["id"],
                {
                    "platform": "抖音",
                    "source_url": "https://www.douyin.com/video/1",
                    "source_type": "single",
                    "title": "标题",
                    "caption": "逐字稿",
                    "cover_url": "https://example.com/cover.jpg",
                    "duration": "01:00",
                    "likes": 1,
                    "comments": 2,
                    "shares": 3,
                    "published_at": "2026年01月01日00时00分00秒",
                    "status": "成功",
                    "error": "",
                    "raw_metadata_json": "{}",
                },
            )
            items = collector.desktop_list_items(db, table["id"])

            self.assertEqual(items[0]["id"], saved["id"])
            self.assertEqual(items[0]["title"], "标题")

    def test_desktop_save_item_preserves_existing_data_when_retry_has_blanks(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]
            collector.desktop_save_item(
                db,
                table["id"],
                {
                    "platform": "抖音",
                    "source_url": "https://www.douyin.com/video/keep-me",
                    "source_type": "profile",
                    "title": "主页已发现标题",
                    "cover_url": "https://example.com/cover.jpg",
                    "duration": "01:00",
                    "likes": 7,
                    "status": "已发现链接",
                    "raw_metadata_json": '{"ok": true}',
                },
            )

            saved = collector.desktop_save_item(
                db,
                table["id"],
                {
                    "platform": "抖音",
                    "source_url": "https://www.douyin.com/video/keep-me",
                    "source_type": "profile",
                    "title": "",
                    "cover_url": "",
                    "duration": "",
                    "likes": None,
                    "status": "等待登录",
                    "error": "临时失败",
                    "raw_metadata_json": "{}",
                },
            )

            self.assertEqual(saved["title"], "主页已发现标题")
            self.assertEqual(saved["cover_url"], "https://example.com/cover.jpg")
            self.assertEqual(saved["duration"], "01:00")
            self.assertEqual(saved["likes"], 7)
            self.assertEqual(saved["status"], "等待登录")
            self.assertEqual(saved["error"], "临时失败")

    def test_desktop_update_item_edits_allowed_fields(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]
            saved = collector.desktop_save_item(
                db,
                table["id"],
                {
                    "platform": "抖音",
                    "source_url": "https://www.douyin.com/video/edit-me",
                    "source_type": "single",
                    "title": "旧标题",
                    "likes": 1,
                    "status": "成功",
                    "raw_metadata_json": "{}",
                },
            )

            updated = collector.desktop_update_item(
                db,
                saved["id"],
                {"title": "新标题", "likes": "12", "source_url": "https://evil.example/ignored"},
            )

            self.assertEqual(updated["title"], "新标题")
            self.assertEqual(updated["likes"], 12)
            self.assertEqual(updated["source_url"], "https://www.douyin.com/video/edit-me")

    def test_desktop_save_cover_file_writes_file(self):
        collector = load_collector()
        original_fetch_binary = collector.fetch_binary
        try:
            collector.fetch_binary = lambda url, cfg, platform: (b"image-bytes", "image/jpeg", ".jpg")
            with tempfile.TemporaryDirectory() as tmp:
                result = collector.desktop_save_cover_file(
                    "https://example.com/cover.jpg",
                    {},
                    "抖音",
                    Path(tmp),
                )

                saved = Path(result["path"])
                self.assertTrue(saved.exists())
                self.assertEqual(saved.read_bytes(), b"image-bytes")
                self.assertEqual(result["content_type"], "image/jpeg")
        finally:
            collector.fetch_binary = original_fetch_binary

    def test_desktop_save_video_file_uses_saved_media_url(self):
        collector = load_collector()
        original_download = getattr(collector, "download_media_url_to_file", None)
        try:
            calls = []

            def fake_download(url, cfg, platform, target):
                calls.append((url, platform, target.name))
                target.write_bytes(b"video-bytes")
                return {"content_type": "video/mp4", "bytes": len(b"video-bytes")}

            collector.download_media_url_to_file = fake_download
            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "desktop.sqlite3"
                downloads = Path(tmp) / "downloads"
                collector.desktop_db_init(db)
                table = collector.desktop_list_tables(db)[0]
                saved = collector.desktop_save_item(
                    db,
                    table["id"],
                    {
                        "platform": "抖音",
                        "source_url": "https://www.douyin.com/video/1",
                        "source_type": "single",
                        "title": "测试/视频:无水印",
                        "status": "成功",
                        "raw_metadata_json": json.dumps(
                            {
                                "platform": "抖音",
                                "title": "测试/视频:无水印",
                                "media_url": "https://v5.365yg.com/a?mime_type=video_mp4",
                            },
                            ensure_ascii=False,
                        ),
                    },
                )

                result = collector.desktop_save_video_file(db, saved["id"], {}, downloads)

                self.assertEqual(result["bytes"], len(b"video-bytes"))
                self.assertTrue(Path(result["path"]).exists())
                self.assertEqual(Path(result["path"]).read_bytes(), b"video-bytes")
                self.assertEqual(calls[0][0], "https://v5.365yg.com/a?mime_type=video_mp4")
                self.assertIn("测试_视频_无水印", Path(result["path"]).name)
        finally:
            if original_download is not None:
                collector.download_media_url_to_file = original_download

    def test_desktop_save_video_file_falls_back_to_ytdlp_for_youtube(self):
        collector = load_collector()
        original_extract = collector.extract_from_page
        original_download_ytdlp = collector.download_media_with_ytdlp
        try:
            collector.extract_from_page = lambda url, cfg: {
                "platform": "YouTube",
                "content_type": "video",
                "title": "YouTube视频",
                "media_url": "",
            }

            def fake_ytdlp(url, cfg):
                tmp_dir = Path(tempfile.mkdtemp(prefix="video-download-test-"))
                path = tmp_dir / "media.mp4"
                path.write_bytes(b"yt-video")
                return path

            collector.download_media_with_ytdlp = fake_ytdlp
            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "desktop.sqlite3"
                downloads = Path(tmp) / "downloads"
                collector.desktop_db_init(db)
                table = collector.desktop_list_tables(db)[0]
                saved = collector.desktop_save_item(
                    db,
                    table["id"],
                    {
                        "platform": "YouTube",
                        "source_url": "https://www.youtube.com/watch?v=abc123xyz90",
                        "source_type": "single",
                        "title": "YouTube视频",
                        "status": "成功",
                        "raw_metadata_json": "{}",
                    },
                )

                result = collector.desktop_save_video_file(db, saved["id"], {}, downloads)

                self.assertEqual(Path(result["path"]).read_bytes(), b"yt-video")
                self.assertEqual(result["method"], "yt-dlp")
        finally:
            collector.extract_from_page = original_extract
            collector.download_media_with_ytdlp = original_download_ytdlp

    def test_desktop_save_video_file_refreshes_expired_media_url(self):
        collector = load_collector()
        original_download = getattr(collector, "download_media_url_to_file", None)
        original_extract = collector.extract_from_page
        try:
            calls = []

            def fake_download(url, cfg, platform, target):
                calls.append(url)
                if "expired" in url:
                    raise RuntimeError("HTTP Error 403: Forbidden")
                target.write_bytes(b"fresh-video")
                return {"content_type": "video/mp4", "bytes": len(b"fresh-video")}

            collector.download_media_url_to_file = fake_download
            collector.extract_from_page = lambda url, cfg: {
                "platform": "抖音",
                "content_type": "video",
                "title": "刷新后标题",
                "media_url": "https://v5.365yg.com/fresh?mime_type=video_mp4",
            }
            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "desktop.sqlite3"
                downloads = Path(tmp) / "downloads"
                collector.desktop_db_init(db)
                table = collector.desktop_list_tables(db)[0]
                saved = collector.desktop_save_item(
                    db,
                    table["id"],
                    {
                        "platform": "抖音",
                        "source_url": "https://www.douyin.com/video/1",
                        "source_type": "single",
                        "title": "旧标题",
                        "status": "成功",
                        "raw_metadata_json": json.dumps(
                            {"platform": "抖音", "title": "旧标题", "media_url": "https://v5.365yg.com/expired?mime_type=video_mp4"},
                            ensure_ascii=False,
                        ),
                    },
                )

                result = collector.desktop_save_video_file(db, saved["id"], {}, downloads)

                self.assertEqual(calls, [
                    "https://v5.365yg.com/expired?mime_type=video_mp4",
                    "https://v5.365yg.com/fresh?mime_type=video_mp4",
                ])
                self.assertEqual(Path(result["path"]).read_bytes(), b"fresh-video")
                self.assertEqual(result["method"], "media_url")
        finally:
            if original_download is not None:
                collector.download_media_url_to_file = original_download
            collector.extract_from_page = original_extract

    def test_desktop_queue_add_and_process_pending_mobile_tasks(self):
        collector = load_collector()
        original_scrape = collector.desktop_scrape_single_url
        try:
            processed = []

            def fake_scrape(db_path, table_id, url, cfg, platform_hint="", source_type="single", transcribe=True):
                processed.append((url, platform_hint, source_type, transcribe))
                return collector.desktop_save_item(
                    db_path,
                    table_id,
                    {
                        "platform": platform_hint or "抖音",
                        "source_url": url,
                        "source_type": source_type,
                        "title": "队列完成",
                        "status": "成功",
                        "raw_metadata_json": "{}",
                    },
                )

            collector.desktop_scrape_single_url = fake_scrape
            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "desktop.sqlite3"
                collector.desktop_db_init(db)
                table = collector.desktop_list_tables(db)[0]

                added = collector.desktop_queue_add_urls(db, table["id"], ["https://www.douyin.com/video/1"], "抖音")
                self.assertEqual(added["count"], 1)
                self.assertEqual(collector.desktop_queue_pending_items(db)[0]["status"], "待采集")

                result = collector.desktop_queue_process_once(db, {}, limit=5)

                self.assertEqual(result["count"], 1)
                self.assertEqual(result["remaining"], 0)
                self.assertEqual(processed, [("https://www.douyin.com/video/1", "抖音", "single", True)])
                self.assertEqual(collector.desktop_list_items(db, table["id"])[0]["title"], "队列完成")
        finally:
            collector.desktop_scrape_single_url = original_scrape

    def test_desktop_engine_status_reports_pending_queue(self):
        collector = load_collector()
        original_cdp = collector.cdp_browser_available
        original_launchctl = collector.launchctl_label_running
        original_pmset = collector.pmset_power_summary
        try:
            collector.cdp_browser_available = lambda cfg: True
            collector.launchctl_label_running = lambda label: True
            collector.pmset_power_summary = lambda: {"source": "AC Power"}
            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "desktop.sqlite3"
                collector.desktop_db_init(db)
                table = collector.desktop_list_tables(db)[0]
                collector.desktop_queue_add_urls(db, table["id"], ["https://www.douyin.com/video/1"], "抖音")

                status = collector.desktop_engine_status(db, {"browser_fallback": {"enabled": True}})

                self.assertEqual(status["status"], "有待采集任务")
                self.assertEqual(status["pending_count"], 1)
                self.assertEqual(status["cdp_browser"], "运行中")
                self.assertEqual(status["power_source"], "AC Power")
        finally:
            collector.cdp_browser_available = original_cdp
            collector.launchctl_label_running = original_launchctl
            collector.pmset_power_summary = original_pmset

    def test_desktop_export_formats_include_saved_items(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]
            collector.desktop_save_item(
                db,
                table["id"],
                {
                    "platform": "抖音",
                    "source_url": "https://www.douyin.com/video/export-me",
                    "source_type": "single",
                    "title": "导出标题",
                    "caption": "导出逐字稿",
                    "status": "成功",
                    "raw_metadata_json": "{}",
                },
            )

            csv_data = collector.desktop_export_bytes(db, table["id"], "csv")[0].decode("utf-8")
            md_data = collector.desktop_export_bytes(db, table["id"], "markdown")[0].decode("utf-8")
            json_data = collector.desktop_export_bytes(db, table["id"], "json")[0].decode("utf-8")

            self.assertIn("导出标题", csv_data)
            self.assertIn("### 文案/逐字稿", md_data)
            self.assertIn('"title": "导出标题"', json_data)

    def test_desktop_save_export_file_writes_selected_path(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]
            target = Path(tmp) / "导出结果"

            result = collector.desktop_save_export_file(db, table["id"], "markdown", target)

            saved = Path(result["path"])
            self.assertEqual(saved.suffix, ".md")
            self.assertTrue(saved.exists())
            self.assertIn("CHEN 内容采集表", saved.read_text(encoding="utf-8"))

    def test_desktop_max_daily_export_is_ready_for_max_review(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]
            collector.desktop_rename_table(db, table["id"], "日报素材表")
            collector.desktop_save_item(
                db,
                table["id"],
                {
                    "platform": "抖音",
                    "source_url": "https://www.douyin.com/video/max-daily",
                    "source_type": "single",
                    "title": "Max要看的标题",
                    "caption": "这是一段可以交给 Max 判断切入点的逐字稿。",
                    "likes": 1200,
                    "comments": 34,
                    "shares": 56,
                    "status": "成功",
                    "error": "",
                    "raw_metadata_json": "{}",
                },
            )
            collector.desktop_save_item(
                db,
                table["id"],
                {
                    "platform": "小红书",
                    "source_url": "https://www.xiaohongshu.com/explore/wait",
                    "source_type": "single",
                    "title": "待处理标题",
                    "status": "需登录",
                    "error": "需要登录后重试",
                    "raw_metadata_json": "{}",
                },
            )

            data, content_type, ext = collector.desktop_export_bytes(db, table["id"], "max-daily")
            text = data.decode("utf-8")

            self.assertEqual(content_type, "text/markdown; charset=utf-8")
            self.assertEqual(ext, ".md")
            self.assertIn("# MAX 日报", text)
            self.assertIn("采集表：日报素材表", text)
            self.assertIn("## 给 Max 的阅读顺序", text)
            self.assertIn("Max要看的标题", text)
            self.assertIn("这是一段可以交给 Max 判断切入点的逐字稿。", text)
            self.assertIn("## 需要补救的素材", text)
            self.assertIn("需要登录后重试", text)

    def test_desktop_max_daily_save_uses_readable_filename(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]
            target = Path(tmp) / "今天给Max"

            result = collector.desktop_save_export_file(db, table["id"], "max-daily", target)

            saved = Path(result["path"])
            self.assertEqual(saved.suffix, ".md")
            self.assertTrue(saved.exists())
            self.assertIn("MAX 日报", saved.read_text(encoding="utf-8"))

    def test_desktop_daily_report_generates_editable_web_report(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]
            collector.desktop_rename_table(db, table["id"], "外部情报素材")
            collector.desktop_save_item(
                db,
                table["id"],
                {
                    "platform": "抖音",
                    "source_url": "https://www.douyin.com/video/oral-daily",
                    "source_type": "single",
                    "title": "外部情报标题",
                    "caption": "这是一条适合 Max 口喷的外部情报。",
                    "likes": 30,
                    "comments": 4,
                    "shares": 2,
                    "status": "成功",
                    "raw_metadata_json": "{}",
                },
            )

            report = collector.desktop_get_daily_report(db, table["id"])

            self.assertEqual(report["title"], "外部情报口喷日报")
            self.assertEqual(report["table_name"], "外部情报素材")
            self.assertIn("外部情报标题", report["body"])
            self.assertIn("这是一条适合 Max 口喷的外部情报。", report["body"])
            self.assertEqual(report["url"], f"/daily?table_id={table['id']}")

    def test_desktop_daily_report_saves_max_edits(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]

            saved = collector.desktop_save_daily_report(
                db,
                table["id"],
                "Max改过的标题",
                "Max 在网页里编辑后的正文。",
            )
            loaded = collector.desktop_get_daily_report(db, table["id"])

            self.assertEqual(saved["title"], "Max改过的标题")
            self.assertEqual(loaded["title"], "Max改过的标题")
            self.assertEqual(loaded["body"], "Max 在网页里编辑后的正文。")
            self.assertTrue(loaded["updated_at"])

    def test_desktop_daily_columns_and_card_workflow_are_restored(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]
            saved = collector.desktop_save_item(
                db,
                table["id"],
                {
                    "platform": "抖音",
                    "source_url": "https://www.douyin.com/video/daily-card",
                    "source_type": "single",
                    "title": "外部情报标题",
                    "caption": "外部情报逐字稿",
                    "status": "成功",
                    "raw_metadata_json": "{}",
                },
            )

            added = collector.desktop_daily_add_items(db, table["id"], [saved["id"]], "2026-07-09")
            cards = collector.desktop_daily_cards(db, table["id"], "2026-07-09")

            self.assertEqual(added["count"], 1)
            self.assertEqual(cards[0]["id"], saved["id"])
            self.assertEqual(cards[0]["daily_selected"], 1)
            self.assertEqual(cards[0]["daily_date"], "2026-07-09")
            self.assertIn("外部情报标题", cards[0]["max_daily_card"])
            self.assertIn("外部情报逐字稿", cards[0]["max_daily_card"])

            updated = collector.desktop_daily_update_card(
                db,
                saved["id"],
                {"max_daily_card": "Max 改过的口喷卡片", "max_feedback": "已看"},
            )
            self.assertEqual(updated["max_daily_card"], "Max 改过的口喷卡片")
            self.assertEqual(updated["max_feedback"], "已看")

            removed = collector.desktop_daily_remove_items(db, [saved["id"]])
            self.assertEqual(removed["count"], 1)
            self.assertEqual(collector.desktop_daily_cards(db, table["id"], "2026-07-09"), [])

    def test_desktop_html_restores_daily_buttons_and_card_table_names(self):
        collector = load_collector()

        self.assertIn("录入日报", collector.DESKTOP_APP_HTML)
        self.assertIn("删除日报", collector.DESKTOP_APP_HTML)
        self.assertIn("外部情报口喷卡片", collector.DESKTOP_DAILY_HTML)
        self.assertIn("/api/daily", collector.DESKTOP_DAILY_HTML)

    def test_desktop_daily_page_restores_rich_internal_workbench(self):
        collector = load_collector()

        html = collector.DESKTOP_DAILY_HTML
        self.assertIn("MAX DAILY INTEL", html)
        self.assertIn("外部情报口喷日报", html)
        self.assertIn('aria-label="日报视图工具栏"', html)
        self.assertIn("视频专注", html)
        self.assertIn("文稿阅读", html)
        self.assertIn("表格总览", html)
        self.assertIn("字段配置", html)
        self.assertIn("调整空间", html)
        self.assertIn("spaceRange", html)
        self.assertIn("type=\"range\"", html)
        self.assertIn("@keyframes dailyFloat", html)
        self.assertIn("particleCanvas", html)
        self.assertIn("startParticles", html)
        self.assertIn("timelineDrawer", html)
        self.assertIn("renderTimeline", html)
        self.assertIn("bindSpringSlider", html)
        self.assertIn("cubic-bezier(.18,.89,.32,1.28)", html)
        self.assertIn("MAX口喷卡片", html)
        self.assertIn("返回采集助手", html)

    def test_desktop_daily_page_plays_downloaded_video(self):
        collector = load_collector()

        html = collector.DESKTOP_DAILY_HTML
        self.assertIn("<video", html)
        self.assertIn("/api/daily/video?item_id=", html)
        self.assertIn("controls playsinline", html)
        self.assertIn('preload="metadata"', html)
        self.assertIn("poster=", html)
        self.assertIn("handleDailyVideoError", html)
        self.assertIn("这条素材尚未下载视频", html)
        self.assertIn("本地视频文件不存在或无法播放，请重新下载", html)
        self.assertIn("mode==='text'?text:dailyVideoHtml(current)", html)

    def test_desktop_daily_video_path_requires_linked_existing_file(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]
            saved = collector.desktop_save_item(
                db,
                table["id"],
                {
                    "platform": "抖音",
                    "source_url": "https://www.douyin.com/video/local-daily",
                    "source_type": "single",
                    "title": "本地日报视频",
                    "status": "成功",
                    "raw_metadata_json": "{}",
                },
            )
            video = root / "clip.mp4"
            video.write_bytes(b"0123456789")
            with collector.desktop_connect(db) as conn:
                conn.execute(
                    "UPDATE collected_items SET video_path = ? WHERE id = ?",
                    (str(video), saved["id"]),
                )

            self.assertEqual(
                collector.desktop_daily_video_path(db, saved["id"]),
                video,
            )

            video.unlink()
            with self.assertRaisesRegex(FileNotFoundError, "本地视频文件不存在"):
                collector.desktop_daily_video_path(db, saved["id"])

    def test_desktop_daily_video_range_response(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.mp4"
            path.write_bytes(b"0123456789")

            handler = FakeBinaryHandler()
            collector.desktop_video_response(handler, path, "bytes=2-5")

            self.assertEqual(handler.status, 206)
            self.assertEqual(handler.headers["Content-Type"], "video/mp4")
            self.assertEqual(handler.headers["Accept-Ranges"], "bytes")
            self.assertEqual(handler.headers["Content-Range"], "bytes 2-5/10")
            self.assertEqual(handler.headers["Content-Length"], "4")
            self.assertEqual(handler.wfile.getvalue(), b"2345")

            suffix = FakeBinaryHandler()
            collector.desktop_video_response(suffix, path, "bytes=-4")
            self.assertEqual(suffix.status, 206)
            self.assertEqual(suffix.headers["Content-Range"], "bytes 6-9/10")
            self.assertEqual(suffix.wfile.getvalue(), b"6789")

            complete = FakeBinaryHandler()
            collector.desktop_video_response(complete, path)
            self.assertEqual(complete.status, 200)
            self.assertEqual(complete.headers["Content-Length"], "10")
            self.assertEqual(complete.wfile.getvalue(), b"0123456789")

    def test_desktop_daily_video_rejects_invalid_range(self):
        collector = load_collector()

        for value in ("items=0-1", "bytes=", "bytes=8-2", "bytes=10-", "bytes=0-1,4-5"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    collector.desktop_parse_byte_range(value, 10)

    def test_desktop_daily_page_has_cloud_publish_controls(self):
        collector = load_collector()
        html = collector.DESKTOP_DAILY_HTML
        copy_function = html.split("async function copyCloudDailyLink()", 1)[1].split(
            "async function loadDaily", 1
        )[0]

        self.assertIn("发布到云端", html)
        self.assertIn("打开云端日报", html)
        self.assertIn("复制链接", html)
        cloud_endpoints = set(re.findall(r"/api/cloud/[A-Za-z0-9_/-]+", html))
        self.assertEqual(
            {
                "/api/cloud/publish",
                "/api/cloud/publish/status",
                "/api/cloud/latest",
            },
            cloud_endpoints,
        )
        self.assertIn('aria-live="polite"', html)
        self.assertIn("window.open(latestCloudReport.reportUrl", html)
        self.assertIn(",'_blank','noopener')", html)
        self.assertIn("navigator.clipboard.writeText", html)
        self.assertIn("shareUrl:report.shareUrl", html)
        self.assertIn("const shareUrl=latestCloudReport.shareUrl", copy_function)
        self.assertIn("navigator.clipboard.writeText(shareUrl)", copy_function)
        self.assertNotIn("navigator.clipboard.writeText(reportUrl)", copy_function)
        self.assertIn("固定免登录链接已复制", copy_function)
        self.assertIn('id="publishCloudButton" onclick="publishCloudDaily()" disabled', html)
        self.assertIn("initializeCloudPublish()", html)
        self.assertIn(
            "startParticles();initializeCloudPublish();loadDaily()", html
        )
        self.assertIn(
            "recoverCloudPublish('/api/cloud/publish/status')", html
        )
        self.assertIn("let cloudPublishGeneration=0", html)
        self.assertIn(
            "function isCurrentCloudPublishGeneration(generation){return generation===cloudPublishGeneration}",
            html,
        )
        self.assertIn("if(!isCurrentCloudPublishGeneration(generation))return", html)
        self.assertIn("let cloudPublishPolling=false", html)
        self.assertIn("let cloudPublishPollQueued=false", html)
        self.assertIn("cloudPublishPolling=true", html)
        self.assertIn("cloudPublishPolling=false", html)
        self.assertIn("if(cloudPublishState==='running')pollCloudPublish(cloudPublishGeneration)", html)
        self.assertIn("if(state.state==='running')scheduleCloudPublishPoll(generation);else clearCloudPublishTimer()", html)
        self.assertIn("rememberCloudReport(state)", html)
        self.assertIn("status.textContent=state.message", html)
        self.assertIn("function copyCloudDailyLinkFallback(reportUrl)", html)
        self.assertIn("document.createElement('textarea')", html)
        self.assertIn("textarea.setAttribute('readonly','')", html)
        self.assertIn("document.execCommand('copy')", html)
        self.assertIn("document.body.removeChild(textarea)", html)
        self.assertIn("const generation=cloudPublishGeneration", copy_function)
        self.assertIn(
            "if(!isCurrentCloudPublishGeneration(generation)||cloudPublishState==='running')return",
            copy_function,
        )

    def test_desktop_daily_actions_live_on_each_row_with_manual_oral_card_column(self):
        collector = load_collector()

        self.assertIn("'口喷日报'", collector.DESKTOP_APP_HTML)
        self.assertIn("'max_daily_card'", collector.DESKTOP_APP_HTML)
        self.assertIn("addRowToDaily", collector.DESKTOP_APP_HTML)
        self.assertIn("removeRowFromDaily", collector.DESKTOP_APP_HTML)
        self.assertNotIn('onclick="addSelectedToDaily()">录入日报', collector.DESKTOP_APP_HTML)
        self.assertNotIn('onclick="removeSelectedFromDaily()">删除日报', collector.DESKTOP_APP_HTML)

    def test_desktop_daily_entry_is_prominent_and_opens_without_selected_table(self):
        collector = load_collector()
        html = collector.DESKTOP_APP_HTML

        self.assertIn("打开外部情报口喷日报", html)
        self.assertIn("daily-hero-btn", html)
        self.assertIn("const path=state.tableId?'/daily?table_id='", html)
        self.assertIn(":'/daily'", html)
        self.assertIn("window.location.href=path", html)
        open_daily = html.split("function openDailyPage()", 1)[1].split("async function openCoverOriginal", 1)[0]
        self.assertNotIn("请先选择或新建采集表", open_daily)

    def test_desktop_manual_oral_daily_card_can_be_saved_from_table_cell(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]
            saved = collector.desktop_save_item(
                db,
                table["id"],
                {
                    "platform": "抖音",
                    "source_url": "https://www.douyin.com/video/manual-card",
                    "source_type": "single",
                    "title": "手填卡片素材",
                    "status": "成功",
                    "raw_metadata_json": "{}",
                },
            )

            updated = collector.desktop_update_item(db, saved["id"], {"max_daily_card": "我手动填的口喷日报"})

            self.assertEqual(updated["max_daily_card"], "我手动填的口喷日报")
            self.assertEqual(collector.desktop_get_item(db, saved["id"])["max_daily_card"], "我手动填的口喷日报")

    def test_desktop_daily_summary_defaults_to_latest_daily_date(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]
            first = collector.desktop_save_item(db, table["id"], {"source_url": "https://a.example/1", "title": "旧日报", "raw_metadata_json": "{}"})
            second = collector.desktop_save_item(db, table["id"], {"source_url": "https://a.example/2", "title": "新日报", "raw_metadata_json": "{}"})
            collector.desktop_daily_add_items(db, table["id"], [first["id"]], "2026-07-08")
            collector.desktop_daily_add_items(db, table["id"], [second["id"]], "2026-07-10")

            summary = collector.desktop_daily_summary(db, table["id"], "")

            self.assertEqual(summary["date"], "2026-07-10")
            self.assertEqual(summary["count"], 1)
            self.assertEqual(summary["items"][0]["title"], "新日报")

    def test_douyin_profile_entries_extract_unique_video_links(self):
        collector = load_collector()

        links = collector.douyin_profile_entries_to_links(
            [
                {"href": "https://www.douyin.com/video/741147029037124910", "text": "第1集 标题"},
                {"href": "https://www.douyin.com/?modal_id=741147029037124910", "text": "重复链接"},
                {"href": "/video/741147029037124911", "text": "第2集 标题"},
                {"href": "https://www.douyin.com/share/video/741147029037124912?aweme_id=741147029037124912", "text": ""},
                {"href": "https://www.douyin.com/user/MS4wLjABAAAA", "text": "主页"},
            ],
            "https://www.douyin.com/user/MS4wLjABAAAA",
        )

        self.assertEqual(
            [item["url"] for item in links],
            [
                "https://www.douyin.com/video/741147029037124910",
                "https://www.douyin.com/video/741147029037124911",
                "https://www.douyin.com/video/741147029037124912",
            ],
        )
        self.assertEqual(links[0]["title"], "第1集 标题")

    def test_xiaohongshu_profile_entries_extract_unique_note_links(self):
        collector = load_collector()

        links = collector.xiaohongshu_profile_entries_to_links(
            [
                {"href": "https://www.xiaohongshu.com/explore/665aabbccddeeff001122334", "text": "小红书视频笔记 标题"},
                {"href": "/explore/665aabbccddeeff001122334?xsec_token=abc", "text": "重复笔记"},
                {"href": "https://www.xiaohongshu.com/discovery/item/665aabbccddeeff001122335", "text": "图文笔记"},
                {"href": "https://www.xiaohongshu.com/user/profile/abc", "text": "主页"},
            ],
            "https://www.xiaohongshu.com/user/profile/abc",
        )

        self.assertEqual(
            [item["url"] for item in links],
            [
                "https://www.xiaohongshu.com/explore/665aabbccddeeff001122334",
                "https://www.xiaohongshu.com/explore/665aabbccddeeff001122335",
            ],
        )
        self.assertEqual(links[0]["title"], "小红书视频笔记 标题")

    def test_bilibili_profile_entries_extract_unique_video_links(self):
        collector = load_collector()

        links = collector.bilibili_profile_entries_to_links(
            [
                {"href": "https://www.bilibili.com/video/BV1xx411c7mD/", "text": "B站视频标题"},
                {"href": "/video/BV1xx411c7mD/?spm_id_from=333", "text": "重复"},
                {"href": "https://www.bilibili.com/video/av123456", "text": "av 视频"},
                {"href": "https://space.bilibili.com/123/video", "text": "主页"},
            ],
            "https://space.bilibili.com/123/video",
        )

        self.assertEqual(
            [item["url"] for item in links],
            [
                "https://www.bilibili.com/video/BV1xx411c7mD",
                "https://www.bilibili.com/video/av123456",
            ],
        )
        self.assertEqual(links[0]["title"], "B站视频标题")

    def test_shipinhao_profile_entries_extract_unique_visible_work_links(self):
        collector = load_collector()

        links = collector.shipinhao_profile_entries_to_links(
            [
                {
                    "href": "https://channels.weixin.qq.com/platform/post/1234567890?scene=home",
                    "text": "视频号作品标题",
                },
                {
                    "href": "/platform/post/1234567890?scene=home",
                    "text": "重复作品",
                },
                {
                    "href": "https://channels.weixin.qq.com/web/pages/feed?exportkey=abc&feed_id=feed123",
                    "text": "分享页作品",
                    "cover_url": "//res.wx.qq.com/cover.jpg",
                },
                {
                    "href": "https://channels.weixin.qq.com/platform/profile/abc",
                    "text": "主页",
                },
                {
                    "href": "https://support.weixin.qq.com/",
                    "text": "帮助",
                },
            ],
            "https://channels.weixin.qq.com/platform/profile/abc",
        )

        self.assertEqual(
            [item["url"] for item in links],
            [
                "https://channels.weixin.qq.com/platform/post/1234567890?scene=home",
                "https://channels.weixin.qq.com/web/pages/feed?exportkey=abc&feed_id=feed123",
            ],
        )
        self.assertEqual(links[0]["title"], "视频号作品标题")
        self.assertEqual(links[1]["cover_url"], "https://res.wx.qq.com/cover.jpg")

    def test_youtube_profile_entries_extract_unique_video_and_shorts_links(self):
        collector = load_collector()

        links = collector.youtube_profile_entries_to_links(
            [
                {"href": "https://www.youtube.com/watch?v=abc123xyz90&list=foo", "text": "YouTube 视频标题"},
                {"href": "/watch?v=abc123xyz90", "text": "重复"},
                {"href": "https://www.youtube.com/shorts/shorts12345", "text": "Shorts 标题"},
                {"href": "https://www.youtube.com/@creator", "text": "主页"},
            ],
            "https://www.youtube.com/@creator/videos",
        )

        self.assertEqual(
            [item["url"] for item in links],
            [
                "https://www.youtube.com/watch?v=abc123xyz90",
                "https://www.youtube.com/shorts/shorts12345",
            ],
        )
        self.assertEqual(links[0]["title"], "YouTube 视频标题")

    def test_instagram_profile_entries_extract_unique_post_and_reel_links(self):
        collector = load_collector()

        links = collector.instagram_profile_entries_to_links(
            [
                {"href": "https://www.instagram.com/reel/ABCdef123/?utm_source=ig_web_copy_link", "text": "Reel 标题"},
                {"href": "/reel/ABCdef123/", "text": "重复"},
                {"href": "https://www.instagram.com/p/POSTid456/", "text": "图文帖"},
                {"href": "https://www.instagram.com/stories/user/123", "text": "快拍"},
            ],
            "https://www.instagram.com/creator/",
        )

        self.assertEqual(
            [item["url"] for item in links],
            [
                "https://www.instagram.com/reel/ABCdef123/",
                "https://www.instagram.com/p/POSTid456/",
            ],
        )
        self.assertEqual(links[0]["title"], "Reel 标题")

    def test_desktop_save_profile_candidate_does_not_deep_scrape(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]

            saved = collector.desktop_save_profile_candidate(
                db,
                table["id"],
                {
                    "url": "https://www.douyin.com/video/741147029037124910",
                    "title": "主页预览标题",
                    "cover_url": "https://example.com/cover.jpg",
                },
                "https://www.douyin.com/user/MS4wLjABAAAA",
            )

            self.assertEqual(saved["status"], "候选")
            self.assertEqual(saved["title"], "主页预览标题")
            self.assertEqual(saved["duration"], "")
            self.assertIn("勾选", saved["error"])

    def test_desktop_save_profile_candidate_uses_target_platform(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]

            saved = collector.desktop_save_profile_candidate(
                db,
                table["id"],
                {"url": "https://www.bilibili.com/video/BV1xx411c7mD", "title": "B站候选"},
                "https://space.bilibili.com/123/video",
                platform="B站",
            )

            self.assertEqual(saved["platform"], "B站")
            self.assertEqual(saved["status"], "候选")

    def test_desktop_profile_session_accepts_added_platforms_without_starting_worker(self):
        collector = load_collector()
        started = []

        class FakeThread:
            def __init__(self, target, args, daemon):
                self.target = target
                self.args = args
                self.daemon = daemon

            def start(self):
                started.append((self.target.__name__, self.args[5]))

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]
            original_thread = collector.threading.Thread
            xhs = bz = shipin = yt = ig = None
            try:
                collector.threading.Thread = FakeThread
                xhs = collector.desktop_start_profile_session(
                    db,
                    table["id"],
                    "https://www.xiaohongshu.com/user/profile/abc",
                    {},
                    "小红书",
                )
                bz = collector.desktop_start_profile_session(
                    db,
                    table["id"],
                    "https://space.bilibili.com/123/video",
                    {},
                    "B站",
                )
                shipin = collector.desktop_start_profile_session(
                    db,
                    table["id"],
                    "https://channels.weixin.qq.com/platform/profile/abc",
                    {},
                    "视频号",
                )
                yt = collector.desktop_start_profile_session(
                    db,
                    table["id"],
                    "https://www.youtube.com/@creator/videos",
                    {},
                    "YouTube",
                )
                ig = collector.desktop_start_profile_session(
                    db,
                    table["id"],
                    "https://www.instagram.com/creator/",
                    {},
                    "Instagram",
                )
            finally:
                collector.threading.Thread = original_thread
                for session in (xhs, bz, shipin, yt, ig):
                    if session:
                        collector.DESKTOP_PROFILE_SESSIONS.pop(session["session_id"], None)

            self.assertEqual([item[1] for item in started], ["小红书", "B站", "视频号", "YouTube", "Instagram"])

    def test_desktop_app_html_enables_all_added_profile_modes(self):
        collector = load_collector()

        self.assertNotIn("主页批量任务（规划中）", collector.DESKTOP_APP_HTML)
        self.assertNotIn("第一版主页批量采集先支持抖音主页", collector.DESKTOP_APP_HTML)
        self.assertIn("小红书主页候选预览", collector.DESKTOP_APP_HTML)
        self.assertIn("B站主页候选预览", collector.DESKTOP_APP_HTML)
        self.assertIn("视频号主页候选预览", collector.DESKTOP_APP_HTML)
        self.assertIn("YouTube频道候选预览", collector.DESKTOP_APP_HTML)
        self.assertIn("Instagram主页候选预览", collector.DESKTOP_APP_HTML)

    def test_desktop_app_selection_tools_apply_to_all_current_rows(self):
        collector = load_collector()

        self.assertIn("全选当前", collector.DESKTOP_APP_HTML)
        self.assertNotIn("全选候选", collector.DESKTOP_APP_HTML)
        self.assertIn("function selectableItems()", collector.DESKTOP_APP_HTML)
        self.assertIn("state.items.filter(i=>i.source_url", collector.DESKTOP_APP_HTML)
        self.assertIn("row-selected", collector.DESKTOP_APP_HTML)
        self.assertIn("setTableFeedback", collector.DESKTOP_APP_HTML)

    def test_desktop_app_table_tools_match_bitable_workflow(self):
        collector = load_collector()

        for label in ("字段配置", "筛选", "排序", "行高", "重置视图"):
            self.assertIn(label, collector.DESKTOP_APP_HTML)
        self.assertIn("function visibleColumns()", collector.DESKTOP_APP_HTML)
        self.assertIn("function filteredItems()", collector.DESKTOP_APP_HTML)
        self.assertIn("function sortedItems()", collector.DESKTOP_APP_HTML)
        self.assertIn("function setRowDensity", collector.DESKTOP_APP_HTML)
        self.assertIn("function saveTablePrefs()", collector.DESKTOP_APP_HTML)
        self.assertIn("localStorage.setItem('chen.tablePrefs'", collector.DESKTOP_APP_HTML)
        self.assertIn("selected-count", collector.DESKTOP_APP_HTML)
        self.assertIn("table-toolbar-panel", collector.DESKTOP_APP_HTML)

    def test_desktop_app_table_toolbar_has_clear_bitable_layout(self):
        collector = load_collector()

        self.assertIn("results-panel", collector.DESKTOP_APP_HTML)
        self.assertIn("table-shell-head", collector.DESKTOP_APP_HTML)
        self.assertIn("table-primary-actions", collector.DESKTOP_APP_HTML)
        self.assertIn("bitable-toolbar", collector.DESKTOP_APP_HTML)
        self.assertIn("toolbar-group", collector.DESKTOP_APP_HTML)
        self.assertIn("toolbar-spacer", collector.DESKTOP_APP_HTML)

    def test_shipinhao_html_meta_keeps_video_and_status_details(self):
        collector = load_collector()
        html = """
        <html><head>
          <meta property="og:title" content="视频号作品标题">
          <meta property="og:image" content="https://res.wx.qq.com/cover.jpg">
          <meta property="og:video" content="https://finder.video.qq.com/video.mp4">
        </head><body>
          <script>window.__INITIAL_STATE__={"likeCount": 88, "commentCount": 6, "shareCount": 3, "duration": 92, "publishTime": 1780000000};</script>
        </body></html>
        """

        meta = collector.extract_from_html(
            "https://channels.weixin.qq.com/platform/post/123",
            html,
            "https://channels.weixin.qq.com/platform/post/123",
            "视频号",
        )

        self.assertEqual(meta["platform"], "视频号")
        self.assertEqual(meta["content_type"], "video")
        self.assertEqual(meta["title"], "视频号作品标题")
        self.assertEqual(meta["cover_url"], "https://res.wx.qq.com/cover.jpg")
        self.assertEqual(meta["media_url"], "https://finder.video.qq.com/video.mp4")
        self.assertEqual(meta["duration"], "01:32")
        self.assertEqual(meta["likes"], 88)
        self.assertEqual(meta["comments"], 6)
        self.assertEqual(meta["shares"], 3)

    def test_ytdlp_meta_marks_youtube_and_instagram_as_video(self):
        collector = load_collector()
        payload = {
            "webpage_url": "https://www.youtube.com/watch?v=abc123xyz90",
            "title": "YouTube 标题",
            "thumbnail": "https://i.ytimg.com/vi/abc/hqdefault.jpg",
            "duration": 61,
            "like_count": 10,
            "comment_count": 2,
            "timestamp": 1780000000,
            "url": "https://example.com/video.mp4",
        }

        class Result:
            returncode = 0
            stdout = __import__("json").dumps(payload)
            stderr = ""

        original_ytdlp_path = collector.ytdlp_path
        original_run = collector.subprocess.run
        try:
            collector.ytdlp_path = lambda: "/usr/local/bin/yt-dlp"
            collector.subprocess.run = lambda *args, **kwargs: Result()

            meta = collector.extract_with_ytdlp("https://www.youtube.com/watch?v=abc123xyz90", {"yt_dlp": {"enabled": True}})
        finally:
            collector.ytdlp_path = original_ytdlp_path
            collector.subprocess.run = original_run

        self.assertEqual(meta["platform"], "YouTube")
        self.assertEqual(meta["content_type"], "video")
        self.assertEqual(meta["title"], "YouTube 标题")
        self.assertEqual(meta["duration"], "01:01")
        self.assertEqual(meta["media_url"], "https://example.com/video.mp4")

    def test_ytdlp_meta_prefers_youtube_subtitles_as_caption(self):
        collector = load_collector()
        payload = {
            "webpage_url": "https://www.youtube.com/watch?v=abc123xyz90",
            "title": "YouTube 标题",
            "thumbnail": "https://i.ytimg.com/vi/abc/hqdefault.jpg",
            "duration": 61,
            "url": "https://example.com/video.mp4",
            "subtitles": {
                "en": [
                    {"ext": "json3", "url": "https://www.youtube.com/api/timedtext?v=abc&lang=en&fmt=json3"},
                ],
            },
        }

        class Result:
            returncode = 0
            stdout = __import__("json").dumps(payload)
            stderr = ""

        original_ytdlp_path = collector.ytdlp_path
        original_run = collector.subprocess.run
        original_fetch = collector.fetch_youtube_caption_url
        try:
            collector.ytdlp_path = lambda: "/usr/local/bin/yt-dlp"
            collector.subprocess.run = lambda *args, **kwargs: Result()
            collector.fetch_youtube_caption_url = lambda url, cfg: __import__("json").dumps({
                "events": [
                    {"segs": [{"utf8": "hello "}, {"utf8": "world"}]},
                    {"segs": [{"utf8": "\\n"}]},
                    {"segs": [{"utf8": "second line"}]},
                ]
            })

            meta = collector.extract_with_ytdlp("https://www.youtube.com/watch?v=abc123xyz90", {"yt_dlp": {"enabled": True}})
        finally:
            collector.ytdlp_path = original_ytdlp_path
            collector.subprocess.run = original_run
            collector.fetch_youtube_caption_url = original_fetch

        self.assertEqual(meta["caption"], "hello world second line")

    def test_youtube_caption_from_initial_player_response(self):
        collector = load_collector()
        payload = {
            "captions": {
                "playerCaptionsTracklistRenderer": {
                    "captionTracks": [
                        {
                            "languageCode": "en",
                            "baseUrl": "https://www.youtube.com/api/timedtext?v=abc&lang=en&fmt=json3",
                        }
                    ]
                }
            }
        }
        original_fetch = collector.fetch_youtube_caption_url
        try:
            collector.fetch_youtube_caption_url = lambda url, cfg: __import__("json").dumps(
                {"events": [{"segs": [{"utf8": "browser "}, {"utf8": "caption"}]}]}
            )

            caption = collector.youtube_caption_from_initial_player_response(payload, {})
        finally:
            collector.fetch_youtube_caption_url = original_fetch

        self.assertEqual(caption, "browser caption")

    def test_cdp_page_reuses_existing_youtube_tab(self):
        collector = load_collector()

        class Page:
            def __init__(self, url):
                self.url = url

        class Context:
            def __init__(self):
                self.pages = [Page("https://www.youtube.com/watch?v=abc123xyz90")]
                self.created = 0

            def new_page(self):
                self.created += 1
                page = Page("about:blank")
                self.pages.append(page)
                return page

        context = Context()
        page = collector.cdp_page_for_url(context, "https://www.youtube.com/watch?v=abc123xyz90")

        self.assertIs(page, context.pages[0])
        self.assertEqual(context.created, 0)

    def test_youtube_transcript_uses_cdp_before_playwright(self):
        collector = load_collector()
        calls = []

        collector.extract_youtube_transcript_with_cdp = lambda url, cfg: calls.append(("cdp", url)) or "CDP transcript"
        collector.launch_cdp_browser = lambda fallback_cfg, start_url="about:blank": calls.append(("launch", start_url))

        result = collector.extract_youtube_transcript_with_browser(
            "https://www.youtube.com/watch?v=pdCCk59woMQ",
            {"browser_fallback": {"enabled": True}},
        )

        self.assertEqual(result, "CDP transcript")
        self.assertEqual(calls, [("cdp", "https://www.youtube.com/watch?v=pdCCk59woMQ")])

    def test_cdp_target_for_url_reuses_existing_youtube_target(self):
        collector = load_collector()
        calls = []
        pages = [
            {"type": "page", "url": "https://www.youtube.com/watch?v=pdCCk59woMQ", "webSocketDebuggerUrl": "ws://page-1"},
            {"type": "page", "url": "https://www.youtube.com/", "webSocketDebuggerUrl": "ws://page-2"},
        ]

        collector.cdp_list_targets = lambda fallback_cfg: pages
        collector.cdp_create_target = lambda fallback_cfg, url: calls.append(url) or {
            "type": "page",
            "url": url,
            "webSocketDebuggerUrl": "ws://new-page",
        }

        target = collector.cdp_target_for_url({}, "https://youtu.be/pdCCk59woMQ?si=abc")

        self.assertEqual(target["webSocketDebuggerUrl"], "ws://page-1")
        self.assertEqual(calls, [])

    def test_youtube_cdp_transcript_timeout_returns_empty_text(self):
        collector = load_collector()
        calls = []

        collector.launch_cdp_browser = lambda fallback_cfg, url: calls.append(("launch", url))
        collector.cdp_target_for_url = lambda fallback_cfg, url: {
            "url": url,
            "webSocketDebuggerUrl": "ws://page-1",
        }

        def fake_eval(websocket_url, expression, timeout=12):
            if expression == "location.href":
                return "https://www.youtube.com/watch?v=pdCCk59woMQ"
            if expression == "document.readyState":
                return "complete"
            if expression == "document.body ? document.body.innerText : ''":
                return "女人最招架不住的3種男人思維"
            if expression == "window.ytInitialPlayerResponse || {}":
                return {}
            raise TimeoutError()

        collector.cdp_evaluate_sync = fake_eval

        result = collector.extract_youtube_transcript_with_cdp(
            "https://www.youtube.com/watch?v=pdCCk59woMQ",
            {"browser_fallback": {"enabled": True}},
        )

        self.assertEqual(result, "")

    def test_youtube_login_error_explains_empty_transcript_and_asr_download_gate(self):
        collector = load_collector()

        status, error = collector.youtube_desktop_error_status(
            "需登录",
            RuntimeError(
                "YouTube 浏览器文字稿为空，已尝试音频 ASR 兜底但失败："
                "yt-dlp 下载媒体失败：ERROR: [youtube] pdCCk59woMQ: Sign in to confirm you’re not a bot."
            ),
        )

        self.assertEqual(status, "需登录")
        self.assertIn("官方文字稿为空", error)
        self.assertIn("刷新 YouTube 登录态/Cookie", error)

    def test_desktop_youtube_transcribes_even_without_downloadable_media_url(self):
        collector = load_collector()
        db = Path(tempfile.mkdtemp(prefix="collector-test-")) / "desktop.sqlite3"
        table = collector.desktop_create_table(db, "YouTube测试", "YouTube")
        calls = []

        original_preflight = collector.youtube_preflight_check
        original_prepare = collector.youtube_prepare_browser_for_scrape
        original_extract = collector.extract_from_page
        original_transcribe = collector.transcribe_from_meta
        try:
            collector.youtube_preflight_check = lambda cfg: {"ok": True}

            def fake_prepare(url, cfg):
                raise AssertionError("desktop YouTube single scrape should not pre-open browser gate")

            collector.youtube_prepare_browser_for_scrape = fake_prepare
            collector.extract_from_page = lambda url, cfg: {
                "source_url": url,
                "final_url": url,
                "platform": "YouTube",
                "content_type": "video",
                "title": "YouTube标题",
                "caption": "",
                "cover_url": "https://i.ytimg.com/vi/abc/maxresdefault.jpg",
                "duration": "05:03",
                "likes": None,
                "comments": None,
                "shares": None,
                "published_at": "",
                "media_url": "https://www.youtube.com/embed/abc123xyz90",
            }

            def fake_transcribe(cfg, meta):
                calls.append(meta["source_url"])
                return "浏览器逐字稿"

            collector.transcribe_from_meta = fake_transcribe

            item = collector.desktop_scrape_single_url(
                db,
                table["id"],
                "https://www.youtube.com/watch?v=abc123xyz90",
                {},
                "YouTube",
                transcribe=True,
            )
        finally:
            collector.youtube_preflight_check = original_preflight
            collector.youtube_prepare_browser_for_scrape = original_prepare
            collector.extract_from_page = original_extract
            collector.transcribe_from_meta = original_transcribe

        self.assertEqual(calls, ["https://www.youtube.com/watch?v=abc123xyz90"])
        self.assertEqual(item["caption"], "浏览器逐字稿。")
        self.assertEqual(item["status"], "成功")

    def test_youtube_transcribe_reports_empty_browser_transcript_before_ytdlp_download_error(self):
        collector = load_collector()
        original_browser_transcript = collector.extract_youtube_transcript_with_browser
        original_download_ytdlp = collector.download_media_with_ytdlp
        try:
            collector.extract_youtube_transcript_with_browser = lambda url, cfg: ""
            collector.download_media_with_ytdlp = lambda url, cfg: (_ for _ in ()).throw(
                RuntimeError("yt-dlp 下载媒体失败：Sign in to confirm you’re not a bot")
            )
            meta = {
                "platform": "YouTube",
                "source_url": "https://www.youtube.com/watch?v=abc123xyz90",
                "content_type": "video",
                "media_url": "https://www.youtube.com/embed/abc123xyz90",
            }

            with self.assertRaises(RuntimeError) as got:
                collector.transcribe_from_meta({"browser_fallback": {"enabled": True}}, meta)
        finally:
            collector.extract_youtube_transcript_with_browser = original_browser_transcript
            collector.download_media_with_ytdlp = original_download_ytdlp

        self.assertIn("YouTube 浏览器文字稿为空", str(got.exception))
        self.assertIn("Sign in to confirm", str(got.exception))

    def test_youtube_falls_back_to_browser_when_ytdlp_requires_login(self):
        collector = load_collector()
        original_extract_ytdlp = collector.extract_with_ytdlp
        original_real_browser = collector.extract_with_real_browser
        original_browser_transcript = collector.extract_youtube_transcript_with_browser
        try:
            collector.extract_with_ytdlp = lambda url, cfg: (_ for _ in ()).throw(
                RuntimeError("Sign in to confirm you’re not a bot")
            )
            collector.extract_with_real_browser = lambda url, cfg: {
                "source_url": url,
                "final_url": url,
                "platform": "YouTube",
                "content_type": "video",
                "title": "浏览器标题",
                "caption": "",
                "cover_url": "",
                "duration": "05:02",
                "likes": None,
                "comments": None,
                "shares": None,
                "published_at": "",
                "media_url": "",
            }
            collector.extract_youtube_transcript_with_browser = lambda url, cfg: "浏览器文字稿"

            meta = collector.extract_from_page(
                "https://www.youtube.com/watch?v=abc123xyz90",
                {"browser_fallback": {"enabled": True}},
            )
        finally:
            collector.extract_with_ytdlp = original_extract_ytdlp
            collector.extract_with_real_browser = original_real_browser
            collector.extract_youtube_transcript_with_browser = original_browser_transcript

        self.assertEqual(meta["title"], "浏览器标题")
        self.assertEqual(meta["caption"], "浏览器文字稿")

    def test_ytdlp_args_include_proxy_and_extractor_args(self):
        collector = load_collector()
        payload = {
            "webpage_url": "https://www.youtube.com/watch?v=abc123xyz90",
            "title": "YouTube 标题",
            "duration": 61,
            "url": "https://example.com/video.mp4",
        }
        calls = []

        class Result:
            returncode = 0
            stdout = __import__("json").dumps(payload)
            stderr = ""

        original_ytdlp_path = collector.ytdlp_path
        original_run = collector.subprocess.run
        try:
            collector.ytdlp_path = lambda: "/usr/local/bin/yt-dlp"

            def fake_run(cmd, *args, **kwargs):
                calls.append(cmd)
                return Result()

            collector.subprocess.run = fake_run
            collector.extract_with_ytdlp(
                "https://www.youtube.com/watch?v=abc123xyz90",
                {
                    "yt_dlp": {
                        "enabled": True,
                        "proxy": "http://127.0.0.1:7897",
                        "extractor_args": ["youtube:player_client=mweb"],
                    }
                },
            )
        finally:
            collector.ytdlp_path = original_ytdlp_path
            collector.subprocess.run = original_run

        self.assertIn("--proxy", calls[0])
        self.assertIn("http://127.0.0.1:7897", calls[0])
        self.assertIn("--extractor-args", calls[0])
        self.assertIn("youtube:player_client=mweb", calls[0])

    def test_ytdlp_cookie_args_fall_back_to_app_directory_cookie_file(self):
        collector = load_collector()
        temp_root = Path(tempfile.mkdtemp(prefix="collector-test-"))
        original_here = collector.HERE
        try:
            (temp_root / "cookies.txt").write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
            collector.HERE = temp_root
            cmd = ["yt-dlp"]
            collector.add_ytdlp_cookie_args(cmd, {"yt_dlp": {"cookies_file": "/missing/old/cookies.txt"}})
        finally:
            collector.HERE = original_here
            shutil.rmtree(temp_root, ignore_errors=True)

        self.assertIn("--cookies", cmd)
        self.assertIn(str(temp_root / "cookies.txt"), cmd)

    def test_youtube_ytdlp_missing_raises_actionable_error(self):
        collector = load_collector()
        original_ytdlp_path = collector.ytdlp_path
        try:
            collector.ytdlp_path = lambda: None
            with self.assertRaisesRegex(RuntimeError, "yt-dlp"):
                collector.extract_with_ytdlp(
                    "https://www.youtube.com/watch?v=abc123xyz90",
                    {"yt_dlp": {"enabled": True}},
                )
        finally:
            collector.ytdlp_path = original_ytdlp_path

    def test_youtube_embed_url_is_not_treated_as_media_stream(self):
        collector = load_collector()

        self.assertEqual(collector.media_url_or_empty({"media_url": "https://www.youtube.com/embed/abc123xyz90"}), "")
        self.assertEqual(collector.media_url_or_empty({"media_url": "https://example.com/watch?v=1"}), "")
        self.assertEqual(collector.media_url_or_empty({"media_url": "https://example.com/video.mp4"}), "https://example.com/video.mp4")

    def test_youtube_and_instagram_missing_metadata_reports_quality_message(self):
        collector = load_collector()

        youtube_message = collector.metadata_quality_message({
            "platform": "YouTube",
            "content_type": "video",
            "title": "公开视频",
            "cover_url": "https://example.com/cover.jpg",
            "duration": "",
            "likes": None,
            "comments": None,
            "shares": None,
            "published_at": "",
            "media_url": "https://www.youtube.com/embed/abc123xyz90",
        })
        instagram_message = collector.metadata_quality_message({
            "platform": "Instagram",
            "content_type": "",
            "title": "Instagram",
            "cover_url": "",
            "duration": "",
            "likes": None,
            "comments": None,
            "shares": None,
            "published_at": "",
            "media_url": "",
        })

        self.assertIn("YouTube页面未暴露或未解析到", youtube_message)
        self.assertIn("视频直链", youtube_message)
        self.assertIn("Instagram页面未暴露或未解析到", instagram_message)
        self.assertIn("作品标题", instagram_message)

    def test_merge_meta_replaces_invalid_media_url_with_valid_stream(self):
        collector = load_collector()

        merged = collector.merge_meta(
            {
                "platform": "YouTube",
                "media_url": "https://www.youtube.com/embed/abc123xyz90",
                "title": "HTML标题",
            },
            {
                "media_url": "https://rr1---sn.example.googlevideo.com/videoplayback?expire=1",
                "duration": "03:33",
            },
        )

        self.assertEqual(merged["media_url"], "https://rr1---sn.example.googlevideo.com/videoplayback?expire=1")
        self.assertEqual(merged["title"], "HTML标题")

    def test_merge_meta_replaces_invalid_generic_youtube_title(self):
        collector = load_collector()

        merged = collector.merge_meta(
            {"platform": "YouTube", "title": "- YouTube"},
            {"title": "真实 YouTube 标题"},
        )

        self.assertEqual(merged["title"], "真实 YouTube 标题")

    def test_extract_from_page_uses_ytdlp_first_for_youtube_when_html_is_forbidden(self):
        collector = load_collector()
        called = {"fetch": 0}

        def forbidden_fetch(*args, **kwargs):
            called["fetch"] += 1
            raise RuntimeError("HTTP Error 403: Forbidden")

        original_fetch = collector.fetch_text
        original_ytdlp = collector.extract_with_ytdlp
        try:
            collector.fetch_text = forbidden_fetch
            collector.extract_with_ytdlp = lambda url, cfg: {
                "platform": "YouTube",
                "content_type": "video",
                "title": "yt-dlp 标题",
                "cover_url": "https://i.ytimg.com/vi/abc/sddefault.jpg",
                "duration": "05:02",
                "likes": 1,
                "comments": None,
                "shares": None,
                "published_at": "2026年01月01日00时00分00秒",
                "media_url": "https://rr1---sn.example.googlevideo.com/videoplayback",
                "caption": "",
            }

            meta = collector.extract_from_page("https://youtu.be/abc123xyz90", {"yt_dlp": {"enabled": True}})
        finally:
            collector.fetch_text = original_fetch
            collector.extract_with_ytdlp = original_ytdlp

        self.assertEqual(called["fetch"], 0)
        self.assertEqual(meta["title"], "yt-dlp 标题")

    def test_youtube_and_instagram_do_not_require_share_count_for_quality(self):
        collector = load_collector()

        youtube_message = collector.metadata_quality_message({
            "platform": "YouTube",
            "content_type": "video",
            "title": "完整视频",
            "cover_url": "https://example.com/cover.jpg",
            "duration": "03:33",
            "likes": 10,
            "comments": 2,
            "shares": None,
            "published_at": "2026年01月01日00时00分00秒",
            "media_url": "https://rr1---sn.example.googlevideo.com/videoplayback",
        })
        instagram_message = collector.metadata_quality_message({
            "platform": "Instagram",
            "content_type": "video",
            "title": "完整 Reel",
            "cover_url": "https://example.com/cover.jpg",
            "duration": "00:12",
            "likes": 10,
            "comments": 2,
            "shares": None,
            "published_at": "2026年01月01日00时00分00秒",
            "media_url": "https://scontent.cdninstagram.com/v/t50.2886-16/video",
        })

        self.assertEqual(youtube_message, "")
        self.assertEqual(instagram_message, "")

    def test_desktop_collect_selected_profile_items_only_scrapes_selected_urls(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]
            first = collector.desktop_save_item(
                db,
                table["id"],
                {
                    "platform": "抖音",
                    "source_url": "https://www.douyin.com/video/1",
                    "source_type": "profile",
                    "title": "候选1",
                    "status": "候选",
                    "raw_metadata_json": "{}",
                },
            )
            collector.desktop_save_item(
                db,
                table["id"],
                {
                    "platform": "抖音",
                    "source_url": "https://www.douyin.com/video/2",
                    "source_type": "profile",
                    "title": "候选2",
                    "status": "候选",
                    "raw_metadata_json": "{}",
                },
            )
            scraped = []

            def fake_scrape(db_path, table_id, url, cfg, platform_hint="", source_type="single", transcribe=True):
                scraped.append((url, transcribe))
                return collector.desktop_save_item(
                    db_path,
                    table_id,
                    {
                        "platform": "抖音",
                        "source_url": url,
                        "source_type": "profile",
                        "title": "已采集",
                        "cover_url": "https://example.com/cover.jpg",
                        "duration": "00:30",
                        "status": "基础信息成功",
                        "raw_metadata_json": "{}",
                    },
                )

            original = collector.desktop_scrape_single_url
            try:
                collector.desktop_scrape_single_url = fake_scrape
                result = collector.desktop_collect_selected_profile_items(
                    db,
                    table["id"],
                    [first["id"]],
                    {},
                    "抖音",
                    transcribe=False,
                )
            finally:
                collector.desktop_scrape_single_url = original

            self.assertEqual(result["processed_count"], 1)
            self.assertEqual(scraped, [("https://www.douyin.com/video/1", False)])
            statuses = {item["source_url"]: item["status"] for item in collector.desktop_list_items(db, table["id"])}
            self.assertEqual(statuses["https://www.douyin.com/video/1"], "基础信息成功")
            self.assertEqual(statuses["https://www.douyin.com/video/2"], "候选")

    def test_selected_mobile_inbox_item_writes_running_and_final_status_to_feishu(self):
        collector = load_collector()
        cfg = {
            "fields": collector.DEFAULT_FIELDS,
            "feishu": {
                "app_id": "cli_xxx",
                "app_secret": "secret",
                "app_token": "app_token",
                "table_id": "tblMain",
                "mobile_inbox_table_id": "tblMobile",
            },
        }
        updates = []
        original_scrape = collector.desktop_scrape_single_url
        original_update_record = collector.update_record
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "desktop.sqlite3"
                table = collector.desktop_ensure_mobile_inbox_table(db)
                item = collector.desktop_save_item(
                    db,
                    table["id"],
                    {
                        "platform": "抖音",
                        "source_url": "https://www.douyin.com/video/mobile-status",
                        "source_type": "mobile_inbox",
                        "status": "待扒取",
                        "raw_metadata_json": json.dumps(
                            {
                                "mobile_inbox_record_id": "recMobileStatus",
                                "mobile_remote_modified_at": "1784286000000",
                                "source": "手机收集箱",
                            },
                            ensure_ascii=False,
                        ),
                    },
                )

                def fake_scrape(db_path, table_id, url, cfg_arg, platform_hint="", source_type="single", transcribe=True):
                    return collector.desktop_save_item(
                        db_path,
                        table_id,
                        {
                            "platform": "抖音",
                            "source_url": url,
                            "source_type": source_type,
                            "title": "已采集",
                            "status": "基础信息成功",
                        },
                    )

                collector.desktop_scrape_single_url = fake_scrape
                collector.update_record = lambda table_cfg, record_id, fields: updates.append((table_cfg, record_id, fields))

                result = collector.desktop_collect_selected_profile_items(
                    db,
                    table["id"],
                    [item["id"]],
                    cfg,
                    "抖音",
                    transcribe=False,
                )

            self.assertEqual(result["processed_count"], 1)
            self.assertEqual([entry[1] for entry in updates], ["recMobileStatus", "recMobileStatus"])
            self.assertEqual(updates[0][0]["feishu"]["table_id"], "tblMobile")
            self.assertEqual(updates[0][2], {"抓取状态": "扒取中", "错误信息": ""})
            self.assertEqual(updates[-1][2], {"抓取状态": "基础信息成功", "错误信息": ""})
        finally:
            collector.desktop_scrape_single_url = original_scrape
            collector.update_record = original_update_record

    def test_selected_mobile_inbox_item_uses_its_row_platform(self):
        collector = load_collector()
        seen_platforms = []
        original_scrape = collector.desktop_scrape_single_url
        original_update_record = collector.update_record
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "desktop.sqlite3"
                table = collector.desktop_ensure_mobile_inbox_table(db)
                item = collector.desktop_save_item(
                    db,
                    table["id"],
                    {
                        "platform": "YouTube",
                        "source_url": "https://www.youtube.com/watch?v=mobile-platform",
                        "source_type": "mobile_inbox",
                        "status": "待扒取",
                        "raw_metadata_json": json.dumps(
                            {"mobile_inbox_record_id": "recMobilePlatform"},
                            ensure_ascii=False,
                        ),
                    },
                )

                def fake_scrape(
                    db_path,
                    table_id,
                    url,
                    cfg_arg,
                    platform_hint="",
                    source_type="single",
                    transcribe=True,
                ):
                    seen_platforms.append(platform_hint)
                    return collector.desktop_save_item(
                        db_path,
                        table_id,
                        {
                            "platform": platform_hint,
                            "source_url": url,
                            "source_type": source_type,
                            "status": "基础信息成功",
                        },
                    )

                collector.desktop_scrape_single_url = fake_scrape
                collector.update_record = lambda *args, **kwargs: None

                result = collector.desktop_collect_selected_profile_items(
                    db,
                    table["id"],
                    [item["id"]],
                    {"fields": collector.DEFAULT_FIELDS, "feishu": {"mobile_inbox_table_id": "tblMobile"}},
                    "抖音",
                    transcribe=False,
                )

            self.assertEqual(result["processed_count"], 1)
            self.assertEqual(seen_platforms, ["YouTube"])
        finally:
            collector.desktop_scrape_single_url = original_scrape
            collector.update_record = original_update_record

    def test_selected_unknown_mobile_inbox_item_detects_platform_from_url(self):
        collector = load_collector()
        seen_platforms = []
        original_scrape = collector.desktop_scrape_single_url
        original_update_record = collector.update_record
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "desktop.sqlite3"
                table = collector.desktop_ensure_mobile_inbox_table(db)
                item = collector.desktop_save_item(
                    db,
                    table["id"],
                    {
                        "platform": "未知",
                        "source_url": "https://www.youtube.com/watch?v=mobile-detected-platform",
                        "source_type": "mobile_inbox",
                        "status": "待扒取",
                        "raw_metadata_json": json.dumps(
                            {"mobile_inbox_record_id": "recMobileDetectedPlatform"},
                            ensure_ascii=False,
                        ),
                    },
                )

                def fake_scrape(
                    db_path,
                    table_id,
                    url,
                    cfg_arg,
                    platform_hint="",
                    source_type="single",
                    transcribe=True,
                ):
                    seen_platforms.append(platform_hint)
                    return collector.desktop_save_item(
                        db_path,
                        table_id,
                        {
                            "platform": platform_hint,
                            "source_url": url,
                            "source_type": source_type,
                            "status": "基础信息成功",
                        },
                    )

                collector.desktop_scrape_single_url = fake_scrape
                collector.update_record = lambda *args, **kwargs: None

                collector.desktop_collect_selected_profile_items(
                    db,
                    table["id"],
                    [item["id"]],
                    {"fields": collector.DEFAULT_FIELDS, "feishu": {"mobile_inbox_table_id": "tblMobile"}},
                    "抖音",
                    transcribe=False,
                )

            self.assertEqual(seen_platforms, ["YouTube"])
        finally:
            collector.desktop_scrape_single_url = original_scrape
            collector.update_record = original_update_record

    def test_mobile_inbox_status_writeback_failure_preserves_local_success(self):
        collector = load_collector()
        cfg = {
            "fields": collector.DEFAULT_FIELDS,
            "feishu": {
                "app_id": "cli_xxx",
                "app_secret": "secret",
                "app_token": "app_token",
                "table_id": "tblMain",
                "mobile_inbox_table_id": "tblMobile",
            },
        }
        original_scrape = collector.desktop_scrape_single_url
        original_update_record = collector.update_record
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "desktop.sqlite3"
                table = collector.desktop_ensure_mobile_inbox_table(db)
                item = collector.desktop_save_item(
                    db,
                    table["id"],
                    {
                        "platform": "抖音",
                        "source_url": "https://www.douyin.com/video/mobile-writeback-failure",
                        "source_type": "mobile_inbox",
                        "status": "待扒取",
                        "raw_metadata_json": json.dumps(
                            {
                                "mobile_inbox_record_id": "recMobileWritebackFailure",
                                "mobile_remote_modified_at": "1784286000000",
                                "source": "手机收集箱",
                            },
                            ensure_ascii=False,
                        ),
                    },
                )

                def fake_scrape(db_path, table_id, url, cfg_arg, platform_hint="", source_type="single", transcribe=True):
                    return collector.desktop_save_item(
                        db_path,
                        table_id,
                        {
                            "platform": "抖音",
                            "source_url": url,
                            "source_type": source_type,
                            "title": "本地成功",
                            "status": "基础信息成功",
                            "error": "",
                        },
                    )

                collector.desktop_scrape_single_url = fake_scrape
                collector.update_record = lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit("feishu unavailable"))

                result = collector.desktop_collect_selected_profile_items(
                    db,
                    table["id"],
                    [item["id"]],
                    cfg,
                    "抖音",
                    transcribe=False,
                )
                saved = collector.desktop_get_item(db, item["id"])

            self.assertEqual(result["processed_count"], 1)
            self.assertEqual(saved["status"], "基础信息成功")
            self.assertEqual(saved["error"], "")
            self.assertIn("飞书状态回写失败", result["message"])
        finally:
            collector.desktop_scrape_single_url = original_scrape
            collector.update_record = original_update_record

    def test_mobile_inbox_scrape_preserves_record_metadata_for_future_writeback(self):
        collector = load_collector()
        original_extract = collector.extract_from_page
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "desktop.sqlite3"
                table = collector.desktop_ensure_mobile_inbox_table(db)
                item = collector.desktop_save_item(
                    db,
                    table["id"],
                    {
                        "platform": "抖音",
                        "source_url": "https://www.douyin.com/video/mobile-metadata",
                        "source_type": "mobile_inbox",
                        "status": "待扒取",
                        "raw_metadata_json": json.dumps(
                            {
                                "mobile_inbox_record_id": "recMetadata",
                                "mobile_note": "保留备注",
                                "mobile_remote_modified_at": "1784286000000",
                                "source": "手机收集箱",
                            },
                            ensure_ascii=False,
                        ),
                    },
                )
                collector.extract_from_page = lambda url, cfg: {
                    "platform": "抖音",
                    "source_url": url,
                    "title": "抓取标题",
                    "caption": "抓取文案",
                    "content_type": "image",
                    "extractor_field": "new-value",
                }

                result = collector.desktop_scrape_single_url(
                    db,
                    table["id"],
                    item["source_url"],
                    {},
                    "抖音",
                    source_type="mobile_inbox",
                    transcribe=False,
                )
                metadata = json.loads(result["raw_metadata_json"])

            self.assertEqual(metadata["mobile_inbox_record_id"], "recMetadata")
            self.assertEqual(metadata["mobile_note"], "保留备注")
            self.assertEqual(metadata["extractor_field"], "new-value")
        finally:
            collector.extract_from_page = original_extract

    def test_desktop_delete_table_removes_items_and_keeps_one_table(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            default_table = collector.desktop_list_tables(db)[0]
            custom = collector.desktop_create_table(db, "临时表", "抖音")
            collector.desktop_save_item(
                db,
                custom["id"],
                {
                    "platform": "抖音",
                    "source_url": "https://www.douyin.com/video/delete-me",
                    "source_type": "single",
                    "title": "待删除",
                    "status": "成功",
                    "raw_metadata_json": "{}",
                },
            )

            result = collector.desktop_delete_table(db, custom["id"])

            self.assertEqual(result["deleted_id"], custom["id"])
            self.assertEqual(result["next_table"]["id"], default_table["id"])
            self.assertEqual(len(collector.desktop_list_tables(db)), 1)
            self.assertEqual(collector.desktop_list_items(db, custom["id"]), [])

            with self.assertRaises(ValueError):
                collector.desktop_delete_table(db, default_table["id"])

    def test_desktop_scrape_single_url_reuses_existing_scraper_and_asr(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]
            collector.extract_from_page = lambda url, cfg: {
                "platform": "抖音",
                "content_type": "video",
                "source_url": url,
                "title": "视频标题",
                "caption": "",
                "cover_url": "https://example.com/cover.jpg",
                "duration": "00:30",
                "likes": 10,
                "comments": 2,
                "shares": 1,
                "published_at": "2026年01月01日00时00分00秒",
                "media_url": "https://example.com/video.mp4",
            }
            collector.transcribe_from_meta = lambda cfg, meta: "这是逐字稿。"

            result = collector.desktop_scrape_single_url(
                db,
                table["id"],
                "https://www.douyin.com/video/1",
                {"asr": {}},
            )

            self.assertEqual(result["status"], "成功")
            self.assertEqual(result["caption"], "这是逐字稿。")
            self.assertEqual(collector.desktop_list_items(db, table["id"])[0]["title"], "视频标题")

    def test_desktop_profile_scrape_can_skip_asr_for_fast_batch_metadata(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]
            collector.extract_from_page = lambda url, cfg: {
                "platform": "抖音",
                "content_type": "video",
                "source_url": url,
                "title": "主页批量标题",
                "caption": "",
                "cover_url": "https://example.com/cover.jpg",
                "duration": "00:30",
                "likes": 10,
                "comments": 2,
                "shares": 1,
                "published_at": "2026年01月01日00时00分00秒",
                "media_url": "https://example.com/video.mp4",
            }
            collector.transcribe_from_meta = lambda cfg, meta: (_ for _ in ()).throw(AssertionError("ASR should be skipped"))

            result = collector.desktop_scrape_single_url(
                db,
                table["id"],
                "https://www.douyin.com/video/1",
                {"asr": {}},
                "抖音",
                source_type="profile",
                transcribe=False,
            )

            self.assertEqual(result["title"], "主页批量标题")
            self.assertEqual(result["caption"], "")
            self.assertIn("主页批量模式", result["error"])

    def test_desktop_youtube_scrape_saves_metadata_when_asr_is_unavailable(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]
            collector.extract_from_page = lambda url, cfg: {
                "platform": "YouTube",
                "content_type": "video",
                "source_url": url,
                "title": "YouTube 标题",
                "caption": "",
                "cover_url": "https://i.ytimg.com/vi/abc/maxresdefault.jpg",
                "duration": "03:33",
                "likes": 10,
                "comments": 2,
                "shares": None,
                "published_at": "2026年01月01日00时00分00秒",
                "media_url": "https://rr1---sn.example.googlevideo.com/videoplayback",
            }
            collector.desktop_asr_available = lambda cfg: False
            collector.transcribe_from_meta = lambda cfg, meta: (_ for _ in ()).throw(AssertionError("ASR should not run"))

            result = collector.desktop_scrape_single_url(
                db,
                table["id"],
                "https://www.youtube.com/watch?v=abc123xyz90",
                {"asr": {"backend": "local"}},
                "YouTube",
            )

            self.assertEqual(result["status"], "字幕缺失")
            self.assertEqual(result["title"], "YouTube 标题")
            self.assertEqual(result["caption"], "")
            self.assertIn("没有可用字幕", result["error"])

    def test_desktop_scrape_preserves_metadata_when_transcription_fails(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]
            collector.extract_from_page = lambda url, cfg: {
                "platform": "YouTube",
                "content_type": "video",
                "source_url": url,
                "title": "YouTube 标题",
                "caption": "",
                "cover_url": "https://i.ytimg.com/vi/abc/sddefault.jpg",
                "duration": "05:02",
                "likes": 1,
                "comments": None,
                "shares": None,
                "published_at": "2026年01月01日00时00分00秒",
                "media_url": "https://rr1---sn.example.googlevideo.com/videoplayback",
            }
            collector.desktop_asr_available = lambda cfg: True
            collector.transcribe_from_meta = lambda cfg, meta: (_ for _ in ()).throw(RuntimeError("HTTP Error 403: Forbidden"))

            result = collector.desktop_scrape_single_url(
                db,
                table["id"],
                "https://youtu.be/abc123xyz90",
                {"asr": {"backend": "local"}},
                "YouTube",
            )

            self.assertEqual(result["title"], "YouTube 标题")
            self.assertEqual(result["cover_url"], "https://i.ytimg.com/vi/abc/sddefault.jpg")
            self.assertEqual(result["duration"], "05:02")
            self.assertEqual(result["status"], "ASR失败")
            self.assertEqual(result["caption"], "")
            self.assertIn("HTTP Error 403", result["error"])

    def test_desktop_youtube_download_403_reports_download_limited(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]
            collector.extract_from_page = lambda url, cfg: {
                "platform": "YouTube",
                "content_type": "video",
                "source_url": url,
                "final_url": "https://www.youtube.com/watch?v=abc123xyz90",
                "title": "YouTube 标题",
                "caption": "",
                "cover_url": "https://i.ytimg.com/vi/abc/sddefault.jpg",
                "duration": "05:02",
                "likes": 1,
                "comments": None,
                "shares": None,
                "published_at": "2026年01月01日00时00分00秒",
                "media_url": "https://rr1---sn.example.googlevideo.com/videoplayback",
            }
            collector.desktop_asr_available = lambda cfg: True
            collector.transcribe_from_meta = lambda cfg, meta: (_ for _ in ()).throw(
                RuntimeError("yt-dlp 下载媒体失败：HTTP Error 403: Forbidden")
            )

            result = collector.desktop_scrape_single_url(
                db,
                table["id"],
                "https://youtu.be/abc123xyz90",
                {"youtube_safety": {"enabled": True, "preflight": False}},
                "YouTube",
            )

            self.assertEqual(result["title"], "YouTube 标题")
            self.assertEqual(result["status"], "YouTube下载受限")
            self.assertIn("VPN", result["error"])

    def test_desktop_youtube_does_not_open_browser_gate_before_scrape_when_metadata_has_caption(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]
            calls = []
            original_gate = collector.youtube_prepare_browser_for_scrape
            try:
                collector.youtube_prepare_browser_for_scrape = lambda url, cfg: calls.append(url) or {"ok": True}
                collector.extract_from_page = lambda url, cfg: {
                    "platform": "YouTube",
                    "content_type": "video",
                    "source_url": url,
                    "title": "YouTube 标题",
                    "caption": "已有字幕",
                    "cover_url": "",
                    "duration": "01:00",
                    "likes": None,
                    "comments": None,
                    "shares": None,
                    "published_at": "",
                    "media_url": "",
                }

                result = collector.desktop_scrape_single_url(
                    db,
                    table["id"],
                    "https://www.youtube.com/watch?v=abc123xyz90",
                    {
                        "youtube_safety": {"enabled": True, "preflight": False, "open_browser_before_scrape": True},
                        "browser_fallback": {"enabled": True},
                    },
                    "YouTube",
                )
            finally:
                collector.youtube_prepare_browser_for_scrape = original_gate

            self.assertEqual(calls, [])
            self.assertEqual(result["status"], "成功")

    def test_desktop_youtube_login_error_from_scraper_is_saved(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]
            original_gate = collector.youtube_prepare_browser_for_scrape
            original_extract = collector.extract_from_page
            try:
                collector.youtube_prepare_browser_for_scrape = lambda url, cfg: (_ for _ in ()).throw(
                    RuntimeError("YouTube 要求登录验证：请在专用浏览器里完成 YouTube 登录/机器人验证后重试。")
                )
                collector.extract_from_page = lambda url, cfg: (_ for _ in ()).throw(
                    RuntimeError("YouTube 要求登录验证：请在专用浏览器里完成 YouTube 登录/机器人验证后重试。")
                )

                result = collector.desktop_scrape_single_url(
                    db,
                    table["id"],
                    "https://www.youtube.com/watch?v=abc123xyz90",
                    {
                        "youtube_safety": {"enabled": True, "preflight": False, "open_browser_before_scrape": True},
                        "browser_fallback": {"enabled": True},
                    },
                    "YouTube",
                )
            finally:
                collector.youtube_prepare_browser_for_scrape = original_gate
                collector.extract_from_page = original_extract

            self.assertEqual(result["status"], "需登录")
            self.assertIn("登录验证", result["error"])

    def test_transcribe_from_meta_uses_ytdlp_for_youtube_instead_of_expiring_media_url(self):
        collector = load_collector()
        temp_root = Path(tempfile.mkdtemp(prefix="collector-test-"))
        media_path = temp_root / "media.mp4"
        media_path.write_bytes(b"fake video")
        audio_path = temp_root / "audio.mp3"
        calls = []

        def fake_download_media_file(url, cfg, platform):
            raise AssertionError("YouTube ASR should not download expiring googlevideo URL directly")

        def fake_download_media_with_ytdlp(url, cfg):
            calls.append(url)
            return media_path

        original_download_file = collector.download_media_file
        original_download_ytdlp = collector.download_media_with_ytdlp
        original_extract_audio = collector.extract_audio_file
        original_transcribe_audio = collector.transcribe_audio_file
        try:
            collector.download_media_file = fake_download_media_file
            collector.download_media_with_ytdlp = fake_download_media_with_ytdlp
            collector.extract_audio_file = lambda path: audio_path
            collector.transcribe_audio_file = lambda cfg, path: "YouTube 转写结果"

            text = collector.transcribe_from_meta(
                {},
                {
                    "platform": "YouTube",
                    "source_url": "https://youtu.be/abc123xyz90",
                    "final_url": "https://www.youtube.com/watch?v=abc123xyz90",
                    "media_url": "https://rr4---sn.example.googlevideo.com/videoplayback",
                },
            )
        finally:
            collector.download_media_file = original_download_file
            collector.download_media_with_ytdlp = original_download_ytdlp
            collector.extract_audio_file = original_extract_audio
            collector.transcribe_audio_file = original_transcribe_audio

        self.assertEqual(text, "YouTube 转写结果")
        self.assertEqual(calls, ["https://www.youtube.com/watch?v=abc123xyz90"])
        self.assertFalse(temp_root.exists())

    def test_transcribe_from_meta_prefers_youtube_browser_transcript(self):
        collector = load_collector()
        original_browser_transcript = collector.extract_youtube_transcript_with_browser
        original_download_ytdlp = collector.download_media_with_ytdlp
        try:
            collector.extract_youtube_transcript_with_browser = lambda url, cfg: "浏览器文字稿"
            collector.download_media_with_ytdlp = lambda url, cfg: (_ for _ in ()).throw(
                AssertionError("YouTube browser transcript should run before audio download")
            )

            text = collector.transcribe_from_meta(
                {"browser_fallback": {"enabled": True}},
                {
                    "platform": "YouTube",
                    "source_url": "https://youtu.be/abc123xyz90",
                    "final_url": "https://www.youtube.com/watch?v=abc123xyz90",
                    "media_url": "https://rr4---sn.example.googlevideo.com/videoplayback",
                },
            )
        finally:
            collector.extract_youtube_transcript_with_browser = original_browser_transcript
            collector.download_media_with_ytdlp = original_download_ytdlp

        self.assertEqual(text, "浏览器文字稿。")

    def test_download_media_with_ytdlp_uses_configured_proxy_and_format(self):
        collector = load_collector()
        temp_root = Path(tempfile.mkdtemp(prefix="collector-test-"))
        calls = []

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        original_ytdlp_path = collector.ytdlp_path
        original_mkdtemp = collector.tempfile.mkdtemp
        original_run = collector.subprocess.run
        try:
            collector.ytdlp_path = lambda: "/usr/local/bin/yt-dlp"
            collector.tempfile.mkdtemp = lambda prefix: str(temp_root)

            def fake_run(cmd, *args, **kwargs):
                calls.append(cmd)
                (temp_root / "media.mp4").write_bytes(b"fake")
                return Result()

            collector.subprocess.run = fake_run
            path = collector.download_media_with_ytdlp(
                "https://youtu.be/abc123xyz90",
                {"yt_dlp": {"enabled": True, "proxy": "http://127.0.0.1:7897", "download_format": "18/best"}},
            )
        finally:
            collector.ytdlp_path = original_ytdlp_path
            collector.tempfile.mkdtemp = original_mkdtemp
            collector.subprocess.run = original_run

        self.assertEqual(path.name, "media.mp4")
        self.assertIn("--proxy", calls[0])
        self.assertIn("http://127.0.0.1:7897", calls[0])
        self.assertIn("-f", calls[0])
        self.assertIn("18/best", calls[0])

    def test_add_ytdlp_common_args_includes_js_runtime(self):
        collector = load_collector()
        cmd = ["yt-dlp"]

        collector.add_ytdlp_common_args(cmd, {"yt_dlp": {"js_runtimes": "node:/tmp/node"}})

        self.assertIn("--js-runtimes", cmd)
        self.assertIn("node:/tmp/node", cmd)

    def test_ensure_ffmpeg_on_path_prepends_ffmpeg_directory(self):
        collector = load_collector()
        original_path = collector.os.environ.get("PATH", "")
        original_ffmpeg_path = collector.ffmpeg_path
        try:
            collector.os.environ["PATH"] = "/usr/bin"
            collector.ffmpeg_path = lambda: "/tmp/chen-bin/ffmpeg"

            collector.ensure_ffmpeg_on_path()

            self.assertTrue(collector.os.environ["PATH"].startswith("/tmp/chen-bin:"))
        finally:
            collector.ffmpeg_path = original_ffmpeg_path
            collector.os.environ["PATH"] = original_path

    def test_download_media_with_ytdlp_retries_youtube_client_strategies(self):
        collector = load_collector()
        temp_root = Path(tempfile.mkdtemp(prefix="collector-test-"))
        calls = []

        class Result:
            def __init__(self, returncode, stderr=""):
                self.returncode = returncode
                self.stdout = ""
                self.stderr = stderr

        original_ytdlp_path = collector.ytdlp_path
        original_mkdtemp = collector.tempfile.mkdtemp
        original_run = collector.subprocess.run
        try:
            collector.ytdlp_path = lambda: "/usr/local/bin/yt-dlp"
            collector.tempfile.mkdtemp = lambda prefix: str(temp_root)

            def fake_run(cmd, *args, **kwargs):
                calls.append(cmd)
                if "youtube:player_client=mweb" not in cmd:
                    return Result(1, "HTTP Error 403: Forbidden")
                (temp_root / "media.m4a").write_bytes(b"fake")
                return Result(0)

            collector.subprocess.run = fake_run
            path = collector.download_media_with_ytdlp(
                "https://www.youtube.com/watch?v=abc123xyz90",
                {
                    "yt_dlp": {
                        "enabled": True,
                        "youtube_retry_extractor_args": ["youtube:player_client=mweb"],
                    }
                },
            )
        finally:
            collector.ytdlp_path = original_ytdlp_path
            collector.tempfile.mkdtemp = original_mkdtemp
            collector.subprocess.run = original_run
            shutil.rmtree(temp_root, ignore_errors=True)

        self.assertEqual(path.name, "media.m4a")
        self.assertNotIn("youtube:player_client=mweb", calls[0])
        self.assertTrue(any("youtube:player_client=mweb" in cmd for cmd in calls[1:]))

    def test_download_media_with_ytdlp_retries_login_browser_cookie_source(self):
        collector = load_collector()
        temp_root = Path(tempfile.mkdtemp(prefix="collector-test-"))
        calls = []

        class Result:
            def __init__(self, returncode, stderr=""):
                self.returncode = returncode
                self.stdout = ""
                self.stderr = stderr

        original_ytdlp_path = collector.ytdlp_path
        original_mkdtemp = collector.tempfile.mkdtemp
        original_run = collector.subprocess.run
        try:
            collector.ytdlp_path = lambda: "/usr/local/bin/yt-dlp"
            collector.tempfile.mkdtemp = lambda prefix: str(temp_root)

            def fake_run(cmd, *args, **kwargs):
                calls.append(cmd)
                source = cmd[cmd.index("--cookies-from-browser") + 1]
                if source == "edge":
                    return Result(1, "Sign in to confirm you’re not a bot")
                (temp_root / "media.m4a").write_bytes(b"fake")
                return Result(0)

            collector.subprocess.run = fake_run
            path = collector.download_media_with_ytdlp(
                "https://www.youtube.com/watch?v=abc123xyz90",
                {
                    "yt_dlp": {
                        "enabled": True,
                        "cookies_from_browser": "edge",
                        "youtube_retry_extractor_args": [],
                    },
                    "browser_fallback": {
                        "executable_path": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    },
                },
            )
        finally:
            collector.ytdlp_path = original_ytdlp_path
            collector.tempfile.mkdtemp = original_mkdtemp
            collector.subprocess.run = original_run
            shutil.rmtree(temp_root, ignore_errors=True)

        self.assertEqual(path.name, "media.m4a")
        seen_sources = []
        for cmd in calls:
            source = cmd[cmd.index("--cookies-from-browser") + 1]
            if source not in seen_sources:
                seen_sources.append(source)
        self.assertEqual(seen_sources, ["edge", "chrome"])

    def test_download_media_with_ytdlp_uses_cdp_exported_cookies_before_browser_decrypt(self):
        collector = load_collector()
        temp_root = Path(tempfile.mkdtemp(prefix="collector-test-"))
        calls = []

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        original_ytdlp_path = collector.ytdlp_path
        original_mkdtemp = collector.tempfile.mkdtemp
        original_run = collector.subprocess.run
        original_export = getattr(collector, "export_youtube_cookies_from_cdp", None)
        try:
            collector.ytdlp_path = lambda: "/usr/local/bin/yt-dlp"
            collector.tempfile.mkdtemp = lambda prefix: str(temp_root)

            def fake_export(url, cfg, cookie_path):
                cookie_path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
                return cookie_path

            def fake_run(cmd, *args, **kwargs):
                calls.append(cmd)
                (temp_root / "media.m4a").write_bytes(b"fake")
                return Result()

            collector.export_youtube_cookies_from_cdp = fake_export
            collector.subprocess.run = fake_run
            path = collector.download_media_with_ytdlp(
                "https://www.youtube.com/watch?v=abc123xyz90",
                {
                    "yt_dlp": {
                        "enabled": True,
                        "cookies_from_browser": "chrome:/tmp/profile",
                        "youtube_retry_extractor_args": [],
                    },
                    "browser_fallback": {"enabled": True, "profile_dir": "/tmp/profile"},
                },
            )
        finally:
            collector.ytdlp_path = original_ytdlp_path
            collector.tempfile.mkdtemp = original_mkdtemp
            collector.subprocess.run = original_run
            if original_export is not None:
                collector.export_youtube_cookies_from_cdp = original_export
            shutil.rmtree(temp_root, ignore_errors=True)

        self.assertEqual(path.name, "media.m4a")
        self.assertIn("--cookies", calls[0])
        self.assertNotIn("--cookies-from-browser", calls[0])

    def test_download_media_with_ytdlp_retries_fallback_format_when_requested_missing(self):
        collector = load_collector()
        temp_root = Path(tempfile.mkdtemp(prefix="collector-test-"))
        calls = []

        class Result:
            def __init__(self, returncode, stderr=""):
                self.returncode = returncode
                self.stdout = ""
                self.stderr = stderr

        original_ytdlp_path = collector.ytdlp_path
        original_mkdtemp = collector.tempfile.mkdtemp
        original_run = collector.subprocess.run
        try:
            collector.ytdlp_path = lambda: "/usr/local/bin/yt-dlp"
            collector.tempfile.mkdtemp = lambda prefix: str(temp_root)

            def fake_run(cmd, *args, **kwargs):
                calls.append(cmd)
                fmt = cmd[cmd.index("-f") + 1]
                if fmt == "ba[ext=m4a]/ba":
                    return Result(1, "Requested format is not available")
                (temp_root / "media.webm").write_bytes(b"fake")
                return Result(0)

            collector.subprocess.run = fake_run
            path = collector.download_media_with_ytdlp(
                "https://www.youtube.com/watch?v=abc123xyz90",
                {
                    "yt_dlp": {
                        "enabled": True,
                        "download_format": "ba[ext=m4a]/ba",
                        "youtube_retry_extractor_args": [],
                    }
                },
            )
        finally:
            collector.ytdlp_path = original_ytdlp_path
            collector.tempfile.mkdtemp = original_mkdtemp
            collector.subprocess.run = original_run
            shutil.rmtree(temp_root, ignore_errors=True)

        self.assertEqual(path.name, "media.webm")
        self.assertEqual(calls[0][calls[0].index("-f") + 1], "ba[ext=m4a]/ba")
        self.assertEqual(calls[1][calls[1].index("-f") + 1], "bestaudio/best")

    def test_download_media_with_ytdlp_probe_mode_limits_attempts(self):
        collector = load_collector()
        temp_root = Path(tempfile.mkdtemp(prefix="collector-test-"))
        calls = []

        class Result:
            returncode = 1
            stdout = ""
            stderr = "HTTP Error 403: Forbidden"

        original_ytdlp_path = collector.ytdlp_path
        original_mkdtemp = collector.tempfile.mkdtemp
        original_run = collector.subprocess.run
        try:
            collector.ytdlp_path = lambda: "/usr/local/bin/yt-dlp"
            collector.tempfile.mkdtemp = lambda prefix: str(temp_root)

            def fake_run(cmd, *args, **kwargs):
                calls.append(cmd)
                return Result()

            collector.subprocess.run = fake_run
            with self.assertRaises(RuntimeError):
                collector.download_media_with_ytdlp(
                    "https://www.youtube.com/watch?v=abc123xyz90",
                    {
                        "yt_dlp": {
                            "enabled": True,
                            "_download_probe": True,
                            "download_probe_max_attempts": 2,
                            "youtube_retry_extractor_args": ["youtube:player_client=mweb"],
                        }
                    },
                )
        finally:
            collector.ytdlp_path = original_ytdlp_path
            collector.tempfile.mkdtemp = original_mkdtemp
            collector.subprocess.run = original_run
            shutil.rmtree(temp_root, ignore_errors=True)

        self.assertEqual(len(calls), 2)

    def test_download_media_with_ytdlp_adds_po_token_extractor_arg(self):
        collector = load_collector()

        strategies = collector.ytdlp_download_strategies(
            "https://www.youtube.com/watch?v=abc123xyz90",
            {
                "yt_dlp": {
                    "youtube_po_token": "mweb.gvs+TOKEN",
                    "youtube_retry_extractor_args": [],
                }
            },
        )

        self.assertEqual(strategies[0], "youtube:player_client=mweb;po_token=mweb.gvs+TOKEN")
        self.assertIn("youtube:player_client=mweb;po_token=mweb.gvs+TOKEN", strategies)

    def test_download_media_with_ytdlp_prioritizes_po_token_provider_mweb(self):
        collector = load_collector()

        strategies = collector.ytdlp_download_strategies(
            "https://www.youtube.com/watch?v=abc123xyz90",
            {
                "yt_dlp": {
                    "youtube_po_token_provider": True,
                    "youtube_retry_extractor_args": ["youtube:player_client=ios"],
                }
            },
        )

        self.assertEqual(strategies[0], "youtube:player_client=mweb")
        self.assertIn("youtube:player_client=ios", strategies)

    def test_youtube_diagnose_config_reports_po_token_provider(self):
        collector = load_collector()

        config = collector.youtube_diagnose_config(
            {"yt_dlp": {"youtube_po_token_provider": True}},
            "https://www.youtube.com/watch?v=abc123xyz90",
        )

        self.assertTrue(config["po_token_provider_enabled"])
        self.assertEqual(config["retry_strategies"][0], "youtube:player_client=mweb")

    def test_save_youtube_po_provider_config_preserves_existing_config(self):
        collector = load_collector()
        config_path = Path(tempfile.mkdtemp(prefix="collector-config-")) / "config.json"
        try:
            config_path.write_text(
                json.dumps(
                    {
                        "feishu": {"app_secret": "SECRET"},
                        "yt_dlp": {"cookies_from_browser": "chrome"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = collector.save_youtube_po_provider_config(config_path, True)
            saved = json.loads(config_path.read_text(encoding="utf-8"))
        finally:
            shutil.rmtree(config_path.parent, ignore_errors=True)

        self.assertTrue(result["youtube_po_token_provider"])
        self.assertEqual(saved["feishu"]["app_secret"], "SECRET")
        self.assertEqual(saved["yt_dlp"]["cookies_from_browser"], "chrome")
        self.assertTrue(saved["yt_dlp"]["youtube_po_token_provider"])

    def test_youtube_diagnose_reports_caption_available_without_download_probe(self):
        collector = load_collector()
        calls = []
        collector.youtube_preflight_check = lambda cfg: {"ok": True, "status": "可采集", "error": ""}
        collector.extract_with_ytdlp = lambda url, cfg: {
            "platform": "YouTube",
            "title": "YouTube 标题",
            "caption": "已有字幕",
            "duration": "01:01",
        }
        collector.download_media_with_ytdlp = lambda url, cfg: calls.append(url)

        result = collector.youtube_diagnose_url(
            "https://www.youtube.com/watch?v=abc123xyz90",
            {"yt_dlp": {"enabled": True}},
        )

        self.assertEqual(result["status"], "字幕可用")
        self.assertTrue(result["checks"]["captions"]["ok"])
        self.assertEqual(calls, [])

    def test_youtube_diagnose_download_probe_points_to_po_token(self):
        collector = load_collector()
        collector.youtube_preflight_check = lambda cfg: {"ok": True, "status": "可采集", "error": ""}
        collector.extract_with_ytdlp = lambda url, cfg: {
            "platform": "YouTube",
            "title": "YouTube 标题",
            "caption": "",
            "duration": "01:01",
        }
        collector.download_media_with_ytdlp = lambda url, cfg: (_ for _ in ()).throw(
            RuntimeError("yt-dlp 下载媒体失败：default: HTTP Error 403: Forbidden")
        )

        result = collector.youtube_diagnose_url(
            "https://www.youtube.com/watch?v=abc123xyz90",
            {"yt_dlp": {"enabled": True, "youtube_po_token": ""}},
            probe_download=True,
        )

        self.assertEqual(result["status"], "下载需PO Token")
        self.assertFalse(result["checks"]["download_probe"]["ok"])
        self.assertIn("PO Token", result["recommended_action"])

    def test_youtube_diagnose_flags_browser_cookie_mismatch(self):
        collector = load_collector()
        config = collector.youtube_diagnose_config(
            {
                "yt_dlp": {"cookies_from_browser": "edge"},
                "browser_fallback": {
                    "executable_path": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                },
            },
            "https://www.youtube.com/watch?v=abc123xyz90",
        )

        self.assertTrue(config["browser_cookie_mismatch"])
        self.assertEqual(config["login_browser"], "chrome")
        self.assertEqual(config["cookie_browser"], "edge")
        self.assertIn("统一", config["cookie_advice"])

    def test_ytdlp_cookie_sources_retry_login_browser_when_mismatch(self):
        collector = load_collector()

        sources = collector.ytdlp_cookie_sources(
            "https://www.youtube.com/watch?v=abc123xyz90",
            {
                "yt_dlp": {"cookies_from_browser": "edge"},
                "browser_fallback": {
                    "executable_path": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                },
            },
        )

        self.assertEqual(sources, ["edge", "chrome"])

    def test_ytdlp_cookie_sources_include_dedicated_login_profile(self):
        collector = load_collector()

        sources = collector.ytdlp_cookie_sources(
            "https://www.youtube.com/watch?v=abc123xyz90",
            {
                "yt_dlp": {"cookies_from_browser": "edge"},
                "browser_fallback": {
                    "executable_path": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    "profile_dir": "/tmp/chen-youtube-profile",
                },
            },
        )

        self.assertEqual(sources, ["edge", "chrome:/tmp/chen-youtube-profile", "chrome"])

    def test_extract_with_ytdlp_retries_login_browser_cookie_after_cookie_error(self):
        collector = load_collector()
        calls = []
        payload = {
            "webpage_url": "https://www.youtube.com/watch?v=abc123xyz90",
            "title": "YouTube 标题",
            "duration": 61,
            "url": "https://example.com/video.mp4",
        }

        class Result:
            def __init__(self, returncode, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        original_ytdlp_path = collector.ytdlp_path
        original_run = collector.subprocess.run
        try:
            collector.ytdlp_path = lambda: "/usr/local/bin/yt-dlp"

            def fake_run(cmd, *args, **kwargs):
                calls.append(cmd)
                if "--cookies-from-browser" in cmd and cmd[cmd.index("--cookies-from-browser") + 1] == "edge":
                    return Result(1, stderr="Sign in to confirm you’re not a bot")
                return Result(0, stdout=__import__("json").dumps(payload))

            collector.subprocess.run = fake_run
            meta = collector.extract_with_ytdlp(
                "https://www.youtube.com/watch?v=abc123xyz90",
                {
                    "yt_dlp": {"enabled": True, "cookies_from_browser": "edge"},
                    "browser_fallback": {
                        "executable_path": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    },
                },
            )
        finally:
            collector.ytdlp_path = original_ytdlp_path
            collector.subprocess.run = original_run

        self.assertEqual(meta["title"], "YouTube 标题")
        self.assertEqual([cmd[cmd.index("--cookies-from-browser") + 1] for cmd in calls], ["edge", "chrome"])

    def test_desktop_app_exposes_youtube_diagnose_entry(self):
        collector = load_collector()

        self.assertIn("/api/youtube/diagnose", collector.DESKTOP_APP_HTML)
        self.assertIn("/api/youtube/po-provider-enable", collector.DESKTOP_APP_HTML)
        self.assertIn("YouTube修复", collector.DESKTOP_APP_HTML)
        self.assertIn("检查失败原因", collector.DESKTOP_APP_HTML)
        self.assertIn("测试能否转写", collector.DESKTOP_APP_HTML)
        self.assertIn("增强下载", collector.DESKTOP_APP_HTML)
        self.assertNotIn(">诊断YouTube<", collector.DESKTOP_APP_HTML)
        self.assertNotIn(">下载探测<", collector.DESKTOP_APP_HTML)
        self.assertNotIn(">启用PO模式<", collector.DESKTOP_APP_HTML)
        self.assertIn("toggleYouTubeTools()", collector.DESKTOP_APP_HTML)
        self.assertIn("diagnoseYouTube(true)", collector.DESKTOP_APP_HTML)
        self.assertIn("probe_download:probe", collector.DESKTOP_APP_HTML)

    def test_youtube_preflight_reports_missing_ytdlp(self):
        collector = load_collector()
        original_ytdlp_path = collector.ytdlp_path
        try:
            collector.ytdlp_path = lambda: None
            result = collector.youtube_preflight_check({"youtube_safety": {"enabled": True}})
        finally:
            collector.ytdlp_path = original_ytdlp_path

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "yt-dlp缺失")
        self.assertIn("yt-dlp", result["error"])

    def test_desktop_youtube_network_errors_are_labeled_vpn_network(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]
            collector.extract_from_page = lambda url, cfg: (_ for _ in ()).throw(RuntimeError("urlopen error DNS failed"))

            result = collector.desktop_scrape_single_url(
                db,
                table["id"],
                "https://www.youtube.com/watch?v=abc123xyz90",
                {"youtube_safety": {"enabled": True, "preflight": False}},
                "YouTube",
            )

            self.assertEqual(result["status"], "VPN/网络异常")
            self.assertIn("VPN", result["error"])

    def test_desktop_collect_selected_youtube_throttles_between_items(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]
            first = collector.desktop_save_item(
                db,
                table["id"],
                {"platform": "YouTube", "source_url": "https://www.youtube.com/watch?v=one12345678", "source_type": "profile", "status": "候选"},
            )
            second = collector.desktop_save_item(
                db,
                table["id"],
                {"platform": "YouTube", "source_url": "https://www.youtube.com/watch?v=two12345678", "source_type": "profile", "status": "候选"},
            )
            sleeps = []

            def fake_scrape(db_path, table_id, url, cfg, platform_hint="", source_type="single", transcribe=True):
                return collector.desktop_save_item(
                    db_path,
                    table_id,
                    {"platform": "YouTube", "source_url": url, "source_type": source_type, "title": "ok", "status": "基础信息成功"},
                )

            original_scrape = collector.desktop_scrape_single_url
            original_sleep = collector.time.sleep
            try:
                collector.desktop_scrape_single_url = fake_scrape
                collector.time.sleep = lambda seconds: sleeps.append(seconds)
                result = collector.desktop_collect_selected_profile_items(
                    db,
                    table["id"],
                    [first["id"], second["id"]],
                    {"youtube_safety": {"enabled": True, "throttle_seconds": 2.5}},
                    "YouTube",
                    transcribe=False,
                )
            finally:
                collector.desktop_scrape_single_url = original_scrape
                collector.time.sleep = original_sleep

            self.assertEqual(result["processed_count"], 2)
            self.assertEqual(sleeps, [2.5])

    def test_desktop_collect_selected_youtube_pauses_after_network_failures(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]
            ids = []
            for index in range(3):
                saved = collector.desktop_save_item(
                    db,
                    table["id"],
                    {
                        "platform": "YouTube",
                        "source_url": f"https://www.youtube.com/watch?v=fail{index}12345",
                        "source_type": "profile",
                        "status": "候选",
                    },
                )
                ids.append(saved["id"])
            calls = []

            def fake_scrape(db_path, table_id, url, cfg, platform_hint="", source_type="single", transcribe=True):
                calls.append(url)
                return collector.desktop_save_item(
                    db_path,
                    table_id,
                    {"platform": "YouTube", "source_url": url, "source_type": source_type, "status": "VPN/网络异常", "error": "VPN 不稳定"},
                )

            original_scrape = collector.desktop_scrape_single_url
            try:
                collector.desktop_scrape_single_url = fake_scrape
                result = collector.desktop_collect_selected_profile_items(
                    db,
                    table["id"],
                    ids,
                    {"youtube_safety": {"enabled": True, "throttle_seconds": 0, "max_consecutive_network_failures": 2}},
                    "YouTube",
                    transcribe=False,
                )
            finally:
                collector.desktop_scrape_single_url = original_scrape

            self.assertEqual(len(calls), 2)
            self.assertEqual(result["processed_count"], 2)
            self.assertTrue(result["paused"])
            self.assertIn("VPN", result["message"])

    def test_desktop_local_asr_requires_ffmpeg(self):
        collector = load_collector()
        original_ffmpeg_path = collector.ffmpeg_path
        try:
            collector.ffmpeg_path = lambda: ""
            self.assertFalse(collector.desktop_asr_available({"asr": {"backend": "local"}}))
        finally:
            collector.ffmpeg_path = original_ffmpeg_path

    def test_desktop_profile_complete_counts_only_successful_core_data(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]
            session_id = "profile-session-test"
            collector.DESKTOP_PROFILE_SESSIONS[session_id] = {
                "found_count": 1,
                "completed_count": 0,
                "error_count": 0,
                "updated_at": "",
            }
            calls = []

            def fake_scrape(*args, **kwargs):
                calls.append(kwargs)
                return {
                    "status": "浏览器未就绪",
                    "title": "",
                    "cover_url": "",
                    "duration": "",
                }

            original_scrape = collector.desktop_scrape_single_url
            original_sleep = collector.time.sleep
            try:
                collector.desktop_scrape_single_url = fake_scrape
                collector.time.sleep = lambda _seconds: None
                collector.desktop_profile_complete_video(
                    session_id,
                    db,
                    table["id"],
                    "https://www.douyin.com/video/wait-login",
                    {},
                )
            finally:
                collector.desktop_scrape_single_url = original_scrape
                collector.time.sleep = original_sleep
                session = collector.DESKTOP_PROFILE_SESSIONS.pop(session_id)

            self.assertEqual(len(calls), 3)
            self.assertEqual(session["completed_count"], 0)
            self.assertEqual(session["error_count"], 1)
            self.assertIn("待处理 1 条", session["message"])

    def test_desktop_profile_complete_passes_target_platform_to_scraper(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "desktop.sqlite3"
            collector.desktop_db_init(db)
            table = collector.desktop_list_tables(db)[0]
            session_id = "youtube-profile-session-test"
            collector.DESKTOP_PROFILE_SESSIONS[session_id] = {
                "found_count": 1,
                "completed_count": 0,
                "error_count": 0,
                "updated_at": "",
            }
            calls = []

            def fake_scrape(db_path, table_id, url, cfg, platform_hint="", source_type="single", transcribe=True):
                calls.append((platform_hint, source_type, transcribe))
                return {
                    "status": "基础信息成功",
                    "title": "YouTube 标题",
                    "cover_url": "https://i.ytimg.com/vi/abc/sddefault.jpg",
                    "duration": "05:02",
                }

            original_scrape = collector.desktop_scrape_single_url
            try:
                collector.desktop_scrape_single_url = fake_scrape
                collector.desktop_profile_complete_video(
                    session_id,
                    db,
                    table["id"],
                    "https://www.youtube.com/watch?v=abc123xyz90",
                    {},
                    "YouTube",
                )
            finally:
                collector.desktop_scrape_single_url = original_scrape
                collector.DESKTOP_PROFILE_SESSIONS.pop(session_id)

            self.assertEqual(calls, [("YouTube", "profile", False)])

    def test_load_config_fills_default_browser_executable_when_available(self):
        collector = load_collector()
        original = collector.DEFAULT_BROWSER_EXECUTABLES
        with tempfile.TemporaryDirectory() as tmp:
            browser = Path(tmp) / "Chrome"
            browser.write_text("", encoding="utf-8")
            cfg_path = Path(tmp) / "config.json"
            cfg_path.write_text('{"browser_fallback":{"executable_path":""}}', encoding="utf-8")
            collector.DEFAULT_BROWSER_EXECUTABLES = [str(browser)]
            try:
                cfg = collector.load_config(cfg_path)
            finally:
                collector.DEFAULT_BROWSER_EXECUTABLES = original

            self.assertEqual(cfg["browser_fallback"]["executable_path"], str(browser))

    def test_iter_json_objects_reads_xiaohongshu_initial_state(self):
        collector = load_collector()
        html = '<script>window.__INITIAL_STATE__={"note":{"title":"小红书标题"}};</script>'

        objects = list(collector.iter_json_objects(html))

        self.assertEqual(objects[-1]["note"]["title"], "小红书标题")

    def test_iter_json_objects_reads_xiaohongshu_initial_state_with_undefined(self):
        collector = load_collector()
        html = (
            '<script>window.__INITIAL_STATE__={"feed":undefined,'
            '"note":{"noteDetailMap":{"target":{"note":{"noteId":"target","type":"video"}}}}};</script>'
        )

        objects = list(collector.iter_json_objects(html))

        self.assertIsNone(objects[-1]["feed"])
        self.assertEqual(
            objects[-1]["note"]["noteDetailMap"]["target"]["note"]["noteId"],
            "target",
        )

    def test_extract_xiaohongshu_image_note_writes_body_as_caption(self):
        collector = load_collector()
        html = """
        <html><script>window.__INITIAL_STATE__={
          "noteDetailMap": {
            "abc": {"note": {
              "type": "normal",
              "title": "穿搭图文笔记",
              "desc": "这是图文正文",
              "imageList": [{"urlDefault": "https://ci.xiaohongshu.com/cover.jpg"}],
              "interactInfo": {"likedCount": "1.2万", "commentCount": "345", "shareCount": "67", "collectedCount": "890"},
              "time": 1710000000000
            }}
          }
        };</script></html>
        """

        meta = collector.extract_xiaohongshu_meta("https://www.xiaohongshu.com/explore/abc", html, "https://www.xiaohongshu.com/explore/abc")

        self.assertEqual(meta["platform"], "小红书")
        self.assertEqual(meta["title"], "穿搭图文笔记")
        self.assertEqual(meta["caption"], "这是图文正文")
        self.assertEqual(meta["cover_url"], "https://ci.xiaohongshu.com/cover.jpg")
        self.assertEqual(meta["duration"], "图文")
        self.assertEqual(meta["likes"], 12000)
        self.assertEqual(meta["comments"], 345)
        self.assertEqual(meta["shares"], 67)

    def test_extract_xiaohongshu_cover_normalizes_protocol_relative_url(self):
        collector = load_collector()
        html = """
        <meta property="og:image" content="//ci.xiaohongshu.com/cover.jpg">
        <script>window.__INITIAL_STATE__={"note":{"title":"标题","desc":"正文","type":"normal"}};</script>
        """

        meta = collector.extract_xiaohongshu_meta("https://www.xiaohongshu.com/explore/abc", html, "https://www.xiaohongshu.com/explore/abc")

        self.assertEqual(meta["cover_url"], "https://ci.xiaohongshu.com/cover.jpg")

    def test_extract_xiaohongshu_video_note_keeps_caption_for_asr(self):
        collector = load_collector()
        html = """
        <html><script>window.__INITIAL_STATE__={
          "note": {
            "type": "video",
            "title": "视频笔记",
            "desc": "这是视频简介，不是逐字稿",
            "cover": {"url": "https://ci.xiaohongshu.com/video-cover.jpg"},
            "interactInfo": {"likedCount": 12, "commentCount": 3, "shareCount": 4, "collectedCount": 9},
            "video": {"media": {"stream": {"h264": [{"masterUrl": "https://sns-video-hw.xhscdn.com/video.mp4"}]}}, "duration": 83000}
          }
        };</script></html>
        """

        meta = collector.extract_xiaohongshu_meta("https://www.xiaohongshu.com/explore/abc", html, "https://www.xiaohongshu.com/explore/abc")

        self.assertEqual(meta["title"], "视频笔记")
        self.assertEqual(meta["caption"], "")
        self.assertEqual(meta["cover_url"], "https://ci.xiaohongshu.com/video-cover.jpg")
        self.assertEqual(meta["duration"], "01:23")
        self.assertEqual(meta["media_url"], "https://sns-video-hw.xhscdn.com/video.mp4")
        self.assertEqual(meta["shares"], 4)

    def test_extract_xiaohongshu_does_not_treat_collections_as_shares(self):
        collector = load_collector()
        html = """
        <script>window.__INITIAL_STATE__={"note":{"noteDetailMap":{"abc":{"note":{
          "noteId":"abc",
          "type":"normal",
          "title":"图文",
          "desc":"正文",
          "interactInfo":{"likedCount":"3","commentCount":"2","collectedCount":"99"},
          "imageList":[{"urlDefault":"https://ci.xiaohongshu.com/cover.jpg"}]
        }}}}};</script>
        """

        meta = collector.extract_xiaohongshu_meta(
            "https://www.xiaohongshu.com/explore/abc",
            html,
            "https://www.xiaohongshu.com/explore/abc",
        )

        self.assertIsNone(meta["shares"])

    def test_extract_xiaohongshu_video_note_matches_url_id_and_reads_current_schema(self):
        collector = load_collector()
        html = """
        <html>
        <meta name="og:title" content="错误的元标签标题 - 小红书">
        <script>window.__INITIAL_STATE__={
          "feed": undefined,
          "note": {
            "noteDetailMap": {
              "other": {"note": {
                "noteId": "other",
                "type": "normal",
                "title": "不应选中的图文",
                "desc": "错误正文",
                "imageList": [{"urlDefault": "https://ci.xiaohongshu.com/wrong.jpg"}]
              }},
              "6a23fe8c000000003503bc2a": {"note": {
                "noteId": "6a23fe8c000000003503bc2a",
                "type": "video",
                "title": "Jason谈展示面三要素（第一期：颜值）",
                "desc": "#男性情感[话题]# #展示面[话题]#",
                "time": 1780743820000,
                "interactInfo": {
                  "likedCount": "14",
                  "commentCount": "2",
                  "shareCount": "1"
                },
                "imageList": [{"urlDefault": "https://ci.xiaohongshu.com/right.jpg"}],
                "video": {
                  "media": {
                    "video": {"duration": 132},
                    "stream": {
                      "h264": [{
                        "masterUrl": "https://sns-video-v4.xhscdn.com/target.mp4",
                        "videoDuration": 131900
                      }]
                    }
                  }
                }
              }}
            }
          }
        };</script></html>
        """

        meta = collector.extract_xiaohongshu_meta(
            "https://www.xiaohongshu.com/discovery/item/6a23fe8c000000003503bc2a",
            html,
            "https://www.xiaohongshu.com/explore/6a23fe8c000000003503bc2a",
        )

        self.assertEqual(meta["content_type"], "video")
        self.assertEqual(meta["title"], "Jason谈展示面三要素（第一期：颜值）")
        self.assertEqual(meta["caption"], "")
        self.assertEqual(meta["cover_url"], "https://ci.xiaohongshu.com/right.jpg")
        self.assertEqual(meta["duration"], "02:12")
        self.assertEqual(meta["likes"], 14)
        self.assertEqual(meta["comments"], 2)
        self.assertEqual(meta["shares"], 1)
        self.assertEqual(meta["published_at"], "2026年06月06日19时03分40秒")
        self.assertEqual(meta["media_url"], "https://sns-video-v4.xhscdn.com/target.mp4")

    def test_xiaohongshu_missing_metadata_is_reported_after_transcription(self):
        collector = load_collector()

        self.assertEqual(
            collector.metadata_quality_message(
                {
                    "platform": "小红书",
                    "content_type": "video",
                    "title": "视频标题",
                    "cover_url": "https://example.com/cover.jpg",
                    "media_url": "https://example.com/video.mp4",
                    "duration": "01:00",
                    "likes": None,
                    "comments": None,
                    "shares": None,
                    "published_at": "",
                }
            ),
            "小红书页面未暴露或未解析到：点赞、评论、分享、发布时间。",
        )

    def test_attachment_parent_node_uses_wiki_obj_token_when_available(self):
        collector = load_collector()
        calls = []

        def fake_http_json(method, url, **kwargs):
            calls.append((method, url, kwargs))
            return 200, {
                "code": 0,
                "data": {
                    "node": {
                        "obj_type": "bitable",
                        "obj_token": "real_bitable_token",
                    }
                },
            }

        collector.http_json = fake_http_json
        cfg = {
            "feishu": {
                "app_id": "cli_x",
                "app_secret": "secret",
                "app_token": "wiki_node_token",
                "table_id": "tbl_x",
                "base_url": "https://open.feishu.cn",
            }
        }

        collector.tenant_access_token = lambda cfg: "tenant_token"
        self.assertEqual(collector.attachment_parent_node(cfg), "real_bitable_token")
        self.assertIn("/open-apis/wiki/v2/spaces/get_node?token=wiki_node_token", calls[0][1])

    def test_transcribe_from_meta_removes_temp_media_dir_when_asr_fails(self):
        collector = load_collector()
        temp_root = Path(tempfile.mkdtemp(prefix="collector-test-"))
        media_path = temp_root / "media.mp4"
        media_path.write_bytes(b"fake video")
        audio_path = temp_root / "audio.mp3"

        collector.download_media_file = lambda url, cfg, platform: media_path
        collector.extract_audio_file = lambda path: audio_path

        def fail_asr(cfg, path):
            raise RuntimeError("ASR失败")

        collector.transcribe_audio_file = fail_asr

        with self.assertRaisesRegex(RuntimeError, "ASR失败"):
            collector.transcribe_from_meta({}, {"media_url": "https://example.com/video.mp4", "platform": "抖音"})

        self.assertFalse(temp_root.exists())

    def test_classify_processing_error_uses_actionable_statuses(self):
        collector = load_collector()

        self.assertEqual(collector.classify_processing_error(RuntimeError("yt-dlp 需要登录 Cookie")), "需Cookie")
        self.assertEqual(collector.classify_processing_error(RuntimeError("Sign in to confirm you’re not a bot")), "需登录")
        self.assertEqual(collector.classify_processing_error(RuntimeError("YouTube 要求登录验证")), "需登录")
        self.assertEqual(collector.classify_processing_error(RuntimeError("抖音要求刷新登录态")), "需登录")
        self.assertEqual(collector.classify_processing_error(RuntimeError("等待登录：请完成登录")), "等待登录")
        self.assertEqual(collector.classify_processing_error(RuntimeError("这是图文作品，没有视频音频")), "图文作品")
        self.assertEqual(collector.classify_processing_error(RuntimeError("未拿到视频/音频直链")), "平台限制")
        self.assertEqual(collector.classify_processing_error(RuntimeError("音频流为空 no audio stream")), "无音频")
        self.assertEqual(collector.classify_processing_error(RuntimeError("ffmpeg 抽取音频失败")), "下载失败")
        self.assertEqual(collector.classify_processing_error(RuntimeError("OpenAI 转写失败 HTTP 500")), "ASR失败")
        self.assertEqual(collector.classify_processing_error(RuntimeError("本机没有找到 yt-dlp")), "yt-dlp缺失")
        self.assertEqual(collector.classify_processing_error(RuntimeError("YouTube 没有可用字幕")), "字幕缺失")
        self.assertEqual(collector.classify_processing_error(RuntimeError("其它错误")), "待人工确认")

    def test_classify_browser_errors_are_not_login_errors(self):
        collector = load_collector()

        self.assertEqual(
            collector.classify_processing_error(RuntimeError("浏览器连接异常：真实浏览器页面已关闭")),
            "浏览器连接异常",
        )
        self.assertEqual(
            collector.classify_processing_error(RuntimeError("真实浏览器已尝试启动，但 http://127.0.0.1:9223 没有就绪。")),
            "浏览器未就绪",
        )
        self.assertEqual(
            collector.classify_processing_error(RuntimeError("connect_over_cdp failed")),
            "浏览器未就绪",
        )
        self.assertEqual(
            collector.classify_processing_error(RuntimeError("等待登录：请完成登录")),
            "等待登录",
        )

    def test_build_update_fields_does_not_write_empty_caption(self):
        collector = load_collector()
        cfg = {"fields": collector.DEFAULT_FIELDS}
        field_types = {name: 1 for name in collector.DEFAULT_FIELDS.values()}

        fields = collector.build_update_fields(
            cfg,
            {
                "platform": "抖音",
                "title": "有标题",
                "caption": "",
                "duration": "",
                "published_at": "",
            },
            field_types,
        )

        self.assertNotIn("文案", fields)

    def test_process_record_waits_for_login_when_only_title_is_available(self):
        collector = load_collector()
        updates = []
        cfg = {"fields": collector.DEFAULT_FIELDS}
        field_types = {name: 1 for name in collector.DEFAULT_FIELDS.values()}

        collector.extract_from_page = lambda url, cfg: {
            "source_url": url,
            "final_url": url,
            "platform": "抖音",
            "title": "分享文本里的标题",
            "caption": "",
            "cover_url": "",
            "duration": "01:00",
            "likes": None,
            "comments": None,
            "shares": None,
            "published_at": "",
            "media_url": "",
        }
        collector.update_record = lambda cfg, record_id, fields: updates.append(fields)
        collector.transcribe_from_meta = lambda cfg, meta: (_ for _ in ()).throw(AssertionError("should not transcribe"))

        result = collector.process_record(
            {"record_id": "rec123", "fields": {"作品链接": "https://www.douyin.com/video/123"}},
            cfg,
            field_types,
            transcribe=True,
        )

        self.assertEqual(result, "metadata_incomplete")
        merged = {k: v for update in updates for k, v in update.items()}
        self.assertEqual(merged["抓取状态"], "等待登录")
        self.assertIn("未抓到封面", merged["错误信息"])
        self.assertEqual(merged["文案"], "")
        self.assertEqual(merged["封面图链接"], "")
        self.assertEqual(merged["时长"], "")
        self.assertEqual(merged["发布时间"], "")

    def test_process_record_does_not_downgrade_existing_caption_to_pending(self):
        collector = load_collector()
        updates = []
        cfg = {"fields": collector.DEFAULT_FIELDS}
        field_types = {name: 1 for name in collector.DEFAULT_FIELDS.values()}

        collector.extract_from_page = lambda url, cfg: {
            "source_url": url,
            "final_url": url,
            "platform": "抖音",
            "title": "真实标题",
            "caption": "",
            "cover_url": "https://example.com/cover.jpg",
            "duration": "01:23",
            "likes": 1,
            "comments": 2,
            "shares": 3,
            "published_at": "2026-06-27 12:00",
            "media_url": "https://example.com/video.mp4",
        }
        collector.update_record = lambda cfg, record_id, fields: updates.append(fields)
        collector.transcribe_from_meta = lambda cfg, meta: (_ for _ in ()).throw(AssertionError("should not transcribe"))
        collector.upload_cover_to_feishu = lambda cfg, cover_url, platform: None

        result = collector.process_record(
            {"record_id": "rec123", "fields": {"作品链接": "https://www.douyin.com/video/123", "文案": "已有逐字稿"}},
            cfg,
            field_types,
            transcribe=False,
        )

        merged = {k: v for update in updates for k, v in update.items()}
        self.assertEqual(result, "metadata_synced")
        self.assertEqual(merged["抓取状态"], "成功")

    def test_douyin_detail_to_meta_extracts_browser_api_payload(self):
        collector = load_collector()

        meta = collector.douyin_detail_to_meta(
            "https://www.douyin.com/video/123",
            "https://www.douyin.com/video/123",
            {
                "desc": "第1集｜VibeCoding大赏 #AI教程",
                "create_time": 1782540050,
                "statistics": {
                    "digg_count": 807,
                    "comment_count": 55,
                    "share_count": 148,
                },
                "video": {
                    "duration": 528000,
                    "cover": {"url_list": ["https://example.com/cover.jpg"]},
                    "play_addr": {"url_list": ["https://example.com/video.mp4"]},
                },
            },
        )

        self.assertEqual(meta["platform"], "抖音")
        self.assertEqual(meta["content_type"], "video")
        self.assertEqual(meta["title"], "第1集｜VibeCoding大赏 #AI教程")
        self.assertEqual(meta["cover_url"], "https://example.com/cover.jpg")
        self.assertEqual(meta["duration"], "08:48")
        self.assertEqual(meta["likes"], 807)
        self.assertEqual(meta["comments"], 55)
        self.assertEqual(meta["shares"], 148)
        self.assertEqual(meta["media_url"], "https://example.com/video.mp4")

    def test_visible_published_at_reads_douyin_page_text(self):
        collector = load_collector()

        self.assertEqual(
            collector.visible_published_at("举报\\n发布时间：2026-05-14 11:58\\n评论"),
            "2026-05-14 11:58",
        )

    def test_douyin_with_only_share_title_still_uses_real_browser(self):
        collector = load_collector()

        self.assertTrue(
            collector.should_try_browser_fallback(
                "抖音",
                {
                    "platform": "抖音",
                    "title": "分享文本里的标题",
                    "caption": "",
                    "cover_url": "",
                    "likes": None,
                    "comments": None,
                    "shares": None,
                    "media_url": "",
                },
            )
        )

    def test_douyin_uses_browser_api_before_opening_real_page(self):
        collector = load_collector()
        calls = []

        collector.detect_platform = lambda url: "抖音"
        collector.extract_douyin_api = lambda url, cfg: {}
        collector.fetch_text = lambda url, cfg, platform: ("<html></html>", "https://www.douyin.com/video/123")
        collector.extract_from_html = lambda url, text, final_url, platform: {
            "source_url": url,
            "final_url": final_url,
            "platform": platform,
            "title": "分享文本标题",
            "caption": "",
            "cover_url": "",
            "duration": "",
            "likes": None,
            "comments": None,
            "shares": None,
            "published_at": "",
            "media_url": "",
        }
        collector.extract_douyin_with_browser_api = lambda url, cfg: {
            "source_url": url,
            "final_url": url,
            "platform": "抖音",
            "title": "浏览器接口标题",
            "caption": "",
            "cover_url": "https://example.com/cover.jpg",
            "duration": "01:23",
            "likes": 1,
            "comments": 2,
            "shares": 3,
            "published_at": "2026-06-27 12:00",
            "media_url": "https://example.com/video.mp4",
        }

        def fake_real_browser(url, cfg):
            calls.append(url)
            raise AssertionError("should not open target page when browser api has enough data")

        collector.extract_with_real_browser = fake_real_browser

        meta = collector.extract_from_page("https://v.douyin.com/abc/", {"browser_fallback": {"enabled": True}})

        self.assertEqual(calls, [])
        self.assertEqual(meta["cover_url"], "https://example.com/cover.jpg")
        self.assertEqual(meta["media_url"], "https://example.com/video.mp4")
        self.assertEqual(meta["likes"], 1)

    def test_usable_browser_title_rejects_edge_placeholder_titles(self):
        collector = load_collector()

        self.assertEqual(collector.usable_browser_title("PC Tab"), "")
        self.assertEqual(collector.usable_browser_title("开启读屏标签"), "")
        self.assertEqual(collector.usable_browser_title("视频数据加载中"), "")
        self.assertEqual(collector.usable_browser_title("2026 ©"), "")
        self.assertEqual(collector.usable_browser_title("00:01 / 03:01"), "")
        self.assertEqual(collector.usable_browser_title("京ICP备16016397号-3"), "")
        self.assertEqual(collector.usable_browser_title("安全验证"), "")
        self.assertEqual(collector.usable_browser_title("真实作品标题"), "真实作品标题")

    def test_extract_from_html_does_not_keep_pc_tab_as_title(self):
        collector = load_collector()

        meta = collector.extract_from_html(
            "https://www.douyin.com/video/123",
            "<html><head><title>PC Tab</title></head><body></body></html>",
            "https://www.douyin.com/video/123",
            "抖音",
        )

        self.assertEqual(meta["title"], "")

    def test_extract_from_html_does_not_keep_subtitle_config_as_caption(self):
        collector = load_collector()

        meta = collector.extract_from_html(
            "https://www.douyin.com/video/123",
            "<script>{\"captionText\":{\"cache_switch\":false,\"language_list\":[{\"language_code\":\"zh-Hans-CN\"}]}}</script>",
            "https://www.douyin.com/video/123",
            "抖音",
        )

        self.assertEqual(meta["caption"], "")

    def test_browser_fallback_uses_cdp_defaults(self):
        collector = load_collector()
        cfg = {"browser_fallback": {}}

        fallback_cfg = collector.browser_fallback_config(cfg)

        self.assertEqual(fallback_cfg["remote_debugging_port"], 9223)
        self.assertTrue(fallback_cfg["keep_open"])
        self.assertIn("browser-profile-cdp", fallback_cfg["profile_dir"])

    def test_cdp_endpoint_uses_configured_port(self):
        collector = load_collector()

        self.assertEqual(
            collector.cdp_endpoint({"remote_debugging_port": 9333}),
            "http://127.0.0.1:9333",
        )

    def test_connect_cdp_browser_uses_short_timeout(self):
        collector = load_collector()
        calls = []

        class Chromium:
            def connect_over_cdp(self, endpoint, **kwargs):
                calls.append((endpoint, kwargs))
                return "browser"

        class Playwright:
            chromium = Chromium()

        result = collector.connect_cdp_browser(
            Playwright(),
            {"remote_debugging_port": 9333, "timeout": 60},
        )

        self.assertEqual(result, "browser")
        self.assertEqual(calls[0][0], "http://127.0.0.1:9333")
        self.assertLessEqual(calls[0][1]["timeout"], 10000)

    def test_connect_cdp_browser_with_recovery_relaunches_once(self):
        collector = load_collector()
        calls = []

        class Playwright:
            pass

        def fake_connect(playwright, fallback_cfg):
            calls.append("connect")
            if calls.count("connect") == 1:
                raise RuntimeError("connection refused")
            return "browser"

        collector.connect_cdp_browser = fake_connect
        collector.stop_cdp_browser = lambda fallback_cfg: calls.append("stop")
        collector.launch_cdp_browser = lambda fallback_cfg, start_url="about:blank": calls.append(("launch", start_url))

        result = collector.connect_cdp_browser_with_recovery(Playwright(), {}, "抖音")

        self.assertEqual(result, "browser")
        self.assertEqual(calls, ["connect", "stop", ("launch", "https://www.douyin.com/"), "connect"])

    def test_cdp_launch_command_runs_browser_binary_directly(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "Google Chrome for Testing.app" / "Contents" / "MacOS" / "Google Chrome for Testing"
            executable.parent.mkdir(parents=True)
            executable.write_text("", encoding="utf-8")
            cfg = {
                "executable_path": str(executable),
                "profile_dir": str(Path(tmp) / "profile"),
                "remote_debugging_port": 9444,
            }

            cmd = collector.cdp_browser_launch_command(cfg, "https://www.douyin.com/")

            self.assertEqual(cmd[0], str(executable))
            self.assertIn("--remote-debugging-port=9444", cmd)
            self.assertIn("https://www.douyin.com/", cmd)

    def test_douyin_falls_back_to_real_browser_when_ytdlp_needs_fresh_cookies(self):
        collector = load_collector()
        calls = []

        collector.detect_platform = lambda url: "抖音"
        collector.extract_douyin_api = lambda url, cfg: {}
        collector.extract_douyin_with_browser_api = lambda url, cfg: {}
        collector.fetch_text = lambda url, cfg, platform: ("<html></html>", url)
        collector.extract_from_html = lambda url, text, final_url, platform: {
            "source_url": url,
            "final_url": final_url,
            "platform": platform,
            "title": "",
            "caption": "",
            "cover_url": "",
            "duration": "",
            "likes": None,
            "comments": None,
            "shares": None,
            "published_at": "",
        }
        collector.extract_with_ytdlp = lambda url, cfg: (_ for _ in ()).throw(RuntimeError("Fresh cookies are needed"))

        def fake_browser(url, cfg):
            calls.append(url)
            return {
                "source_url": url,
                "final_url": url,
                "platform": "抖音",
                "title": "真实浏览器标题",
                "caption": "",
                "cover_url": "https://example.com/cover.jpg",
                "duration": "",
                "likes": 807,
                "comments": 55,
                "shares": 148,
                "published_at": "",
                "media_url": "https://example.com/video.mp4",
            }

        collector.extract_with_real_browser = fake_browser

        meta = collector.extract_from_page("https://www.douyin.com/video/123", {"browser_fallback": {"enabled": True}})

        self.assertEqual(calls, ["https://www.douyin.com/video/123"])
        self.assertEqual(meta["title"], "真实浏览器标题")
        self.assertEqual(meta["cover_url"], "https://example.com/cover.jpg")
        self.assertEqual(meta["media_url"], "https://example.com/video.mp4")

    def test_douyin_browser_detail_retries_until_api_is_ready(self):
        collector = load_collector()

        class Page:
            def __init__(self):
                self.calls = 0
                self.waits = []

            def evaluate(self, script, aweme_id):
                self.calls += 1
                if self.calls == 1:
                    return {"__status": 403}
                if self.calls == 2:
                    return {}
                return {
                    "aweme_detail": {
                        "desc": "登录后可见标题",
                        "create_time": 1783437900,
                        "statistics": {"digg_count": 576, "comment_count": 51, "share_count": 259},
                        "video": {
                            "duration": 134000,
                            "play_addr": {"url_list": ["https://example.com/video.mp4"]},
                            "cover": {"url_list": ["https://example.com/cover.jpg"]},
                        },
                    }
                }

            def wait_for_timeout(self, ms):
                self.waits.append(ms)

        page = Page()

        detail = collector.fetch_douyin_detail_from_browser_page(page, "7535823852908121353", attempts=4, wait_ms=100)

        self.assertEqual(detail["desc"], "登录后可见标题")
        self.assertEqual(page.calls, 3)
        self.assertEqual(page.waits, [100, 200])

    def test_douyin_signed_365yg_media_url_is_valid_video_url(self):
        collector = load_collector()
        url = (
            "https://v5-se-ex-mc-default.365yg.com/c2abee/video/tos/cn/example/"
            "?mime_type=video_mp4&dy_q=1783442803"
        )

        self.assertEqual(collector.media_url_or_empty({"media_url": url}), url)

    def test_douyin_share_url_canonicalizes_to_web_video_url(self):
        collector = load_collector()
        url = "https://www.iesdouyin.com/share/video/7658258510953822117/?from=web_code_link"

        self.assertEqual(
            collector.canonical_douyin_video_url(url),
            "https://www.douyin.com/video/7658258510953822117",
        )

    def test_read_page_content_retries_while_page_is_navigating(self):
        collector = load_collector()

        class Page:
            attempts = 0

            def content(self):
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("Page.content: Unable to retrieve content because the page is navigating")
                return "<html>ready</html>"

            def wait_for_timeout(self, ms):
                self.waited = ms

        page = Page()

        self.assertEqual(collector.read_page_content_with_retry(page, attempts=2), "<html>ready</html>")
        self.assertEqual(page.attempts, 2)

    def test_xiaohongshu_uses_real_browser_when_page_metadata_is_incomplete(self):
        collector = load_collector()
        calls = []

        collector.detect_platform = lambda url: "小红书"
        collector.fetch_text = lambda url, cfg, platform: ("<html></html>", url)
        collector.extract_from_html = lambda url, text, final_url, platform: {
            "source_url": url,
            "final_url": final_url,
            "platform": platform,
            "content_type": "video",
            "title": "",
            "caption": "",
            "cover_url": "",
            "duration": "",
            "likes": None,
            "comments": None,
            "shares": None,
            "published_at": "",
            "media_url": "",
        }
        collector.extract_xiaohongshu_with_browser_fetch = lambda url, cfg: {}

        def fake_browser(url, cfg):
            calls.append(url)
            return {
                "source_url": url,
                "final_url": url,
                "platform": "小红书",
                "content_type": "video",
                "title": "小红书真实标题",
                "caption": "",
                "cover_url": "https://example.com/xhs.jpg",
                "duration": "00:12",
                "likes": 12,
                "comments": 3,
                "shares": 1,
                "published_at": "2026年01月01日00时00分00秒",
                "media_url": "https://example.com/xhs.mp4",
            }

        collector.extract_with_real_browser = fake_browser

        meta = collector.extract_from_page("https://www.xiaohongshu.com/explore/abc", {"browser_fallback": {"enabled": True}})

        self.assertEqual(calls, ["https://www.xiaohongshu.com/explore/abc"])
        self.assertEqual(meta["title"], "小红书真实标题")
        self.assertEqual(meta["media_url"], "https://example.com/xhs.mp4")

    def test_xiaohongshu_title_only_still_needs_browser_fallback(self):
        collector = load_collector()

        self.assertTrue(
            collector.should_try_browser_fallback(
                "小红书",
                {
                    "platform": "小红书",
                    "content_type": "video",
                    "title": "小红书标题",
                    "caption": "",
                    "cover_url": "",
                    "duration": "",
                    "likes": None,
                    "comments": None,
                    "shares": None,
                    "published_at": "",
                    "media_url": "",
                },
            )
        )

    def test_xiaohongshu_uses_browser_fetch_before_opening_real_page(self):
        collector = load_collector()
        real_browser_calls = []

        collector.detect_platform = lambda url: "小红书"
        collector.fetch_text = lambda url, cfg, platform: ("<html><title>小红书标题</title></html>", url)
        collector.extract_from_html = lambda url, text, final_url, platform: {
            "source_url": url,
            "final_url": final_url,
            "platform": platform,
            "content_type": "video",
            "title": "小红书标题",
            "caption": "",
            "cover_url": "",
            "duration": "",
            "likes": None,
            "comments": None,
            "shares": None,
            "published_at": "",
            "media_url": "",
        }
        collector.extract_xiaohongshu_with_browser_fetch = lambda url, cfg: {
            "source_url": url,
            "final_url": url,
            "platform": "小红书",
            "content_type": "video",
            "title": "浏览器登录态标题",
            "caption": "",
            "cover_url": "https://example.com/xhs-cover.jpg",
            "duration": "00:45",
            "likes": 10,
            "comments": 2,
            "shares": 1,
            "published_at": "2026年06月27日12时00分00秒",
            "media_url": "https://example.com/xhs-video.mp4",
        }

        def fake_real_browser(url, cfg):
            real_browser_calls.append(url)
            raise AssertionError("should not open target page when browser fetch has enough data")

        collector.extract_with_real_browser = fake_real_browser

        meta = collector.extract_from_page("https://www.xiaohongshu.com/explore/abc", {"browser_fallback": {"enabled": True}})

        self.assertEqual(real_browser_calls, [])
        self.assertEqual(meta["cover_url"], "https://example.com/xhs-cover.jpg")
        self.assertEqual(meta["media_url"], "https://example.com/xhs-video.mp4")
        self.assertEqual(meta["likes"], 10)


if __name__ == "__main__":
    unittest.main()
