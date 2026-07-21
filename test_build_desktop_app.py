import importlib.util
import json
import os
import platform
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent
BUILDER_PATH = ROOT / "scripts" / "build_desktop_app.py"


def load_builder():
    spec = importlib.util.spec_from_file_location("build_desktop_app", BUILDER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_fixture(source_root: Path, *, bundle_id: str = "com.chen.content-collector.native") -> None:
    desktop_root = source_root / "desktop_app"
    resources = desktop_root / "Resources"
    resources.mkdir(parents=True)
    (desktop_root / "AppMain.swift").write_text("print(\"fixture\")\n", encoding="utf-8")
    (resources / "orange-app-icon.icns").write_bytes(b"fixture-icon")
    (desktop_root / "Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleDisplayName": "CHEN 内容采集助手",
                "CFBundleExecutable": "CHEN内容采集助手",
                "CFBundleIdentifier": bundle_id,
                "CFBundleName": "CHEN 内容采集助手",
                "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": "1.0",
                "CFBundleVersion": "1",
                "LSMinimumSystemVersion": "10.15",
                "NSDocumentsFolderUsageDescription": "只读访问日报视频。",
            }
        )
    )
    for relative in (
        "content_link_collector.py",
        "max_daily_cloud/publisher/__init__.py",
        "max_daily_cloud/publisher/local_publish_jobs.py",
        "max_daily_cloud/publisher/max_daily_publisher.py",
        "max_daily_cloud/publisher/media_proxy.py",
        "max_daily_cloud/publisher/config.example.json",
    ):
        payload = source_root / relative
        payload.parent.mkdir(parents=True, exist_ok=True)
        payload.write_text(f"fixture: {relative}\n", encoding="utf-8")
    packaging = source_root / "packaging"
    packaging.mkdir()
    (packaging / "runtime-lock.json").write_text('{"fixture": true}\n', encoding="utf-8")
    (packaging / "THIRD_PARTY_NOTICES.md").write_text(
        "# Fixture third-party notices\n", encoding="utf-8"
    )


def write_runtime_fixture(runtime_root: Path) -> None:
    for architecture in ("arm64", "x86_64"):
        runtime = runtime_root / architecture
        python_bin = runtime / "python/bin"
        site_packages = runtime / "python/lib/python3.12/site-packages"
        greenlet = site_packages / "greenlet/_greenlet.cpython-312-darwin.so"
        node = site_packages / "playwright/driver/node"
        ffmpeg = runtime / "tools/ffmpeg"
        python_bin.mkdir(parents=True)
        greenlet.parent.mkdir(parents=True)
        node.parent.mkdir(parents=True)
        ffmpeg.parent.mkdir(parents=True)
        python = python_bin / "python3.12"
        python.write_bytes(b"\xcf\xfa\xed\xfe" + architecture.encode())
        (python_bin / "python3").symlink_to("python3.12")
        node.write_bytes(b"\xcf\xfa\xed\xfe" + architecture.encode())
        greenlet.write_bytes(b"\xca\xfe\xba\xbeuniversal2")
        ffmpeg.write_bytes(b"\xcf\xfa\xed\xfe" + architecture.encode())
        for executable in (python, node, ffmpeg):
            executable.chmod(0o755)


def write_real_swift_fixture(source_root: Path) -> None:
    write_fixture(source_root)
    desktop_root = source_root / "desktop_app"
    for fixture_source in ROOT.joinpath("desktop_app").glob("*.swift"):
        shutil.copyfile(fixture_source, desktop_root / fixture_source.name)


def build_fixture_app(builder, source: Path, output: Path, runtime_root: Path) -> Path:
    with mock.patch.object(builder, "compile_swift", side_effect=fake_compile), mock.patch.object(
        builder, "sign_app"
    ), mock.patch.object(builder, "validate_runtime_resources"), mock.patch.object(
        builder, "assert_exact_architectures"
    ):
        return builder.build_app(source, output, "abc123", runtime_root)


def fake_compile(_source_files, executable: Path) -> None:
    executable.write_bytes(b"fixture-executable")


def install_fake_runtime(app_path: Path, architecture: str) -> dict[str, Path]:
    resources = app_path / "Contents" / "Resources"
    runtime = resources / "Runtime" / architecture
    python = runtime / "python" / "bin" / "python3"
    tools = runtime / "tools"
    ffmpeg = tools / "ffmpeg"
    collector = resources / "CollectorPayload" / "content_link_collector.py"
    python.parent.mkdir(parents=True, exist_ok=True)
    tools.mkdir(parents=True, exist_ok=True)
    collector.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    ffmpeg.write_text("#!/bin/sh\n", encoding="utf-8")
    collector.write_text("# fixture collector\n", encoding="utf-8")
    python.chmod(0o755)
    ffmpeg.chmod(0o755)
    return {
        "python": python,
        "tools": tools,
        "collector": collector,
    }


class RecordingCompileRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command, **kwargs):
        command = [str(part) for part in command]
        self.commands.append(command)
        if command[:4] == ["/usr/bin/xcrun", "--sdk", "macosx15.4", "--show-sdk-path"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="/Library/Developer/CommandLineTools/SDKs/MacOSX15.4.sdk\n",
                stderr="",
            )
        if "swiftc" in command:
            Path(command[command.index("-o") + 1]).write_bytes(b"thin-macho")
        elif command[:2] == ["/usr/bin/lipo", "-create"]:
            Path(command[command.index("-output") + 1]).write_bytes(b"universal-macho")
        elif command[:2] == ["/usr/bin/lipo", "-archs"]:
            inspected = Path(command[2]).name
            if inspected.endswith(".arm64"):
                architectures = "arm64\n"
            elif inspected.endswith(".x86_64"):
                architectures = "x86_64\n"
            else:
                architectures = "x86_64 arm64\n"
            return subprocess.CompletedProcess(
                command, 0, stdout=architectures, stderr=""
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def run_local_service_host_harness(body: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        harness = root / "LocalServiceHostBehavior.swift"
        executable = root / "LocalServiceHostBehavior"
        sdk_path = subprocess.check_output(
            ["/usr/bin/xcrun", "--sdk", "macosx15.4", "--show-sdk-path"],
            text=True,
        ).strip()
        host_target = {
            "arm64": "arm64-apple-macos12.0",
            "x86_64": "x86_64-apple-macos12.0",
        }[platform.machine()]
        environment = os.environ.copy()
        environment["SDKROOT"] = sdk_path
        environment["CLANG_MODULE_CACHE_PATH"] = str(root / "clang-module-cache")
        harness.write_text(
            "import Foundation\n\n"
            "@main\n"
            "struct LocalServiceHostBehavior {\n"
            "    static func main() {\n"
            f"{body}\n"
            "    }\n"
            "}\n",
            encoding="utf-8",
        )
        subprocess.run(
            [
                "/usr/bin/xcrun",
                "--sdk",
                "macosx15.4",
                "swiftc",
                "-target",
                host_target,
                "-sdk",
                sdk_path,
                "-module-cache-path",
                str(root / "module-cache"),
                "-framework",
                "Cocoa",
                str(ROOT / "desktop_app" / "RuntimeBootstrap.swift"),
                str(ROOT / "desktop_app" / "LocalServiceHost.swift"),
                str(harness),
                "-o",
                str(executable),
            ],
            check=True,
            env=environment,
        )
        subprocess.run([executable], check=True)


class BuildDesktopAppTests(unittest.TestCase):
    def test_native_app_uses_bundled_runtime_and_user_data_root(self):
        source = (ROOT / "desktop_app" / "RuntimeBootstrap.swift").read_text()

        self.assertIn("Runtime/arm64", source)
        self.assertIn("Runtime/x86_64", source)
        self.assertIn("CollectorPayload/content_link_collector.py", source)
        self.assertIn("CHEN_COLLECTOR_DATA_ROOT", source)

    def test_local_service_host_preserves_inherited_environment_with_configured_overrides(self):
        run_local_service_host_harness(
            """
        let inherited = [
            "PATH": "/usr/local/bin",
            "CHEN_COLLECTOR_DATA_ROOT": "/previous-data",
            "HTTP_PROXY": "http://proxy.example:8080",
            "LANG": "zh_CN.UTF-8",
            "SSH_AUTH_SOCK": "/private/tmp/agent.sock",
            "SECURITYSESSIONID": "keychain-session",
        ]
        let configured = [
            "PATH": "/app/tools:/usr/bin:/bin:/usr/sbin:/sbin",
            "CHEN_COLLECTOR_DATA_ROOT": "/app-data",
        ]
        let environment = LocalServiceHost.mergedEnvironment(
            inherited: inherited,
            overriding: configured
        )

        guard environment["PATH"] == configured["PATH"],
              environment["CHEN_COLLECTOR_DATA_ROOT"] == configured["CHEN_COLLECTOR_DATA_ROOT"],
              environment["HTTP_PROXY"] == inherited["HTTP_PROXY"],
              environment["LANG"] == inherited["LANG"],
              environment["SSH_AUTH_SOCK"] == inherited["SSH_AUTH_SOCK"],
              environment["SECURITYSESSIONID"] == inherited["SECURITYSESSIONID"] else {
            fatalError("merged environment did not preserve inherited values and configured overrides")
        }
        """
        )

    def test_local_service_host_recognizes_only_collector_desktop_app_commands(self):
        run_local_service_host_harness(
            """
        let currentPython = "/Applications/CHEN 内容采集助手.app/Contents/Resources/Runtime/arm64/python/bin/python3"
        let currentScript = "/Applications/CHEN 内容采集助手.app/Contents/Resources/CollectorPayload/content_link_collector.py"
        let host = LocalServiceHost(
            configuration: ServiceConfiguration(
                executable: currentPython,
                arguments: [currentScript, "desktop-app"],
                workingDirectory: "/tmp",
                logPath: "/tmp/desktop-app.log",
                environment: [:]
            )
        )
        let legacyScript = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/ChenContentLinkCollector/content_link_collector.py")
            .path
        let currentCollector = "\\(currentPython) \\(currentScript) desktop-app --host 127.0.0.1 --port 51216"
        let quotedCurrentCollector = "\\(currentPython) \\\"\\(currentScript)\\\" desktop-app --port 51216"
        let legacyCollector = "/usr/bin/python3 \\(legacyScript) desktop-app --host 127.0.0.1 --port 51216"
        let quotedLegacyCollector = "/usr/bin/python3 '\\(legacyScript)' desktop-app --port 51216"
        let reviewerLabelExample = "/usr/bin/python3 /tmp/unrelated.py --label content_link_collector.py desktop-app --port 51216"
        let unrelatedPython = "/usr/bin/python3 /tmp/unrelated.py desktop-app --port 51216"
        let similarlyNamedScript = "/usr/bin/python3 /tmp/not_content_link_collector.py desktop-app --port 51216"
        let wrongMode = "\\(currentPython) \\(currentScript) worker --port 51216"
        let reorderedDecoy = "\\(currentPython) desktop-app \\(currentScript) --port 51216"
        let labeledCurrentScript = "\\(currentPython) /tmp/unrelated.py --label \\(currentScript) desktop-app --port 51216"
        let similarlyPrefixedCurrentScript = "\\(currentPython) \\(currentScript).backup desktop-app --port 51216"

        guard host.isKnownCollectorCommand(currentCollector),
              host.isKnownCollectorCommand(quotedCurrentCollector),
              host.isKnownCollectorCommand(legacyCollector),
              host.isKnownCollectorCommand(quotedLegacyCollector),
              !host.isKnownCollectorCommand(reviewerLabelExample),
              !host.isKnownCollectorCommand(unrelatedPython),
              !host.isKnownCollectorCommand(similarlyNamedScript),
              !host.isKnownCollectorCommand(wrongMode),
              !host.isKnownCollectorCommand(reorderedDecoy),
              !host.isKnownCollectorCommand(labeledCurrentScript),
              !host.isKnownCollectorCommand(similarlyPrefixedCurrentScript) else {
            fatalError("collector command recognition is too broad or does not support legacy paths")
        }
        """
        )

    def test_compile_swift_builds_both_explicit_targets_with_macos_15_4_sdk(self):
        builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "AppMain.swift"
            executable = root / "CHEN内容采集助手"
            source.write_text("print(\"fixture\")\n", encoding="utf-8")
            runner = RecordingCompileRunner()

            with mock.patch.object(builder.subprocess, "run", side_effect=runner):
                builder.compile_swift([source], executable)
            executable_bytes = executable.read_bytes()

        swift_commands = [command for command in runner.commands if "swiftc" in command]
        self.assertEqual(len(swift_commands), 2)
        self.assertEqual(
            {command[command.index("-target") + 1] for command in swift_commands},
            {"arm64-apple-macos12.0", "x86_64-apple-macos12.0"},
        )
        for command in swift_commands:
            self.assertEqual(command[:4], ["/usr/bin/xcrun", "--sdk", "macosx15.4", "swiftc"])
            self.assertEqual(
                command[command.index("-sdk") + 1],
                "/Library/Developer/CommandLineTools/SDKs/MacOSX15.4.sdk",
            )
        self.assertTrue(
            any(command[:2] == ["/usr/bin/lipo", "-create"] for command in runner.commands)
        )
        self.assertEqual(executable_bytes, b"universal-macho")

    def test_exact_architecture_validation_rejects_missing_and_extra_slices(self):
        builder = load_builder()
        executable = Path("/tmp/CHEN内容采集助手")

        for actual in ("arm64\n", "x86_64 arm64 ppc\n"):
            with self.subTest(actual=actual), mock.patch.object(
                builder.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, stdout=actual, stderr=""),
            ):
                with self.assertRaisesRegex(RuntimeError, "architecture"):
                    builder.assert_exact_architectures(executable, {"arm64", "x86_64"})

    def test_sign_app_signs_nested_macho_inside_out_then_app_and_verifies(self):
        builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "CHEN 内容采集助手.app"
            main = app_path / "Contents/MacOS/CHEN内容采集助手"
            node = app_path / "Contents/Resources/Runtime/arm64/python/lib/node"
            greenlet = (
                app_path
                / "Contents/Resources/Runtime/arm64/python/lib/python3.12/site-packages/greenlet/_greenlet.so"
            )
            text = app_path / "Contents/Resources/THIRD_PARTY_NOTICES.md"
            for path in (main, node, greenlet, text):
                path.parent.mkdir(parents=True, exist_ok=True)
            for path in (main, node, greenlet):
                path.write_bytes(b"\xcf\xfa\xed\xfefixture")
            text.write_text("not code\n", encoding="utf-8")

            with mock.patch.object(builder.subprocess, "run") as run:
                builder.sign_app(app_path)

        self.assertEqual(
            run.call_args_list,
            [
                mock.call(
                    ["/usr/bin/codesign", "--force", "--sign", "-", str(greenlet)],
                    check=True,
                ),
                mock.call(
                    ["/usr/bin/codesign", "--force", "--sign", "-", str(node)],
                    check=True,
                ),
                mock.call(
                    ["/usr/bin/codesign", "--force", "--sign", "-", str(main)],
                    check=True,
                ),
                mock.call(
                    ["/usr/bin/codesign", "--force", "--sign", "-", str(app_path)],
                    check=True,
                ),
                mock.call(
                    ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app_path)],
                    check=True,
                ),
            ],
        )

    def test_sign_app_stops_before_bundle_signature_when_nested_signing_fails(self):
        builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "Failure.app"
            nested = app_path / "Contents/Resources/Runtime/arm64/python/bin/python3.12"
            nested.parent.mkdir(parents=True)
            nested.write_bytes(b"\xcf\xfa\xed\xfefixture")
            failure = subprocess.CalledProcessError(1, ["codesign"])

            with mock.patch.object(builder.subprocess, "run", side_effect=failure) as run:
                with self.assertRaises(subprocess.CalledProcessError):
                    builder.sign_app(app_path)

        self.assertEqual(run.call_count, 1)

    def test_build_app_creates_versioned_bundle(self):
        builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            write_fixture(source)
            runtime_root = root / "runtime"
            write_runtime_fixture(runtime_root)
            output = root / "CHEN 内容采集助手.app"

            with mock.patch.object(builder, "compile_swift", side_effect=fake_compile), mock.patch.object(
                builder, "sign_app"
            ) as sign_app, mock.patch.object(builder, "validate_runtime_resources"), mock.patch.object(
                builder, "assert_exact_architectures"
            ):
                result = builder.build_app(source, output, "abc123", runtime_root)

            info = plistlib.loads((result / "Contents/Info.plist").read_bytes())
            self.assertEqual(info["CFBundleIdentifier"], "com.chen.content-collector.native")
            self.assertEqual(info["ChenSourceCommit"], "abc123")
            self.assertEqual(info["CFBundleExecutable"], "CHEN内容采集助手")
            self.assertEqual(info["LSMinimumSystemVersion"], "12.0")
            self.assertEqual((result / "Contents/MacOS/CHEN内容采集助手").read_bytes(), b"fixture-executable")
            self.assertEqual((result / "Contents/Resources/orange-app-icon.icns").read_bytes(), b"fixture-icon")
            self.assertTrue(
                (result / "Contents/Resources/Runtime/arm64/python/bin/python3").is_symlink()
            )
            self.assertTrue(
                (result / "Contents/Resources/Runtime/x86_64/tools/ffmpeg").is_file()
            )
            embedded_runtime = result / "Contents/Resources/Runtime"
            for architecture in ("arm64", "x86_64"):
                source_runtime = runtime_root / architecture
                copied_runtime = embedded_runtime / architecture
                source_entries = {
                    path.relative_to(source_runtime): path
                    for path in source_runtime.rglob("*")
                }
                copied_entries = {
                    path.relative_to(copied_runtime): path
                    for path in copied_runtime.rglob("*")
                }
                self.assertEqual(set(copied_entries), set(source_entries))
                for relative, source_entry in source_entries.items():
                    copied_entry = copied_entries[relative]
                    self.assertEqual(
                        copied_entry.lstat().st_mode & 0o777,
                        source_entry.lstat().st_mode & 0o777,
                    )
                    self.assertEqual(copied_entry.is_symlink(), source_entry.is_symlink())
                    if source_entry.is_symlink():
                        self.assertEqual(os.readlink(copied_entry), os.readlink(source_entry))
            self.assertEqual(
                (result / "Contents/Resources/Runtime/runtime-lock.json").read_text(),
                '{"fixture": true}\n',
            )
            self.assertTrue(
                (result / "Contents/Resources/THIRD_PARTY_NOTICES.md").is_file()
            )
            self.assertTrue(
                all(
                    path.stat().st_mode & 0o777 == 0o644
                    for path in (result / "Contents/Resources/CollectorPayload").rglob("*")
                    if path.is_file()
                )
            )
            sign_app.assert_called_once_with(mock.ANY)

    def test_build_app_retains_previous_bundle_when_install_and_restore_fail(self):
        builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            write_fixture(source)
            runtime_root = root / "runtime"
            write_runtime_fixture(runtime_root)
            output = root / "CHEN 内容采集助手.app"
            output.mkdir()
            old_bundle = b"known-good-bundle"
            (output / "bundle.bin").write_bytes(old_bundle)
            output = output.resolve()
            original_replace = os.replace

            def fail_install_and_restore(source_path, target_path):
                source_path = Path(source_path)
                target_path = Path(target_path)
                if target_path == output:
                    raise OSError(
                        "restore rename failed"
                        if source_path.name.endswith(".previous")
                        else "install rename failed"
                    )
                return original_replace(source_path, target_path)

            with self.assertRaisesRegex(
                RuntimeError,
                "App replacement failed; recovery copy was retained",
            ) as raised, mock.patch.object(
                builder, "compile_swift", side_effect=fake_compile
            ), mock.patch.object(builder, "sign_app"), mock.patch.object(
                builder, "validate_runtime_resources"
            ), mock.patch.object(
                builder, "assert_exact_architectures"
            ), mock.patch.object(
                builder.os,
                "replace",
                side_effect=fail_install_and_restore,
            ):
                builder.build_app(source, output, "abc123", runtime_root)

            previous = list(root.glob(".CHEN 内容采集助手.app.*.previous"))
            self.assertNotIn(str(root), str(raised.exception))
            self.assertIsNone(raised.exception.__cause__)
            self.assertFalse(output.exists())
            self.assertEqual(len(previous), 1)
            self.assertEqual((previous[0] / "bundle.bin").read_bytes(), old_bundle)

    def test_build_app_embeds_only_allowlisted_payload_and_excludes_secrets(self):
        builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            write_fixture(source)
            runtime_root = root / "runtime"
            write_runtime_fixture(runtime_root)
            for relative in (
                "config.json",
                ".env",
                "collector.db",
                "cache/session.json",
                "videos/private.mp4",
                "browser-profile/Cookies",
                "docs/private.md",
                "node_modules/secret/index.js",
                ".git/config",
            ):
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("excluded personal fixture\n", encoding="utf-8")

            app = build_fixture_app(
                builder, source, root / "CHEN 内容采集助手.app", runtime_root
            )
            payload = app / "Contents/Resources/CollectorPayload"
            embedded = {
                path.relative_to(payload)
                for path in payload.rglob("*")
                if path.is_file()
            }

            self.assertEqual(embedded, set(builder.PAYLOAD_FILES))
            resources_text = "\n".join(
                str(path.relative_to(app / "Contents/Resources"))
                for path in (app / "Contents/Resources").rglob("*")
            )
            for forbidden in (
                "config.json",
                ".env",
                "collector.db",
                "private.mp4",
                "Cookies",
                "private.md",
                "node_modules",
                ".git",
            ):
                self.assertNotIn(forbidden, resources_text)

    def test_build_app_rejects_missing_symlink_non_regular_and_escaping_payload(self):
        builder = load_builder()
        cases = ("missing", "symlink", "directory", "escape")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "source"
                source.mkdir()
                write_fixture(source)
                runtime_root = root / "runtime"
                write_runtime_fixture(runtime_root)
                collector = source / "content_link_collector.py"
                patch_payload = mock.patch.object(
                    builder, "PAYLOAD_FILES", (Path("../outside.py"),)
                )
                if case == "missing":
                    collector.unlink()
                    patch_payload = mock.patch.object(
                        builder, "PAYLOAD_FILES", builder.PAYLOAD_FILES
                    )
                elif case == "symlink":
                    collector.unlink()
                    collector.symlink_to("max_daily_cloud/publisher/__init__.py")
                    patch_payload = mock.patch.object(
                        builder, "PAYLOAD_FILES", builder.PAYLOAD_FILES
                    )
                elif case == "directory":
                    collector.unlink()
                    collector.mkdir()
                    patch_payload = mock.patch.object(
                        builder, "PAYLOAD_FILES", builder.PAYLOAD_FILES
                    )
                else:
                    (root / "outside.py").write_text("secret\n", encoding="utf-8")

                with patch_payload, self.assertRaisesRegex(RuntimeError, "payload"):
                    build_fixture_app(
                        builder, source, root / "Failure.app", runtime_root
                    )

    def test_build_app_rejects_missing_runtime_and_unsafe_runtime_symlink(self):
        builder = load_builder()
        for case in ("missing", "unsafe-symlink"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "source"
                source.mkdir()
                write_fixture(source)
                runtime_root = root / "runtime"
                write_runtime_fixture(runtime_root)
                if case == "missing":
                    shutil.rmtree(runtime_root / "x86_64")
                else:
                    node = (
                        runtime_root
                        / "arm64/python/lib/python3.12/site-packages/playwright/driver/node"
                    )
                    node.unlink()
                    node.symlink_to("/tmp/outside-node")

                with self.assertRaisesRegex(RuntimeError, "runtime"):
                    build_fixture_app(
                        builder, source, root / "Failure.app", runtime_root
                    )

    def test_runtime_validation_checks_all_expected_native_resources(self):
        builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp) / "Runtime"
            write_runtime_fixture(runtime_root)

            def architecture_set(path):
                if "greenlet" in str(path):
                    return {"arm64", "x86_64"}
                return {"arm64"} if "/arm64/" in str(path) else {"x86_64"}

            with mock.patch.object(
                builder, "architecture_set", side_effect=architecture_set
            ) as inspect_architecture:
                builder.validate_runtime_resources(runtime_root)

        self.assertEqual(inspect_architecture.call_count, 8)

    def test_runtime_validation_rejects_wrong_component_architecture(self):
        builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp) / "Runtime"
            write_runtime_fixture(runtime_root)
            with mock.patch.object(builder, "architecture_set", return_value={"x86_64"}):
                with self.assertRaisesRegex(RuntimeError, "architecture"):
                    builder.validate_runtime_resources(runtime_root)

    def test_build_app_rejects_missing_swift_sources(self):
        builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            write_fixture(source)
            (source / "desktop_app" / "AppMain.swift").unlink()

            with self.assertRaisesRegex(RuntimeError, "Swift"):
                builder.build_app(source, root / "Missing.app", "abc123")

    def test_build_app_rejects_missing_icon(self):
        builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            write_fixture(source)
            (source / "desktop_app" / "Resources" / "orange-app-icon.icns").unlink()

            with self.assertRaisesRegex(RuntimeError, "图标"):
                builder.build_app(source, root / "Missing.app", "abc123")

    def test_validate_app_bundle_rejects_wrong_bundle_identifier(self):
        builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "Wrong.app"
            contents = app / "Contents"
            executable = contents / "MacOS" / "CHEN内容采集助手"
            resources = contents / "Resources"
            executable.parent.mkdir(parents=True)
            resources.mkdir()
            executable.write_bytes(b"executable")
            (resources / "orange-app-icon.icns").write_bytes(b"icon")
            (contents / "Info.plist").write_bytes(
                plistlib.dumps(
                    {
                        "CFBundleIdentifier": "com.example.wrong",
                        "ChenSourceCommit": "abc123",
                    }
                )
            )

            with self.assertRaisesRegex(RuntimeError, "bundle identifier"):
                builder.validate_app_bundle(app, "abc123")

    def test_built_app_reports_direct_python_service_config(self):
        builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            write_real_swift_fixture(source)
            runtime_root = root / "runtime"
            write_runtime_fixture(runtime_root)
            with mock.patch.object(builder, "validate_runtime_resources"), mock.patch.object(
                builder, "sign_app"
            ):
                app = builder.build_app(
                    source, root / "CHEN 内容采集助手.app", "abc123", runtime_root
                )
            executable = app / "Contents" / "MacOS" / "CHEN内容采集助手"
            runtime = install_fake_runtime(app, platform.machine())
            home = root / "home"
            home.mkdir()
            environment = os.environ.copy()
            environment["CFFIXED_USER_HOME"] = str(home)

            payload = json.loads(
                subprocess.check_output(
                    [executable, "--print-service-config"],
                    env=environment,
                    text=True,
                )
            )

            data_root = home / "Library" / "Application Support" / "ChenContentLinkCollector"
            self.assertEqual(payload["executable"], str(runtime["python"]))
            self.assertEqual(payload["arguments"][0], str(runtime["collector"]))
            self.assertEqual(
                payload["arguments"][1:],
                ["desktop-app", "--host", "127.0.0.1", "--port", "51216"],
            )
            self.assertEqual(payload["workingDirectory"], str(data_root))
            self.assertEqual(payload["logPath"], str(data_root / "logs" / "desktop-app.log"))
            self.assertEqual(
                payload["environment"],
                {
                    "CHEN_COLLECTOR_DATA_ROOT": str(data_root),
                    "PATH": f"{runtime['tools']}:/usr/bin:/bin:/usr/sbin:/sbin",
                },
            )
            self.assertEqual(data_root.stat().st_mode & 0o777, 0o700)
            serialized = json.dumps(payload)
            self.assertNotIn("nohup", serialized)
            self.assertNotIn("/bin/zsh", serialized)
            self.assertNotIn("/tmp/", serialized)

    def test_print_service_config_reports_missing_bundled_runtime(self):
        builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            write_real_swift_fixture(source)
            runtime_root = root / "runtime"
            write_runtime_fixture(runtime_root)
            with mock.patch.object(builder, "validate_runtime_resources"), mock.patch.object(
                builder, "sign_app"
            ):
                app = builder.build_app(
                    source, root / "CHEN 内容采集助手.app", "abc123", runtime_root
                )
            executable = app / "Contents" / "MacOS" / "CHEN内容采集助手"
            shutil.rmtree(app / "Contents/Resources/Runtime" / platform.machine())
            home = root / "home"
            home.mkdir()
            environment = os.environ.copy()
            environment["CFFIXED_USER_HOME"] = str(home)

            result = subprocess.run(
                [executable, "--print-service-config"],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("内嵌 Python", result.stderr)

    def test_swift_sources_have_separate_runtime_responsibilities(self):
        desktop_root = ROOT / "desktop_app"
        expected = {
            "AppMain.swift",
            "LocalServiceHost.swift",
            "RuntimeBootstrap.swift",
            "VideoFolderAccess.swift",
        }

        self.assertEqual({path.name for path in desktop_root.glob("*.swift")}, expected)
        source = "\n".join((desktop_root / name).read_text(encoding="utf-8") for name in sorted(expected))
        self.assertNotIn("nohup", source)
        self.assertNotIn("/bin/zsh", source)
        self.assertIn("authorizedVideoFolderBookmark", source)
        self.assertIn("startAccessingSecurityScopedResource", source)

    def test_info_plist_explains_documents_access(self):
        info = plistlib.loads((ROOT / "desktop_app" / "Info.plist").read_bytes())

        self.assertEqual(
            info["NSDocumentsFolderUsageDescription"],
            "用于读取你选择的日报视频并发布到云端，不会修改源文件。",
        )
        self.assertEqual(info["LSMinimumSystemVersion"], "12.0")
        self.assertEqual(info["CFBundleIdentifier"], "com.chen.content-collector.native")
        self.assertEqual(info["CFBundleExecutable"], "CHEN内容采集助手")

    def test_video_folder_menu_targets_app_delegate(self):
        source = (ROOT / "desktop_app" / "AppMain.swift").read_text(encoding="utf-8")

        self.assertIn("folderMenuItem.target = self", source)

    def test_native_app_routes_external_links_to_default_browser(self):
        source = (ROOT / "desktop_app" / "AppMain.swift").read_text(encoding="utf-8")

        self.assertIn("WKUIDelegate", source)
        self.assertIn("webView.uiDelegate = self", source)
        self.assertIn("createWebViewWith", source)
        self.assertIn("decidePolicyFor", source)
        self.assertIn("NSWorkspace.shared.open", source)
        self.assertIn('url.host == "127.0.0.1"', source)
        self.assertIn("url.port == 51216", source)
        self.assertIn('scheme == "http" || scheme == "https"', source)
        self.assertIn("decisionHandler(.cancel)", source)
        self.assertIn("decisionHandler(.allow)", source)


if __name__ == "__main__":
    unittest.main()
