import importlib.util
import json
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent
DEPLOY_PATH = ROOT / "scripts" / "deploy_desktop_app.py"


def load_deployer():
    spec = importlib.util.spec_from_file_location("deploy_desktop_app", DEPLOY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(repo),
            "GIT_CONFIG_NOSYSTEM": "1",
        },
    )
    return result.stdout.strip()


def make_source(root: Path) -> Path:
    source = root / "source"
    source.mkdir()
    (source / "content_link_collector.py").write_text("VERSION = 'new'\n", encoding="utf-8")
    publisher = source / "max_daily_cloud" / "publisher"
    publisher.mkdir(parents=True)
    (publisher / "local_publish_jobs.py").write_text("VALUE = 'new'\n", encoding="utf-8")
    git(source, "init", "-b", "master")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.com")
    git(source, "add", ".")
    git(source, "commit", "-m", "initial")
    return source


def write_fake_app(app_path: Path, marker: str) -> Path:
    contents = app_path / "Contents"
    executable = contents / "MacOS" / "CHEN内容采集助手"
    resources = contents / "Resources"
    executable.parent.mkdir(parents=True)
    resources.mkdir(parents=True)
    executable.write_text(marker, encoding="utf-8")
    (resources / "orange-app-icon.icns").write_bytes(b"icon")
    (contents / "Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": "com.chen.content-collector.native",
                "ChenSourceCommit": marker,
            }
        )
    )
    (app_path / "marker.txt").write_text(marker, encoding="utf-8")
    return app_path


def fake_build_native_app(_source_root: Path, output_app: Path, commit: str) -> Path:
    return write_fake_app(output_app, commit)


class DeployDesktopAppTests(unittest.TestCase):
    def test_verification_covers_local_publisher_and_cloud_code(self):
        deployer = load_deployer()
        source = Path("/tmp/source")

        with mock.patch.object(deployer.subprocess, "run") as run:
            deployer.run_verification(source)

        env = run.call_args_list[0].kwargs["env"]
        self.assertIn("PYTHONPYCACHEPREFIX", env)
        self.assertEqual(
            run.call_args_list,
            [
                mock.call(
                    [deployer.sys.executable, "-m", "unittest", "-v"],
                    cwd=source,
                    env=env,
                    check=True,
                ),
                mock.call(
                    [deployer.sys.executable, "-m", "unittest", "-v", "test_build_desktop_app.py"],
                    cwd=source,
                    env=env,
                    check=True,
                ),
                mock.call(
                    [
                        deployer.sys.executable,
                        "-m",
                        "unittest",
                        "-v",
                        "publisher/test_media_proxy.py",
                        "publisher/test_max_daily_publisher.py",
                        "publisher/test_local_publish_jobs.py",
                    ],
                    cwd=source / "max_daily_cloud",
                    env=env,
                    check=True,
                ),
                mock.call(
                    ["npm", "test"],
                    cwd=source / "max_daily_cloud",
                    env=env,
                    check=True,
                ),
                mock.call(
                    ["npm", "run", "typecheck"],
                    cwd=source / "max_daily_cloud",
                    env=env,
                    check=True,
                ),
            ],
        )

    def test_deploy_rejects_non_master_commit(self):
        deployer = load_deployer()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_source(root)
            git(source, "switch", "-c", "feature")
            (source / "content_link_collector.py").write_text("VERSION = 'feature'\n", encoding="utf-8")
            git(source, "add", ".")
            git(source, "commit", "-m", "feature")

            with self.assertRaisesRegex(RuntimeError, "master"):
                deployer.assert_source_matches_master(source)

    def test_deploy_writes_manifest_and_atomic_script(self):
        deployer = load_deployer()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_source(root)
            install = root / "install"
            app_path = root / "Desktop" / "CHEN 内容采集助手.app"
            install.mkdir()
            (install / "content_link_collector.py").write_text("VERSION = 'old'\n", encoding="utf-8")

            with mock.patch.object(deployer, "build_native_app", side_effect=fake_build_native_app):
                result = deployer.deploy(
                    source,
                    install,
                    app_path=app_path,
                    restart=False,
                    verify=False,
                    health_check=lambda: True,
                )

            self.assertEqual((install / "content_link_collector.py").read_text(encoding="utf-8"), "VERSION = 'new'\n")
            manifest = json.loads((install / "deployment_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["commit"], git(source, "rev-parse", "HEAD"))
            self.assertEqual(manifest["script_sha256"], deployer.sha256_file(install / "content_link_collector.py"))
            self.assertEqual(result, manifest)
            self.assertTrue(Path(manifest["backup_path"]).is_dir())
            self.assertEqual((app_path / "marker.txt").read_text(encoding="utf-8"), manifest["commit"])

    def test_deploy_lock_rejects_concurrent_release(self):
        deployer = load_deployer()
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / ".deploy.lock"
            with deployer.exclusive_deploy_lock(lock_path):
                with self.assertRaisesRegex(RuntimeError, "正在部署"):
                    with deployer.exclusive_deploy_lock(lock_path):
                        pass

    def test_failed_health_check_restores_backup(self):
        deployer = load_deployer()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_source(root)
            install = root / "install"
            app_path = root / "Desktop" / "CHEN 内容采集助手.app"
            publisher = install / "max_daily_cloud" / "publisher"
            publisher.mkdir(parents=True)
            (install / "content_link_collector.py").write_text("VERSION = 'old'\n", encoding="utf-8")
            (publisher / "local_publish_jobs.py").write_text("VALUE = 'old'\n", encoding="utf-8")
            write_fake_app(app_path, "old-app")

            with mock.patch.object(deployer, "build_native_app", side_effect=fake_build_native_app):
                with self.assertRaisesRegex(RuntimeError, "健康检查失败"):
                    deployer.deploy(
                        source,
                        install,
                        app_path=app_path,
                        restart=False,
                        verify=False,
                        health_check=lambda: False,
                    )

            self.assertEqual((install / "content_link_collector.py").read_text(encoding="utf-8"), "VERSION = 'old'\n")
            self.assertEqual((publisher / "local_publish_jobs.py").read_text(encoding="utf-8"), "VALUE = 'old'\n")
            self.assertEqual((app_path / "marker.txt").read_text(encoding="utf-8"), "old-app")

    def test_deploy_disables_legacy_service_and_opens_native_app(self):
        deployer = load_deployer()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_source(root)
            install = root / "install"
            app_path = root / "Desktop" / "CHEN 内容采集助手.app"
            state = deployer.LegacyServiceState(enabled=True, running=True)

            with mock.patch.object(deployer, "build_native_app", side_effect=fake_build_native_app), mock.patch.object(
                deployer, "capture_legacy_service_state", return_value=state
            ), mock.patch.object(deployer, "disable_legacy_service") as disable, mock.patch.object(
                deployer, "open_native_app"
            ) as open_app:
                deployer.deploy(
                    source,
                    install,
                    app_path=app_path,
                    restart=True,
                    verify=False,
                    health_check=lambda: True,
                )

            disable.assert_called_once_with(state)
            open_app.assert_called_once_with(app_path.resolve())

    def test_capture_legacy_service_state_accepts_modern_disabled_output(self):
        deployer = load_deployer()
        results = [
            subprocess.CompletedProcess([], returncode=1, stdout="", stderr=""),
            subprocess.CompletedProcess(
                [],
                returncode=0,
                stdout=(
                    'disabled services = {\n'
                    f'\t"{deployer.LAUNCH_AGENT_LABEL}" => disabled\n'
                    '}\n'
                ),
                stderr="",
            ),
        ]

        with mock.patch.object(deployer.subprocess, "run", side_effect=results):
            state = deployer.capture_legacy_service_state()

        self.assertEqual(state, deployer.LegacyServiceState(enabled=False, running=False))

    def test_disable_legacy_service_only_targets_desktop_label(self):
        deployer = load_deployer()
        state = deployer.LegacyServiceState(enabled=True, running=True)

        with mock.patch.object(deployer.subprocess, "run") as run:
            deployer.disable_legacy_service(state)

        commands = [call.args[0] for call in run.call_args_list]
        self.assertTrue(commands)
        self.assertTrue(all(deployer.LAUNCH_AGENT_LABEL in " ".join(command) for command in commands))
        self.assertFalse(any("event-listener" in " ".join(command) for command in commands))

    def test_disable_legacy_service_disables_before_bootout(self):
        deployer = load_deployer()
        state = deployer.LegacyServiceState(enabled=True, running=True)

        with mock.patch.object(deployer.subprocess, "run") as run:
            deployer.disable_legacy_service(state)

        service = f"gui/{deployer.os.getuid()}/{deployer.LAUNCH_AGENT_LABEL}"
        self.assertEqual(
            run.call_args_list,
            [
                mock.call(["launchctl", "disable", service], check=True),
                mock.call(["launchctl", "bootout", service], check=True),
            ],
        )

    def test_failed_running_deploy_restores_app_and_legacy_service(self):
        deployer = load_deployer()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_source(root)
            install = root / "install"
            app_path = root / "Desktop" / "CHEN 内容采集助手.app"
            install.mkdir()
            (install / "content_link_collector.py").write_text("VERSION = 'old'\n", encoding="utf-8")
            write_fake_app(app_path, "old-app")
            state = deployer.LegacyServiceState(enabled=True, running=True)

            with mock.patch.object(deployer, "build_native_app", side_effect=fake_build_native_app), mock.patch.object(
                deployer, "capture_legacy_service_state", return_value=state
            ), mock.patch.object(deployer, "disable_legacy_service"), mock.patch.object(
                deployer, "restore_legacy_service"
            ) as restore, mock.patch.object(deployer, "open_native_app"):
                with self.assertRaisesRegex(RuntimeError, "健康检查失败"):
                    deployer.deploy(
                        source,
                        install,
                        app_path=app_path,
                        restart=True,
                        verify=False,
                        health_check=lambda: False,
                    )

            self.assertEqual((app_path / "marker.txt").read_text(encoding="utf-8"), "old-app")
            self.assertEqual((install / "content_link_collector.py").read_text(encoding="utf-8"), "VERSION = 'old'\n")
            restore.assert_called_once_with(state)

    def test_atomic_app_install_replaces_complete_bundle(self):
        deployer = load_deployer()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = write_fake_app(root / "candidate.app", "new")
            target = write_fake_app(root / "CHEN 内容采集助手.app", "old")

            deployer.atomic_install_app(candidate, target)

            self.assertEqual((target / "marker.txt").read_text(encoding="utf-8"), "new")
            self.assertFalse(candidate.exists())
            self.assertEqual(list(root.glob("*.previous")), [])

    def test_runtime_ownership_accepts_app_parent_and_disabled_legacy_service(self):
        deployer = load_deployer()
        app_path = Path("/Users/test/Desktop/CHEN 内容采集助手.app")
        install_root = Path("/Users/test/Library/Application Support/ChenContentLinkCollector")
        processes = "\n".join(
            [
                "  501     1 /Users/test/Desktop/CHEN 内容采集助手.app/Contents/MacOS/CHEN内容采集助手",
                "  502   501 /usr/bin/python3 /Users/test/Library/Application Support/ChenContentLinkCollector/content_link_collector.py desktop-app --host 127.0.0.1 --port 51216",
            ]
        )

        deployer.validate_runtime_ownership(
            processes,
            app_path,
            install_root,
            deployer.LegacyServiceState(enabled=False, running=False),
        )

    def test_runtime_ownership_rejects_python_not_parented_by_app(self):
        deployer = load_deployer()
        app_path = Path("/Users/test/Desktop/CHEN 内容采集助手.app")
        install_root = Path("/Users/test/Library/Application Support/ChenContentLinkCollector")
        processes = "\n".join(
            [
                "  501     1 /Users/test/Desktop/CHEN 内容采集助手.app/Contents/MacOS/CHEN内容采集助手",
                "  502     1 /usr/bin/python3 /Users/test/Library/Application Support/ChenContentLinkCollector/content_link_collector.py desktop-app --host 127.0.0.1 --port 51216",
            ]
        )

        with self.assertRaisesRegex(RuntimeError, "父进程"):
            deployer.validate_runtime_ownership(
                processes,
                app_path,
                install_root,
                deployer.LegacyServiceState(enabled=False, running=False),
            )

    def test_runtime_ownership_rejects_enabled_legacy_service(self):
        deployer = load_deployer()

        with self.assertRaisesRegex(RuntimeError, "LaunchAgent"):
            deployer.validate_runtime_ownership(
                "",
                Path("/tmp/App.app"),
                Path("/tmp/install"),
                deployer.LegacyServiceState(enabled=True, running=False),
            )

    def test_version_payload_reads_deployment_manifest(self):
        from test_webhook_helpers import load_collector

        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "deployment_manifest.json"
            manifest_path.write_text(
                json.dumps({"commit": "abc123", "script_sha256": "sha256"}),
                encoding="utf-8",
            )

            payload = collector.desktop_version_payload(manifest_path)

            self.assertEqual(payload["commit"], "abc123")
            self.assertEqual(payload["script_sha256"], "sha256")
            self.assertTrue(payload["ok"])


if __name__ == "__main__":
    unittest.main()
