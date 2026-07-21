#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Iterable, Sequence


BUNDLE_IDENTIFIER = "com.chen.content-collector.native"
EXECUTABLE_NAME = "CHEN内容采集助手"
ICON_NAME = "orange-app-icon.icns"
MACOS_SDK = "macosx15.4"
MINIMUM_MACOS_VERSION = "12.0"
ARCHITECTURE_TARGETS = {
    "arm64": "arm64-apple-macos12.0",
    "x86_64": "x86_64-apple-macos12.0",
}
UNIVERSAL_ARCHITECTURES = frozenset(ARCHITECTURE_TARGETS)
PAYLOAD_FILES = (
    Path("content_link_collector.py"),
    Path("max_daily_cloud/publisher/__init__.py"),
    Path("max_daily_cloud/publisher/local_publish_jobs.py"),
    Path("max_daily_cloud/publisher/max_daily_publisher.py"),
    Path("max_daily_cloud/publisher/media_proxy.py"),
    Path("max_daily_cloud/publisher/config.example.json"),
)
RUNTIME_LOCK = Path("packaging/runtime-lock.json")
THIRD_PARTY_NOTICES = Path("packaging/THIRD_PARTY_NOTICES.md")
MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
}


def _command_output(command: Sequence[str]) -> str:
    result = subprocess.run(
        list(command), check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def architecture_set(path: Path) -> set[str]:
    result = subprocess.run(
        ["/usr/bin/lipo", "-archs", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    architectures = set(result.stdout.split())
    if not architectures:
        raise RuntimeError(f"architecture inspection returned no slices for {path}")
    return architectures


def assert_exact_architectures(path: Path, expected: Iterable[str]) -> None:
    expected_set = set(expected)
    actual = architecture_set(path)
    if actual != expected_set:
        raise RuntimeError(
            f"architecture mismatch for {path}: expected {sorted(expected_set)}, "
            f"got {sorted(actual)}"
        )


def compile_swift(source_files: Sequence[Path], executable: Path) -> None:
    sdk_path = _command_output(
        ["/usr/bin/xcrun", "--sdk", MACOS_SDK, "--show-sdk-path"]
    )
    if Path(sdk_path).name != "MacOSX15.4.sdk":
        raise RuntimeError(f"Expected macOS 15.4 SDK, got {sdk_path!r}")

    executable = Path(executable)
    executable.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".swift-universal2-", dir=executable.parent
    ) as temporary_name:
        temporary = Path(temporary_name)
        module_cache = temporary / "module-cache"
        clang_cache = temporary / "clang-module-cache"
        build_environment = os.environ.copy()
        build_environment["SDKROOT"] = sdk_path
        build_environment["CLANG_MODULE_CACHE_PATH"] = str(clang_cache)
        thin_executables: list[Path] = []

        for architecture, target in ARCHITECTURE_TARGETS.items():
            thin = temporary / f"{EXECUTABLE_NAME}.{architecture}"
            subprocess.run(
                [
                    "/usr/bin/xcrun",
                    "--sdk",
                    MACOS_SDK,
                    "swiftc",
                    "-O",
                    "-parse-as-library",
                    "-target",
                    target,
                    "-sdk",
                    sdk_path,
                    "-module-cache-path",
                    str(module_cache),
                    "-framework",
                    "Cocoa",
                    "-framework",
                    "WebKit",
                    *[str(path) for path in source_files],
                    "-o",
                    str(thin),
                ],
                check=True,
                env=build_environment,
            )
            assert_exact_architectures(thin, {architecture})
            thin_executables.append(thin)

        subprocess.run(
            [
                "/usr/bin/lipo",
                "-create",
                *[str(path) for path in thin_executables],
                "-output",
                str(executable),
            ],
            check=True,
        )
    executable.chmod(0o755)
    assert_exact_architectures(executable, UNIVERSAL_ARCHITECTURES)


def _is_macho(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        with path.open("rb") as source:
            return source.read(4) in MACHO_MAGICS
    except OSError:
        return False


def _nested_macho_files(app_path: Path) -> list[Path]:
    contents = app_path / "Contents"
    paths = [path for path in contents.rglob("*") if _is_macho(path)]
    return sorted(
        paths,
        key=lambda path: (-len(path.relative_to(app_path).parts), str(path)),
    )


def sign_app(app_path: Path) -> None:
    for path in _nested_macho_files(app_path):
        subprocess.run(
            ["/usr/bin/codesign", "--force", "--sign", "-", str(path)],
            check=True,
        )
    subprocess.run(
        ["/usr/bin/codesign", "--force", "--sign", "-", str(app_path)],
        check=True,
    )
    subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app_path)],
        check=True,
    )


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _regular_source(source_root: Path, relative: Path, label: str) -> Path:
    relative = Path(relative)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise RuntimeError(f"unsafe {label} path: {relative}")

    current = source_root
    for component in relative.parts:
        current = current / component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError as error:
            raise RuntimeError(f"missing {label} file: {relative}") from error
        if stat.S_ISLNK(mode):
            raise RuntimeError(f"{label} entry must not be a symlink: {relative}")

    resolved = current.resolve(strict=True)
    if not _is_within(resolved, source_root):
        raise RuntimeError(f"escaping {label} path: {relative}")
    if not stat.S_ISREG(current.lstat().st_mode):
        raise RuntimeError(f"{label} entry must be a regular file: {relative}")
    return current


def _validate_runtime_tree(runtime: Path, architecture: str) -> None:
    try:
        root_mode = runtime.lstat().st_mode
    except FileNotFoundError as error:
        raise RuntimeError(f"missing runtime architecture: {architecture}") from error
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        raise RuntimeError(f"runtime architecture must be a real directory: {architecture}")

    resolved_root = runtime.resolve(strict=True)
    for current_name, directories, files in os.walk(
        runtime, topdown=True, followlinks=False
    ):
        current = Path(current_name)
        for name in sorted([*directories, *files]):
            path = current / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                target = os.readlink(path)
                if os.path.isabs(target):
                    raise RuntimeError(f"unsafe runtime symlink: {path}")
                try:
                    resolved = path.resolve(strict=True)
                except (FileNotFoundError, RuntimeError) as error:
                    raise RuntimeError(f"unsafe runtime symlink: {path}") from error
                if not _is_within(resolved, resolved_root):
                    raise RuntimeError(f"escaping runtime symlink: {path}")
                resolved_mode = resolved.lstat().st_mode
                if not (stat.S_ISREG(resolved_mode) or stat.S_ISDIR(resolved_mode)):
                    raise RuntimeError(f"unsafe runtime symlink target: {path}")
            elif not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise RuntimeError(f"unsafe runtime special file: {path}")


def _copy_runtime(runtime_root: Path, destination: Path) -> None:
    try:
        root_mode = runtime_root.lstat().st_mode
    except FileNotFoundError as error:
        raise RuntimeError(f"missing runtime root: {runtime_root}") from error
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        raise RuntimeError(f"runtime root must be a real directory: {runtime_root}")

    destination.mkdir(parents=True, exist_ok=True)
    destination.chmod(0o755)
    for architecture in ARCHITECTURE_TARGETS:
        source = runtime_root / architecture
        _validate_runtime_tree(source, architecture)
        shutil.copytree(
            source,
            destination / architecture,
            symlinks=True,
            copy_function=shutil.copy2,
        )


def _runtime_components(runtime: Path) -> tuple[Path, Path, Path, Path]:
    python = runtime / "python/bin/python3"
    node = runtime / "python/lib/python3.12/site-packages/playwright/driver/node"
    greenlets = list(
        runtime.glob(
            "python/lib/python3.12/site-packages/greenlet/_greenlet*.so"
        )
    )
    ffmpeg = runtime / "tools/ffmpeg"
    if len(greenlets) != 1:
        raise RuntimeError(
            f"runtime must contain exactly one greenlet extension: {runtime}"
        )
    for label, path in (("Python", python), ("Playwright Node", node), ("FFmpeg", ffmpeg)):
        if not path.is_file():
            raise RuntimeError(f"runtime is missing {label}: {path}")
    return python, node, greenlets[0], ffmpeg


def validate_runtime_resources(runtime_root: Path) -> None:
    runtime_root = Path(runtime_root)
    actual_directories = {
        path.name
        for path in runtime_root.iterdir()
        if path.is_dir() and not path.is_symlink()
    }
    if actual_directories != UNIVERSAL_ARCHITECTURES:
        raise RuntimeError(
            "runtime architecture directories mismatch: "
            f"expected {sorted(UNIVERSAL_ARCHITECTURES)}, got {sorted(actual_directories)}"
        )
    for architecture in ARCHITECTURE_TARGETS:
        python, node, greenlet, ffmpeg = _runtime_components(
            runtime_root / architecture
        )
        for path in (python, node, ffmpeg):
            assert_exact_architectures(path, {architecture})
        assert_exact_architectures(greenlet, UNIVERSAL_ARCHITECTURES)


def _copy_payload(source_root: Path, payload_root: Path) -> None:
    payload_root.mkdir(parents=True, exist_ok=True)
    payload_root.chmod(0o755)
    for relative in PAYLOAD_FILES:
        source = _regular_source(source_root, relative, "payload")
        destination = payload_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.parent.chmod(0o755)
        shutil.copyfile(source, destination)
        destination.chmod(0o644)


def _copy_distribution_metadata(source_root: Path, resources: Path) -> None:
    runtime_lock = _regular_source(source_root, RUNTIME_LOCK, "runtime lock")
    notices = _regular_source(
        source_root, THIRD_PARTY_NOTICES, "third-party notices"
    )
    shutil.copyfile(runtime_lock, resources / "Runtime/runtime-lock.json")
    shutil.copyfile(notices, resources / "THIRD_PARTY_NOTICES.md")
    (resources / "Runtime/runtime-lock.json").chmod(0o644)
    (resources / "THIRD_PARTY_NOTICES.md").chmod(0o644)


def validate_app_bundle(app_path: Path, expected_commit: str) -> None:
    info_path = app_path / "Contents" / "Info.plist"
    try:
        info = plistlib.loads(info_path.read_bytes())
    except (OSError, plistlib.InvalidFileException) as error:
        raise RuntimeError("原生 App 缺少有效 Info.plist。") from error

    if info.get("CFBundleIdentifier") != BUNDLE_IDENTIFIER:
        raise RuntimeError("原生 App bundle identifier 不正确。")
    if info.get("CFBundleExecutable") != EXECUTABLE_NAME:
        raise RuntimeError("原生 App executable 不正确。")
    if info.get("LSMinimumSystemVersion") != MINIMUM_MACOS_VERSION:
        raise RuntimeError("原生 App 最低 macOS 版本不正确。")
    if info.get("ChenSourceCommit") != expected_commit:
        raise RuntimeError("原生 App 提交信息不正确。")

    executable = app_path / "Contents" / "MacOS" / EXECUTABLE_NAME
    resources = app_path / "Contents" / "Resources"
    if not executable.is_file():
        raise RuntimeError("原生 App 缺少可执行文件。")
    if not (resources / ICON_NAME).is_file():
        raise RuntimeError("原生 App 缺少图标资源。")
    assert_exact_architectures(executable, UNIVERSAL_ARCHITECTURES)
    validate_runtime_resources(resources / "Runtime")

    payload_root = resources / "CollectorPayload"
    embedded_payload = {
        path.relative_to(payload_root)
        for path in payload_root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if embedded_payload != set(PAYLOAD_FILES):
        raise RuntimeError(
            f"embedded payload does not match allowlist: {sorted(map(str, embedded_payload))}"
        )
    if not (resources / "Runtime/runtime-lock.json").is_file():
        raise RuntimeError("App is missing the runtime lock")
    if not (resources / "THIRD_PARTY_NOTICES.md").is_file():
        raise RuntimeError("App is missing third-party notices")


def build_app(
    source_root: Path,
    output_app: Path,
    commit: str,
    runtime_root: Path | None = None,
) -> Path:
    source_root = Path(source_root).resolve()
    output_app = Path(output_app).expanduser().resolve()
    runtime_root = (
        Path(runtime_root).expanduser()
        if runtime_root is not None
        else source_root / "build/runtime"
    )
    desktop_root = source_root / "desktop_app"
    source_files = sorted(desktop_root.glob("*.swift"))
    info_template = desktop_root / "Info.plist"
    icon_source = desktop_root / "Resources" / ICON_NAME

    if not source_files:
        raise RuntimeError("缺少 desktop_app Swift 源文件。")
    if not info_template.is_file() or info_template.is_symlink():
        raise RuntimeError("缺少 desktop_app/Info.plist。")
    if not icon_source.is_file() or icon_source.is_symlink():
        raise RuntimeError("缺少原生 App 图标资源。")

    for relative in PAYLOAD_FILES:
        _regular_source(source_root, relative, "payload")
    _regular_source(source_root, RUNTIME_LOCK, "runtime lock")
    _regular_source(source_root, THIRD_PARTY_NOTICES, "third-party notices")
    for architecture in ARCHITECTURE_TARGETS:
        _validate_runtime_tree(runtime_root / architecture, architecture)

    output_app.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_app.parent / f".{output_app.name}.{uuid.uuid4().hex}.tmp"
    previous = output_app.parent / f".{output_app.name}.{uuid.uuid4().hex}.previous"
    contents = temporary / "Contents"
    executable = contents / "MacOS" / EXECUTABLE_NAME
    resources = contents / "Resources"
    executable.parent.mkdir(parents=True)
    resources.mkdir(parents=True)
    preserve_previous = False
    for directory in (temporary, contents, executable.parent, resources):
        directory.chmod(0o755)

    try:
        info = plistlib.loads(info_template.read_bytes())
        info["CFBundleIdentifier"] = BUNDLE_IDENTIFIER
        info["CFBundleExecutable"] = EXECUTABLE_NAME
        info["LSMinimumSystemVersion"] = MINIMUM_MACOS_VERSION
        info["ChenSourceCommit"] = commit
        info_path = contents / "Info.plist"
        info_path.write_bytes(plistlib.dumps(info, sort_keys=True))
        info_path.chmod(0o644)
        shutil.copyfile(icon_source, resources / ICON_NAME)
        (resources / ICON_NAME).chmod(0o644)

        compile_swift(source_files, executable)
        _copy_payload(source_root, resources / "CollectorPayload")
        _copy_runtime(runtime_root, resources / "Runtime")
        _copy_distribution_metadata(source_root, resources)
        validate_app_bundle(temporary, commit)
        sign_app(temporary)

        if output_app.exists():
            os.replace(output_app, previous)
        os.replace(temporary, output_app)
        if previous.exists():
            shutil.rmtree(previous)
        return output_app
    except Exception:
        if not output_app.exists() and previous.exists():
            try:
                os.replace(previous, output_app)
            except OSError:
                preserve_previous = True
                raise RuntimeError(
                    "App replacement failed; recovery copy was retained. "
                    "Resolve filesystem permissions and retry."
                ) from None
        raise
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
        if previous.exists() and not preserve_previous:
            shutil.rmtree(previous)


def main() -> None:
    parser = argparse.ArgumentParser(description="构建 CHEN 内容采集助手原生 App")
    parser.add_argument("--source", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--output", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--runtime-root")
    args = parser.parse_args()
    app_path = build_app(
        Path(args.source),
        Path(args.output),
        args.commit,
        Path(args.runtime_root) if args.runtime_root else None,
    )
    print(app_path)


if __name__ == "__main__":
    main()
