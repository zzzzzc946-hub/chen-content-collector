import importlib.util
import hashlib
import io
import os
import stat
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent
BUILDER_PATH = ROOT / "scripts" / "build_runtime_bundle.py"
EXPECTED_YT_DLP_WRAPPER = """#!/bin/sh
SELF=$0
case "$SELF" in
    */*) ;;
    *) SELF=$(command -v -- "$SELF") || exit 127 ;;
esac
SCRIPT_DIR=$(CDPATH= cd -- "${SELF%/*}" && pwd) || exit 127
exec "$SCRIPT_DIR/../python/bin/python3" -m yt_dlp "$@"
"""


def load_builder():
    spec = importlib.util.spec_from_file_location("build_runtime_bundle", BUILDER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuntimeBundleBuilderTests(unittest.TestCase):
    def test_runtime_lock_has_both_architectures_and_hashes(self):
        runtime_builder = load_builder()
        lock = runtime_builder.RuntimeLock.load(ROOT / "packaging/runtime-lock.json")

        self.assertEqual(set(lock.architectures), {"arm64", "x86_64"})
        self.assertTrue(all(len(asset.sha256) == 64 for asset in lock.assets))

    def test_runtime_lock_uses_only_the_brief_artifact_hashes(self):
        runtime_builder = load_builder()
        lock = runtime_builder.RuntimeLock.load(ROOT / "packaging/runtime-lock.json")
        expected_hashes = {
            "009588c2a7e499bc5a8b425b61fa65490968bbda9cd69e0cf2cff10f8304659a",
            "9a1e9e06175c10efd8378b904b07fa21bd791ab3345d7cdffeb4a76c9ff55903",
            "8e6b7e6533bdf746287008edf91102e7bee0a6ca1d24f16c4514237cafd706c5",
            "ff138c3a604f69911e9d42fd036e55c2a171e5616edf04c1e7f60a2a285540b0",
            "719757059f5a53fd0dde23f78cffeafcdd97b21c850ddb7ca684a3c1a1f122e2",
            "af2f8fede4171ef667dfded53f96e2ed0d6e6bd7ee3bb46437f77e3b57689228",
            "481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8",
            "f11f2b11d5a8ac4059f9bdf29fa4407dc7c6bb00c5097e95ca22a7a9db518266",
            "b1ae3173414b5fc5f538a726c4e48ea97edc0d2cdc11f103afee655c463fa742",
            "9d2baaf867088508d4a3458e61eeb30e945c4ad8016025545f66c4b5aaef0a61",
        }

        self.assertEqual({asset.sha256 for asset in lock.assets}, expected_hashes)

    def test_runtime_lock_selects_architecture_specific_playwright_wheels(self):
        runtime_builder = load_builder()
        lock = runtime_builder.RuntimeLock.load(ROOT / "packaging/runtime-lock.json")

        arm_wheels = {asset.name: asset for asset in lock.wheels_for("arm64")}
        intel_wheels = {asset.name: asset for asset in lock.wheels_for("x86_64")}

        self.assertEqual(
            arm_wheels["playwright"].sha256,
            "009588c2a7e499bc5a8b425b61fa65490968bbda9cd69e0cf2cff10f8304659a",
        )
        self.assertIn("macosx_11_0_arm64", arm_wheels["playwright"].url)
        self.assertEqual(
            intel_wheels["playwright"].sha256,
            "ff138c3a604f69911e9d42fd036e55c2a171e5616edf04c1e7f60a2a285540b0",
        )
        self.assertIn("macosx_10_13_x86_64", intel_wheels["playwright"].url)
        for package in ("greenlet", "pyee", "typing_extensions", "yt_dlp"):
            self.assertIs(arm_wheels[package], intel_wheels[package])

    def test_third_party_notices_identify_all_locked_sources_and_licenses(self):
        notices = (ROOT / "packaging/THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        for marker in (
            "CPython 3.12.13",
            "python-build-standalone 20260718",
            "Playwright for Python 1.61.0",
            "separate official PyPI macOS arm64 and x86_64 wheels",
            "1.61.1-beta-1782139630000",
            "greenlet 3.5.3",
            "pyee 13.0.1",
            "typing_extensions 4.16.0",
            "yt-dlp 2026.7.4",
            "imageio-ffmpeg 0.6.0",
            "FFmpeg 7.1",
            "Source:",
            "License:",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, notices)
        self.assertNotIn("/Users/", notices)

    def test_download_rejects_wrong_sha256(self):
        runtime_builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "bad.archive"
            archive.write_bytes(b"not the locked artifact")

            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                runtime_builder.verify_sha256(archive, "0" * 64)

    def test_corrupted_cache_is_replaced_only_with_verified_download(self):
        runtime_builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.whl"
            source.write_bytes(b"locked wheel bytes")
            asset = runtime_builder.LockedAsset(
                "fixture",
                "1.0",
                source.as_uri(),
                hashlib.sha256(source.read_bytes()).hexdigest(),
            )
            cache = root / "cache"
            destination = runtime_builder.cache_path_for(asset, cache)
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"corrupted cache")

            result = runtime_builder.cached_download(asset, cache)

            self.assertEqual(result, destination)
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertEqual(list(cache.glob(f".{destination.name}.*.tmp")), [])

    def test_failed_cache_replacement_preserves_existing_file(self):
        runtime_builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.whl"
            source.write_bytes(b"wrong replacement bytes")
            asset = runtime_builder.LockedAsset(
                "fixture", "1.0", source.as_uri(), "0" * 64
            )
            cache = root / "cache"
            destination = runtime_builder.cache_path_for(asset, cache)
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"existing corrupted cache")

            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                runtime_builder.cached_download(asset, cache)

            self.assertEqual(destination.read_bytes(), b"existing corrupted cache")
            self.assertEqual(list(cache.glob(f".{destination.name}.*.tmp")), [])

    def test_download_retries_transient_checksum_failure(self):
        runtime_builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid = b"verified replacement"
            asset = runtime_builder.LockedAsset(
                "fixture",
                "1.0",
                "https://example.invalid/fixture.whl",
                hashlib.sha256(valid).hexdigest(),
            )
            cache = root / "cache"

            with mock.patch.object(
                runtime_builder.urllib.request,
                "urlopen",
                side_effect=[io.BytesIO(b"damaged transfer"), io.BytesIO(valid)],
            ) as urlopen:
                result = runtime_builder.cached_download(asset, cache)

            self.assertEqual(result.read_bytes(), valid)
            self.assertEqual(urlopen.call_count, 2)
            self.assertEqual(list(cache.glob(f".{result.name}.*.tmp")), [])

    def test_verified_cache_is_reused_without_network_access(self):
        runtime_builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contents = b"verified cache"
            asset = runtime_builder.LockedAsset(
                "fixture",
                "1.0",
                "https://example.invalid/fixture.whl",
                hashlib.sha256(contents).hexdigest(),
            )
            destination = runtime_builder.cache_path_for(asset, root / "cache")
            destination.parent.mkdir(parents=True)
            destination.write_bytes(contents)

            with mock.patch.object(runtime_builder.urllib.request, "urlopen") as urlopen:
                result = runtime_builder.cached_download(asset, root / "cache")

            self.assertEqual(result, destination)
            urlopen.assert_not_called()

    def test_tar_extraction_rejects_absolute_and_parent_paths(self):
        runtime_builder = load_builder()
        for member_name in ("/absolute/file", "../outside", "python/../../outside"):
            with self.subTest(member=member_name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                archive = root / "bad.tar.gz"
                write_tar(archive, [(member_name, b"escape", None, None)])

                with self.assertRaisesRegex(RuntimeError, "unsafe tar"):
                    runtime_builder.safe_extract_tar(archive, root / "output")

                self.assertFalse((root / "outside").exists())

    def test_tar_extraction_rejects_symlink_and_hardlink_escapes(self):
        runtime_builder = load_builder()
        cases = (
            ("symlink", "python/link", "../../outside"),
            ("hardlink", "python/hard", "../outside"),
        )
        for kind, name, linkname in cases:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                archive = root / "bad.tar.gz"
                write_tar(archive, [(name, None, kind, linkname)])

                with self.assertRaisesRegex(RuntimeError, "unsafe tar"):
                    runtime_builder.safe_extract_tar(archive, root / "output")

    def test_tar_extraction_rejects_macos_equivalent_member_names(self):
        runtime_builder = load_builder()
        cases = (
            ("case", "Python/Lib/module.py", "python/lib/MODULE.py"),
            (
                "unicode",
                "python/lib/caf\N{LATIN SMALL LETTER E WITH ACUTE}.py",
                "python/lib/cafe\N{COMBINING ACUTE ACCENT}.py",
            ),
            (
                "mixed-nested",
                "PlayWright/Caf\N{LATIN SMALL LETTER E WITH ACUTE}/Driver.py",
                "playwright/cafe\N{COMBINING ACUTE ACCENT}/driver.PY",
            ),
        )
        for label, first, second in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                archive = root / "collision.tar.gz"
                write_tar(
                    archive,
                    [(first, b"first", None, None), (second, b"second", None, None)],
                )
                output = root / "output"

                with self.assertRaisesRegex(RuntimeError, "duplicate"):
                    runtime_builder.safe_extract_tar(archive, output)

                self.assertEqual(list(output.iterdir()), [])

    def test_tar_extraction_rejects_macos_equivalent_path_below_link(self):
        runtime_builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "link-collision.tar.gz"
            write_tar(
                archive,
                [
                    ("target", b"target", None, None),
                    ("Python/Link", None, "symlink", "../target"),
                    ("python/link/payload", b"payload", None, None),
                ],
            )

            with self.assertRaisesRegex(RuntimeError, "below link"):
                runtime_builder.safe_extract_tar(archive, root / "output")

    def test_wheel_extraction_rejects_path_escape(self):
        runtime_builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wheel = root / "bad.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("../../../outside", b"escape")

            with self.assertRaisesRegex(RuntimeError, "unsafe wheel"):
                runtime_builder.safe_extract_wheel(wheel, root / "site-packages")

            self.assertFalse((root / "outside").exists())

    def test_wheel_extraction_rejects_symlink_members(self):
        runtime_builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wheel = root / "bad.whl"
            member = zipfile.ZipInfo("package/link")
            member.create_system = 3
            member.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(member, "../../outside")

            with self.assertRaisesRegex(RuntimeError, "symlink"):
                runtime_builder.safe_extract_wheel(wheel, root / "site-packages")

    def test_wheel_extraction_rejects_macos_equivalent_member_names(self):
        runtime_builder = load_builder()
        cases = (
            ("case", "Package/Module.py", "package/module.PY"),
            (
                "unicode",
                "package/caf\N{LATIN SMALL LETTER E WITH ACUTE}.py",
                "package/cafe\N{COMBINING ACUTE ACCENT}.py",
            ),
            (
                "mixed-nested",
                "PlayWright/Caf\N{LATIN SMALL LETTER E WITH ACUTE}/Driver.js",
                "playwright/cafe\N{COMBINING ACUTE ACCENT}/driver.JS",
            ),
        )
        for label, first, second in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                wheel = root / "collision.whl"
                write_wheel(wheel, {first: b"first", second: b"second"})
                output = root / "site-packages"

                with self.assertRaisesRegex(RuntimeError, "duplicate"):
                    runtime_builder.safe_extract_wheel(wheel, output)

                self.assertEqual(list(output.iterdir()), [])

    def test_wheel_extraction_preserves_executable_mode(self):
        runtime_builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wheel = root / "playwright.whl"
            member = zipfile.ZipInfo("playwright/driver/node")
            member.create_system = 3
            member.external_attr = (stat.S_IFREG | 0o755) << 16
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(member, b"node")

            runtime_builder.safe_extract_wheel(wheel, root / "site-packages")

            node = root / "site-packages/playwright/driver/node"
            self.assertEqual(stat.S_IMODE(node.stat().st_mode), 0o755)

    def test_ffmpeg_extraction_installs_only_ffmpeg_with_executable_mode(self):
        runtime_builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wheel = root / "imageio_ffmpeg.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("imageio_ffmpeg/binaries/ffmpeg-macos-arm64-v7.1", b"ffmpeg")
                archive.writestr("imageio_ffmpeg/binaries/ffprobe-macos-arm64-v7.1", b"ffprobe")

            ffmpeg = runtime_builder.extract_ffmpeg(wheel, root / "tools")

            self.assertEqual(ffmpeg, (root / "tools/ffmpeg").resolve())
            self.assertEqual(ffmpeg.read_bytes(), b"ffmpeg")
            self.assertEqual(stat.S_IMODE(ffmpeg.stat().st_mode), 0o755)
            self.assertFalse((root / "tools/ffprobe").exists())

    def test_architecture_mismatch_is_rejected(self):
        runtime_builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "binary"
            executable.write_bytes(b"Mach-O fixture")
            completed = mock.Mock(returncode=0, stdout="x86_64\n", stderr="")
            with mock.patch.object(runtime_builder.subprocess, "run", return_value=completed):
                with self.assertRaisesRegex(RuntimeError, "architecture"):
                    runtime_builder.assert_architecture(executable, "arm64")

    def test_validation_checks_python_playwright_greenlet_and_ffmpeg(self):
        runtime_builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = (
                root / "python/bin/python3",
                root / "python/lib/python3.12/site-packages/playwright/driver/node",
                root / "python/lib/python3.12/site-packages/greenlet/_greenlet.cpython-312-darwin.so",
                root / "tools/ffmpeg",
            )
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
                path.chmod(0o755)
            yt_dlp = root / "tools/yt-dlp"
            yt_dlp.write_text(EXPECTED_YT_DLP_WRAPPER, encoding="utf-8")
            yt_dlp.chmod(0o755)

            with mock.patch.object(runtime_builder, "assert_architecture") as assert_arch, mock.patch.object(
                runtime_builder,
                "macho_architectures",
                return_value=frozenset({"arm64"}),
            ) as inspect_macho, mock.patch.object(
                runtime_builder, "host_architecture", return_value="x86_64"
            ), mock.patch.object(runtime_builder.subprocess, "run") as run:
                runtime_builder.validate_runtime(root, "arm64")

            self.assertEqual(
                {call.args[0] for call in assert_arch.call_args_list},
                {paths[0], paths[2], paths[3]},
            )
            inspect_macho.assert_called_once_with(paths[1])
            run.assert_not_called()

    def test_x86_validation_rejects_non_relocatable_yt_dlp_wrapper(self):
        runtime_builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = (
                root / "python/bin/python3",
                root / "python/lib/python3.12/site-packages/playwright/driver/node",
                root / "python/lib/python3.12/site-packages/greenlet/_greenlet.so",
                root / "tools/ffmpeg",
            )
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
                path.chmod(0o755)
            yt_dlp = root / "tools/yt-dlp"
            yt_dlp.write_text(
                "#!/bin/sh\nexec /Users/builder/runtime/python/bin/python3 -m yt_dlp \"$@\"\n",
                encoding="utf-8",
            )
            yt_dlp.chmod(0o755)

            with mock.patch.object(runtime_builder, "assert_architecture"), mock.patch.object(
                runtime_builder,
                "macho_architectures",
                return_value=frozenset({"x86_64"}),
            ), mock.patch.object(
                runtime_builder, "host_architecture", return_value="arm64"
            ):
                with self.assertRaisesRegex(RuntimeError, "yt-dlp wrapper"):
                    runtime_builder.validate_runtime(root, "x86_64")

    def test_x86_validation_rejects_non_executable_yt_dlp_wrapper(self):
        runtime_builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = (
                root / "python/bin/python3",
                root / "python/lib/python3.12/site-packages/playwright/driver/node",
                root / "python/lib/python3.12/site-packages/greenlet/_greenlet.so",
                root / "tools/ffmpeg",
            )
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
                path.chmod(0o755)
            yt_dlp = root / "tools/yt-dlp"
            yt_dlp.write_text(EXPECTED_YT_DLP_WRAPPER, encoding="utf-8")
            yt_dlp.chmod(0o644)

            with mock.patch.object(runtime_builder, "assert_architecture"), mock.patch.object(
                runtime_builder,
                "macho_architectures",
                return_value=frozenset({"x86_64"}),
            ), mock.patch.object(
                runtime_builder, "host_architecture", return_value="arm64"
            ):
                with self.assertRaisesRegex(RuntimeError, "mode 0755"):
                    runtime_builder.validate_runtime(root, "x86_64")

    def test_arm_runtime_rejects_foreign_x86_playwright_node(self):
        runtime_builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python = root / "python/bin/python3"
            ffmpeg = root / "tools/ffmpeg"
            node = root / "python/lib/python3.12/site-packages/playwright/driver/node"
            greenlet = (
                root
                / "python/lib/python3.12/site-packages/greenlet/_greenlet.cpython-312-darwin.so"
            )
            for path in (python, ffmpeg, node, greenlet):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
                path.chmod(0o755)
            yt_dlp = root / "tools/yt-dlp"
            yt_dlp.write_text(EXPECTED_YT_DLP_WRAPPER, encoding="utf-8")
            yt_dlp.chmod(0o755)

            with mock.patch.object(runtime_builder, "assert_architecture"), mock.patch.object(
                runtime_builder,
                "macho_architectures",
                return_value=frozenset({"x86_64"}),
            ), mock.patch.object(
                runtime_builder, "host_architecture", return_value="arm64"
            ), mock.patch.object(runtime_builder.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "Playwright architecture"):
                    runtime_builder.validate_runtime(root, "arm64")

            run.assert_not_called()

    def test_native_validation_imports_dependencies_and_requires_python_3_12(self):
        runtime_builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python = root / "python/bin/python3"
            ffmpeg = root / "tools/ffmpeg"
            node = root / "python/lib/python3.12/site-packages/playwright/driver/node"
            greenlet = (
                root
                / "python/lib/python3.12/site-packages/greenlet/_greenlet.cpython-312-darwin.so"
            )
            python.parent.mkdir(parents=True)
            ffmpeg.parent.mkdir(parents=True)
            node.parent.mkdir(parents=True)
            greenlet.parent.mkdir(parents=True)
            cli = node.parent / "package/cli.js"
            cli.parent.mkdir(parents=True)
            cli.write_text("// fixture\n", encoding="utf-8")
            for path in (python, ffmpeg, node, greenlet):
                path.write_bytes(b"fixture")
                path.chmod(0o755)
            yt_dlp = root / "tools/yt-dlp"
            yt_dlp.write_text(EXPECTED_YT_DLP_WRAPPER, encoding="utf-8")
            yt_dlp.chmod(0o755)

            snapshots = []

            def capture_run(command, **kwargs):
                cwd = Path(kwargs["cwd"])
                snapshots.append((command, kwargs, list(cwd.iterdir())))
                return mock.Mock(returncode=0)

            with mock.patch.object(runtime_builder, "assert_architecture"), mock.patch.object(
                runtime_builder,
                "macho_architectures",
                return_value=frozenset({"arm64"}),
            ), mock.patch.object(
                runtime_builder, "host_architecture", return_value="arm64"
            ), mock.patch.object(
                runtime_builder.subprocess, "run", side_effect=capture_run
            ) as run:
                runtime_builder.validate_runtime(root, "arm64")

            self.assertEqual(run.call_count, 2)
            python_call, driver_call = run.call_args_list
            command = python_call.args[0]
            self.assertEqual(command[:4], [str(python), "-I", "-B", "-c"])
            self.assertIn("playwright", command[4])
            self.assertIn("yt_dlp", command[4])
            self.assertIn("greenlet", command[4])
            self.assertIn("(3, 12)", command[4])
            self.assertIn("shutil.which", command[4])
            self.assertIn("yt-dlp", command[4])
            self.assertIn("--version", command[4])
            self.assertTrue(python_call.kwargs["check"])
            self.assertEqual(
                driver_call.args[0],
                [
                    str(node),
                    str(node.parent / "package/cli.js"),
                    "--version",
                ],
            )
            self.assertTrue(driver_call.kwargs["check"])
            self.assertEqual(
                python_call.kwargs["env"],
                {
                    "PATH": str((root / "tools").resolve()),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
            )
            self.assertEqual(driver_call.kwargs["env"], {})
            self.assertEqual(python_call.kwargs["cwd"], driver_call.kwargs["cwd"])
            self.assertEqual([contents for _, _, contents in snapshots], [[], []])

    def test_native_validation_does_not_inherit_hostile_env_or_project_cwd(self):
        runtime_builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            python = runtime / "python/bin/python3"
            ffmpeg = runtime / "tools/ffmpeg"
            node = (
                runtime
                / "python/lib/python3.12/site-packages/playwright/driver/node"
            )
            greenlet = (
                runtime
                / "python/lib/python3.12/site-packages/greenlet/_greenlet.so"
            )
            cli = node.parent / "package/cli.js"
            for path in (python, ffmpeg, node, greenlet, cli):
                path.parent.mkdir(parents=True, exist_ok=True)
            python_validation_script = """#!/bin/sh
test "$1" = "-I" || exit 41
test "$2" = "-B" || exit 42
test ! -e playwright.py || exit 43
for name in PYTHONPATH PYTHONHOME NODE_OPTIONS NODE_PATH VIRTUAL_ENV CONDA_PREFIX; do
    eval 'test -z "${'"$name"'+x}"' || exit 44
done
test "$PYTHONDONTWRITEBYTECODE" = "1" || exit 48
exit 0
"""
            node_validation_script = """#!/bin/sh
test "$2" = "--version" || exit 45
test ! -e playwright.py || exit 46
for name in PYTHONPATH PYTHONHOME NODE_OPTIONS NODE_PATH VIRTUAL_ENV CONDA_PREFIX; do
    eval 'test -z "${'"$name"'+x}"' || exit 47
done
exit 0
"""
            python.write_text(python_validation_script, encoding="utf-8")
            node.write_text(node_validation_script, encoding="utf-8")
            for path in (python, node):
                path.chmod(0o755)
            ffmpeg.write_bytes(b"fixture")
            greenlet.write_bytes(b"fixture")
            cli.write_text("// fixture\n", encoding="utf-8")
            yt_dlp = runtime / "tools/yt-dlp"
            yt_dlp.write_text(EXPECTED_YT_DLP_WRAPPER, encoding="utf-8")
            yt_dlp.chmod(0o755)

            hostile = root / "hostile-project"
            hostile.mkdir()
            for module in ("playwright.py", "yt_dlp.py", "greenlet.py"):
                (hostile / module).write_text("raise SystemExit(0)\n", encoding="utf-8")
            hostile_env = {
                "PYTHONPATH": str(hostile),
                "PYTHONHOME": str(hostile),
                "NODE_OPTIONS": "--require=./playwright.py",
                "NODE_PATH": str(hostile),
                "VIRTUAL_ENV": str(hostile / ".venv"),
                "CONDA_PREFIX": str(hostile / "conda"),
            }
            original_cwd = Path.cwd()
            try:
                os.chdir(hostile)
                with mock.patch.dict(os.environ, hostile_env, clear=False), mock.patch.object(
                    runtime_builder, "assert_architecture"
                ), mock.patch.object(
                    runtime_builder,
                    "macho_architectures",
                    return_value=frozenset({"arm64"}),
                ), mock.patch.object(
                    runtime_builder, "host_architecture", return_value="arm64"
                ):
                    runtime_builder.validate_runtime(runtime, "arm64")
            finally:
                os.chdir(original_cwd)

    def test_builder_installs_locked_wheels_into_independent_arch_runtime(self):
        runtime_builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python_archive = root / "python.tar.gz"
            write_tar(
                python_archive,
                [("python/bin/python3", b"python", None, None)],
            )
            wheel_paths = {}
            for package in ("playwright", "greenlet", "pyee", "typing_extensions", "yt_dlp"):
                wheel = root / f"{package}.whl"
                member_name = f"{package}/__init__.py"
                if package == "playwright":
                    write_wheel(
                        wheel,
                        {
                            member_name: b"",
                            "playwright/driver/node": b"node",
                        },
                        executable={"playwright/driver/node"},
                    )
                elif package == "greenlet":
                    write_wheel(
                        wheel,
                        {
                            member_name: b"",
                            "greenlet/_greenlet.cpython-312-darwin.so": b"greenlet",
                        },
                    )
                else:
                    write_wheel(wheel, {member_name: b""})
                wheel_paths[package] = wheel
            ffmpeg_wheel = root / "ffmpeg.whl"
            write_wheel(
                ffmpeg_wheel,
                {"imageio_ffmpeg/binaries/ffmpeg-macos-arm64-v7.1": b"ffmpeg"},
            )
            lock = runtime_builder.RuntimeLock(
                python={
                    "arm64": fixture_asset(runtime_builder, "python-arm64"),
                    "x86_64": fixture_asset(runtime_builder, "python-x86_64"),
                },
                wheels={
                    **{
                        name: fixture_asset(runtime_builder, name)
                        for name in wheel_paths
                        if name != "playwright"
                    },
                    "playwright": {
                        "arm64": fixture_asset(runtime_builder, "playwright-arm64"),
                        "x86_64": fixture_asset(runtime_builder, "playwright-x86_64"),
                    },
                },
                ffmpeg={
                    "arm64": fixture_asset(runtime_builder, "ffmpeg-arm64"),
                    "x86_64": fixture_asset(runtime_builder, "ffmpeg-x86_64"),
                },
            )
            archives = {
                "python-arm64": python_archive,
                **wheel_paths,
                "playwright-arm64": wheel_paths["playwright"],
                "ffmpeg-arm64": ffmpeg_wheel,
            }

            with mock.patch.object(
                runtime_builder,
                "cached_download",
                side_effect=lambda asset, _cache: archives[asset.name],
            ), mock.patch.object(runtime_builder, "validate_runtime") as validate:
                result = runtime_builder.RuntimeBundleBuilder(lock, root / "cache").build(
                    "arm64", root / "runtime"
                )

            site_packages = result / "python/lib/python3.12/site-packages"
            self.assertEqual(result, (root / "runtime/arm64").resolve())
            for package in wheel_paths:
                self.assertTrue((site_packages / package / "__init__.py").is_file())
            self.assertEqual((result / "tools/ffmpeg").read_bytes(), b"ffmpeg")
            self.assertFalse((result / "tools/ffprobe").exists())
            yt_dlp = result / "tools/yt-dlp"
            self.assertEqual(yt_dlp.read_text(encoding="utf-8"), EXPECTED_YT_DLP_WRAPPER)
            self.assertEqual(stat.S_IMODE(yt_dlp.stat().st_mode), 0o755)
            self.assertNotIn(str(root), yt_dlp.read_text(encoding="utf-8"))
            for path in (
                result,
                result / "python/bin/python3",
                result / "tools/ffmpeg",
                yt_dlp,
            ):
                self.assertEqual(
                    int(path.stat().st_mtime), runtime_builder.REPRODUCIBLE_MTIME
                )
            validate.assert_called_once_with(mock.ANY, "arm64")

    def test_builder_retains_rollback_copy_when_install_and_restore_fail(self):
        runtime_builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "runtime"
            final = output / "arm64"
            final.mkdir(parents=True)
            final = final.resolve()
            old_runtime = b"known-good-runtime-bytes"
            (final / "runtime.bin").write_bytes(old_runtime)
            rollback = output.resolve() / ".arm64.rollback"
            original_replace = os.replace

            def fail_install_and_restore(source, target):
                source = Path(source)
                target = Path(target)
                if target == final:
                    if source == rollback:
                        raise OSError("restore rename failed")
                    raise OSError("install rename failed")
                return original_replace(source, target)

            with self.assertRaisesRegex(
                RuntimeError,
                "replacement failed; recovery copy was retained",
            ) as raised, self._build_with_replacement_fixtures(
                runtime_builder,
                root,
            ), mock.patch.object(
                runtime_builder.os,
                "replace",
                side_effect=fail_install_and_restore,
            ):
                runtime_builder.RuntimeBundleBuilder(
                    self._replacement_lock(runtime_builder), root / "cache"
                ).build("arm64", output)

            self.assertNotIn(str(root), str(raised.exception))
            self.assertIsNone(raised.exception.__cause__)
            self.assertFalse(final.exists())
            self.assertEqual((rollback / "runtime.bin").read_bytes(), old_runtime)
            self.assertEqual(
                [path.resolve() for path in output.glob(".arm64.*")], [rollback]
            )

    def test_builder_removes_rollback_copy_after_successful_replacement(self):
        runtime_builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "runtime"
            final = output / "arm64"
            final.mkdir(parents=True)
            final = final.resolve()
            (final / "runtime.bin").write_bytes(b"old runtime")
            rollback = output.resolve() / ".arm64.rollback"

            with self._build_with_replacement_fixtures(runtime_builder, root):
                result = runtime_builder.RuntimeBundleBuilder(
                    self._replacement_lock(runtime_builder), root / "cache"
                ).build("arm64", output)

            self.assertEqual(result, final.resolve())
            self.assertFalse(rollback.exists())

    def test_builder_restores_final_and_cleans_rollback_when_install_fails(self):
        runtime_builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "runtime"
            final = output / "arm64"
            final.mkdir(parents=True)
            final = final.resolve()
            old_runtime = b"known-good-runtime-bytes"
            (final / "runtime.bin").write_bytes(old_runtime)
            rollback = output.resolve() / ".arm64.rollback"
            original_replace = os.replace

            def fail_install(source, target):
                if Path(target) == final and Path(source) != rollback:
                    raise OSError("install rename failed")
                return original_replace(source, target)

            with self.assertRaises(OSError), self._build_with_replacement_fixtures(
                runtime_builder,
                root,
            ), mock.patch.object(
                runtime_builder.os,
                "replace",
                side_effect=fail_install,
            ):
                runtime_builder.RuntimeBundleBuilder(
                    self._replacement_lock(runtime_builder), root / "cache"
                ).build("arm64", output)

            self.assertEqual((final / "runtime.bin").read_bytes(), old_runtime)
            self.assertFalse(rollback.exists())

    def test_builder_recovers_stale_rollback_before_replacement(self):
        runtime_builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "runtime"
            rollback = output / ".arm64.rollback"
            rollback.mkdir(parents=True)
            (rollback / "runtime.bin").write_bytes(b"recovery runtime")
            final = (output / "arm64").resolve()
            rollback = rollback.resolve()

            with self._build_with_replacement_fixtures(runtime_builder, root), mock.patch.object(
                runtime_builder.os,
                "replace",
                wraps=os.replace,
            ) as replace:
                runtime_builder.RuntimeBundleBuilder(
                    self._replacement_lock(runtime_builder), root / "cache"
                ).build("arm64", output)

            self.assertIn(mock.call(rollback, final), replace.call_args_list)
            self.assertTrue(final.is_dir())
            self.assertFalse(rollback.exists())

    def test_builder_refuses_to_overwrite_stale_rollback_when_final_exists(self):
        runtime_builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "runtime"
            final = output / "arm64"
            rollback = output / ".arm64.rollback"
            final.mkdir(parents=True)
            rollback.mkdir()
            (final / "runtime.bin").write_bytes(b"current runtime")
            (rollback / "runtime.bin").write_bytes(b"recovery runtime")

            with self.assertRaisesRegex(RuntimeError, "refusing to replace"):
                runtime_builder.RuntimeBundleBuilder(
                    self._replacement_lock(runtime_builder), root / "cache"
                ).build("arm64", output)

            self.assertEqual((final / "runtime.bin").read_bytes(), b"current runtime")
            self.assertEqual((rollback / "runtime.bin").read_bytes(), b"recovery runtime")

    def _replacement_lock(self, runtime_builder):
        asset = fixture_asset(runtime_builder, "replacement")
        return runtime_builder.RuntimeLock(
            python={"arm64": asset, "x86_64": asset},
            wheels={},
            ffmpeg={"arm64": asset, "x86_64": asset},
        )

    def _build_with_replacement_fixtures(self, runtime_builder, root):
        archive = root / "fixture.archive"
        archive.write_bytes(b"fixture")
        return mock.patch.multiple(
            runtime_builder,
            cached_download=mock.DEFAULT,
            safe_extract_tar=mock.DEFAULT,
            safe_extract_wheel=mock.DEFAULT,
            extract_ffmpeg=mock.DEFAULT,
            validate_runtime=mock.DEFAULT,
            normalize_tree_metadata=mock.DEFAULT,
        )

    def test_metadata_is_identical_across_ambient_umasks(self):
        runtime_builder = load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifests = []
            for name, umask in (("permissive", 0o022), ("restrictive", 0o077)):
                runtime = root / name
                previous_umask = os.umask(umask)
                try:
                    nested = runtime / "python/lib/python3.12"
                    nested.mkdir(parents=True)
                    (nested / "stdlib.py").write_text("pass\n", encoding="utf-8")
                    executable = runtime / "python/bin/python3"
                    executable.parent.mkdir(parents=True)
                    executable.write_bytes(b"python")
                    executable.chmod(0o755)
                    os.symlink("python3", executable.parent / "python")
                finally:
                    os.umask(previous_umask)

                runtime_builder.normalize_tree_metadata(runtime)
                manifests.append(metadata_manifest(runtime))

            self.assertEqual(manifests[0], manifests[1])
            modes = {entry[0]: entry[2] for entry in manifests[0]}
            self.assertEqual(modes["."], 0o755)
            self.assertEqual(modes["python/lib/python3.12"], 0o755)
            self.assertEqual(modes["python/lib/python3.12/stdlib.py"], 0o644)
            self.assertEqual(modes["python/bin/python3"], 0o755)
            self.assertEqual(modes["python/bin/python"], 0o755)


def write_tar(archive: Path, entries) -> None:
    with tarfile.open(archive, "w:gz") as tar:
        for name, contents, kind, linkname in entries:
            member = tarfile.TarInfo(name)
            if kind == "symlink":
                member.type = tarfile.SYMTYPE
                member.linkname = linkname
                tar.addfile(member)
            elif kind == "hardlink":
                member.type = tarfile.LNKTYPE
                member.linkname = linkname
                tar.addfile(member)
            else:
                member.size = len(contents)
                tar.addfile(member, io.BytesIO(contents))


def write_wheel(archive: Path, entries, *, executable=frozenset()) -> None:
    with zipfile.ZipFile(archive, "w") as wheel:
        for name, contents in entries.items():
            member = zipfile.ZipInfo(name)
            member.create_system = 3
            permissions = 0o755 if name in executable else 0o644
            member.external_attr = (stat.S_IFREG | permissions) << 16
            wheel.writestr(member, contents)


def metadata_manifest(root: Path):
    entries = []
    for path in [root, *sorted(root.rglob("*"))]:
        relative = "." if path == root else path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            kind = "symlink"
        elif stat.S_ISDIR(info.st_mode):
            kind = "directory"
        else:
            kind = "file"
        entries.append(
            (
                relative,
                kind,
                stat.S_IMODE(info.st_mode),
                int(info.st_mtime),
            )
        )
    return entries


def fixture_asset(runtime_builder, name):
    return runtime_builder.LockedAsset(
        name,
        "1.0",
        f"https://example.invalid/{name}",
        hashlib.sha256(name.encode()).hexdigest(),
    )


if __name__ == "__main__":
    unittest.main()
