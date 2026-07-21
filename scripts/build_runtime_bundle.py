#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unicodedata
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath


ARCHITECTURES = ("arm64", "x86_64")
REPRODUCIBLE_MTIME = 315532800
YT_DLP_WRAPPER = """#!/bin/sh
SELF=$0
case "$SELF" in
    */*) ;;
    *) SELF=$(command -v -- "$SELF") || exit 127 ;;
esac
SCRIPT_DIR=$(CDPATH= cd -- "${SELF%/*}" && pwd) || exit 127
exec "$SCRIPT_DIR/../python/bin/python3" -m yt_dlp "$@"
"""


class LockedAsset:
    def __init__(self, name: str, version: str, url: str, sha256: str) -> None:
        self.name = name
        self.version = version
        self.url = url
        self.sha256 = sha256


class RuntimeLock:
    def __init__(
        self,
        python: dict[str, LockedAsset],
        wheels: dict[str, LockedAsset | dict[str, LockedAsset]],
        ffmpeg: dict[str, LockedAsset],
    ) -> None:
        self.python = python
        self.wheels: dict[str, dict[str, LockedAsset]] = {}
        for name, value in wheels.items():
            if isinstance(value, LockedAsset):
                self.wheels[name] = {arch: value for arch in ARCHITECTURES}
            else:
                if set(value) != set(ARCHITECTURES):
                    raise RuntimeError(
                        f"Architecture-specific wheel {name} must lock both architectures"
                    )
                self.wheels[name] = dict(value)
        self.ffmpeg = ffmpeg
        self.architectures = ARCHITECTURES
        all_assets = [*python.values()]
        for variants in self.wheels.values():
            all_assets.extend(variants.values())
        all_assets.extend(ffmpeg.values())
        self.assets = list(
            {(asset.url, asset.sha256): asset for asset in all_assets}.values()
        )

    @classmethod
    def load(cls, path: Path) -> RuntimeLock:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        python = {
            arch: _locked_asset("python", data["python"]["version"], data["python"][arch])
            for arch in ARCHITECTURES
        }
        wheels: dict[str, LockedAsset | dict[str, LockedAsset]] = {}
        for name, value in data["wheels"].items():
            if "url" in value:
                wheels[name] = _locked_asset(name, value["version"], value)
            else:
                wheels[name] = {
                    arch: _locked_asset(name, value["version"], value[arch])
                    for arch in ARCHITECTURES
                }
        ffmpeg = {
            arch: _locked_asset("ffmpeg", data["ffmpeg"]["version"], data["ffmpeg"][arch])
            for arch in ARCHITECTURES
        }
        lock = cls(python, wheels, ffmpeg)
        for asset in lock.assets:
            if len(asset.sha256) != 64 or any(
                character not in "0123456789abcdef" for character in asset.sha256
            ):
                raise RuntimeError(f"Invalid SHA-256 in runtime lock for {asset.name}")
            if not asset.url.startswith("https://"):
                raise RuntimeError(f"Runtime lock URL must use HTTPS for {asset.name}")
        return lock

    def wheels_for(self, arch: str) -> tuple[LockedAsset, ...]:
        if arch not in self.architectures:
            raise RuntimeError(f"Unsupported runtime architecture: {arch}")
        return tuple(variants[arch] for variants in self.wheels.values())


def _locked_asset(name: str, version: str, value: dict[str, str]) -> LockedAsset:
    return LockedAsset(name, version, value["url"], value["sha256"])


def verify_sha256(path: Path, expected: str) -> None:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != expected:
        raise RuntimeError(
            f"SHA-256 mismatch for {path}: expected {expected}, got {actual}"
        )


def cache_path_for(asset: LockedAsset, cache_root: Path) -> Path:
    url_path = urllib.parse.unquote(urllib.parse.urlsplit(asset.url).path)
    filename = Path(url_path).name
    if not filename:
        raise RuntimeError(f"Locked URL has no filename: {asset.url}")
    return Path(cache_root).expanduser().resolve() / f"{asset.sha256}-{filename}"


def cached_download(asset: LockedAsset, cache_root: Path) -> Path:
    cache_root = Path(cache_root).expanduser().resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    destination = cache_path_for(asset, cache_root)
    if destination.is_file():
        try:
            verify_sha256(destination, asset.sha256)
            return destination
        except RuntimeError:
            pass

    checksum_error: RuntimeError | None = None
    for _attempt in range(3):
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=cache_root
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with urllib.request.urlopen(asset.url) as response, temporary.open(
                "wb"
            ) as target:
                shutil.copyfileobj(response, target, length=1024 * 1024)
            try:
                verify_sha256(temporary, asset.sha256)
            except RuntimeError as error:
                checksum_error = error
                continue
            os.replace(temporary, destination)
            return destination
        finally:
            temporary.unlink(missing_ok=True)
    assert checksum_error is not None
    raise checksum_error


def _archive_parts(name: str, label: str) -> tuple[str, ...]:
    path = PurePosixPath(name)
    if not name or "\x00" in name or path.is_absolute() or ".." in path.parts:
        raise RuntimeError(f"unsafe {label} member path: {name!r}")
    parts = tuple(part for part in path.parts if part not in ("", "."))
    if not parts:
        raise RuntimeError(f"unsafe {label} member path: {name!r}")
    return parts


def _macos_archive_key(parts: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        unicodedata.normalize("NFD", component.casefold()) for component in parts
    )


def _normalized_link_target(
    member_parts: tuple[str, ...], linkname: str, *, hardlink: bool
) -> tuple[str, ...]:
    link = PurePosixPath(linkname)
    if not linkname or "\x00" in linkname or link.is_absolute():
        raise RuntimeError(f"unsafe tar link target: {linkname!r}")
    combined = [*(() if hardlink else member_parts[:-1]), *link.parts]
    normalized: list[str] = []
    for part in combined:
        if part in ("", "."):
            continue
        if part == "..":
            if not normalized:
                raise RuntimeError(f"unsafe tar link target: {linkname!r}")
            normalized.pop()
        else:
            normalized.append(part)
    if not normalized:
        raise RuntimeError(f"unsafe tar link target: {linkname!r}")
    return tuple(normalized)


def _safe_destination(root: Path, parts: tuple[str, ...], label: str) -> Path:
    root = root.resolve()
    destination = root.joinpath(*parts)
    resolved = destination.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise RuntimeError(f"unsafe {label} member path: {'/'.join(parts)!r}")
    return destination


def safe_extract_tar(archive_path: Path, output: Path) -> None:
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:*") as archive:
        members = archive.getmembers()
        indexed: list[tuple[tarfile.TarInfo, tuple[str, ...]]] = []
        member_by_parts: dict[tuple[str, ...], tarfile.TarInfo] = {}
        member_by_key: dict[tuple[str, ...], tarfile.TarInfo] = {}
        link_keys: set[tuple[str, ...]] = set()

        for member in members:
            parts = _archive_parts(member.name, "tar")
            key = _macos_archive_key(parts)
            if key in member_by_key:
                raise RuntimeError(f"unsafe tar duplicate member: {member.name!r}")
            if not (member.isdir() or member.isfile() or member.issym() or member.islnk()):
                raise RuntimeError(f"unsafe tar special member: {member.name!r}")
            member_by_parts[parts] = member
            member_by_key[key] = member
            indexed.append((member, parts))
            if member.issym() or member.islnk():
                link_keys.add(key)

        for member, parts in indexed:
            key = _macos_archive_key(parts)
            if any(key[:index] in link_keys for index in range(1, len(key))):
                raise RuntimeError(f"unsafe tar member below link: {member.name!r}")
            if member.issym():
                _normalized_link_target(parts, member.linkname, hardlink=False)
            elif member.islnk():
                target_parts = _normalized_link_target(parts, member.linkname, hardlink=True)
                target = member_by_parts.get(target_parts)
                if target is None or not target.isfile():
                    raise RuntimeError(f"unsafe tar hardlink target: {member.linkname!r}")

        directories: list[tuple[tarfile.TarInfo, Path]] = []
        for member, parts in indexed:
            if not member.isdir():
                continue
            destination = _safe_destination(output, parts, "tar")
            destination.mkdir(parents=True, exist_ok=True)
            directories.append((member, destination))

        for member, parts in indexed:
            if not member.isfile():
                continue
            destination = _safe_destination(output, parts, "tar")
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError(f"Could not read tar member: {member.name!r}")
            with source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)
            destination.chmod(member.mode & 0o777)
            os.utime(destination, (member.mtime, member.mtime))

        for member, parts in indexed:
            if not member.issym():
                continue
            destination = _safe_destination(output, parts, "tar")
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(member.linkname, destination)

        for member, parts in indexed:
            if not member.islnk():
                continue
            destination = _safe_destination(output, parts, "tar")
            target_parts = _normalized_link_target(parts, member.linkname, hardlink=True)
            target = _safe_destination(output, target_parts, "tar")
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.link(target, destination)

        for member, destination in reversed(directories):
            destination.chmod(member.mode & 0o777)
            os.utime(destination, (member.mtime, member.mtime))


def _validated_wheel_members(
    archive: zipfile.ZipFile,
) -> list[tuple[zipfile.ZipInfo, tuple[str, ...], int]]:
    indexed: list[tuple[zipfile.ZipInfo, tuple[str, ...], int]] = []
    seen: set[tuple[str, ...]] = set()
    for member in archive.infolist():
        parts = _archive_parts(member.filename, "wheel")
        key = _macos_archive_key(parts)
        if key in seen:
            raise RuntimeError(f"unsafe wheel duplicate member: {member.filename!r}")
        seen.add(key)
        mode = member.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise RuntimeError(f"unsafe wheel symlink member: {member.filename!r}")
        file_type = stat.S_IFMT(mode)
        if file_type and not member.is_dir() and not stat.S_ISREG(mode):
            raise RuntimeError(f"unsafe wheel special member: {member.filename!r}")
        indexed.append((member, parts, mode))
    return indexed


def safe_extract_wheel(wheel_path: Path, output: Path) -> None:
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(wheel_path) as archive:
        indexed = _validated_wheel_members(archive)

        for member, parts, mode in indexed:
            destination = _safe_destination(output, parts, "wheel")
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)
            permissions = stat.S_IMODE(mode)
            destination.chmod(permissions or 0o644)


def extract_ffmpeg(wheel_path: Path, tools: Path) -> Path:
    tools = Path(tools)
    with zipfile.ZipFile(wheel_path) as archive:
        candidates = []
        for member, parts, _mode in _validated_wheel_members(archive):
            if parts[-1].startswith("ffmpeg-") and not member.is_dir():
                candidates.append(member)
        if len(candidates) != 1:
            raise RuntimeError(
                f"Expected exactly one imageio ffmpeg binary, found {len(candidates)}"
            )
        tools.mkdir(parents=True, exist_ok=True)
        destination = _safe_destination(tools, ("ffmpeg",), "wheel")
        with archive.open(candidates[0]) as source, destination.open("wb") as target:
            shutil.copyfileobj(source, target)
    destination.chmod(0o755)
    return destination


def install_yt_dlp_wrapper(tools: Path) -> Path:
    tools = Path(tools)
    tools.mkdir(parents=True, exist_ok=True)
    wrapper = tools / "yt-dlp"
    wrapper.write_text(YT_DLP_WRAPPER, encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper


def host_architecture() -> str:
    result = subprocess.run(
        ["/usr/bin/uname", "-m"], check=True, capture_output=True, text=True
    )
    architecture = result.stdout.strip()
    if architecture not in ARCHITECTURES:
        raise RuntimeError(f"Unsupported host architecture: {architecture}")
    return architecture


def macho_architectures(path: Path) -> frozenset[str]:
    path = Path(path)
    if not path.is_file():
        raise RuntimeError(f"Missing executable for architecture validation: {path}")
    result = subprocess.run(
        ["/usr/bin/lipo", "-archs", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    architectures = frozenset(result.stdout.split())
    if result.returncode != 0 or not architectures:
        detail = result.stderr.strip() or result.stdout.strip() or "not a Mach-O binary"
        raise RuntimeError(f"Mach-O architecture validation failed for {path}: {detail}")
    return architectures


def assert_architecture(path: Path, expected: str) -> None:
    architectures = macho_architectures(path)
    if expected not in architectures:
        actual = " ".join(sorted(architectures))
        raise RuntimeError(
            f"Mach-O architecture mismatch for {path}: expected {expected}; got {actual}"
        )


def validate_runtime(root: Path, arch: str) -> None:
    root = Path(root)
    python = root / "python/bin/python3"
    site_packages = root / "python/lib/python3.12/site-packages"
    ffmpeg = root / "tools/ffmpeg"
    yt_dlp = root / "tools/yt-dlp"
    assert_architecture(python, arch)
    assert_architecture(ffmpeg, arch)
    try:
        wrapper_contents = yt_dlp.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise RuntimeError("Missing or unreadable yt-dlp wrapper") from None
    if wrapper_contents != YT_DLP_WRAPPER:
        raise RuntimeError("Invalid non-relocatable yt-dlp wrapper")
    if stat.S_IMODE(yt_dlp.stat().st_mode) != 0o755:
        raise RuntimeError("yt-dlp wrapper must have mode 0755")

    playwright_nodes = [
        path
        for path in site_packages.glob("playwright/**/node")
        if path.is_file() and os.access(path, os.X_OK)
    ]
    if not playwright_nodes:
        raise RuntimeError("Missing executable Playwright native driver/node component")
    for path in playwright_nodes:
        architectures = macho_architectures(path)
        if arch not in architectures:
            actual = " ".join(sorted(architectures))
            raise RuntimeError(
                f"Playwright architecture mismatch for {path}: "
                f"expected {arch}; got {actual}"
            )

    greenlet_extensions = list(site_packages.glob("greenlet/_greenlet*.so"))
    if len(greenlet_extensions) != 1:
        raise RuntimeError(
            f"Expected one greenlet extension, found {len(greenlet_extensions)}"
        )
    assert_architecture(greenlet_extensions[0], arch)

    if arch == host_architecture():
        node = playwright_nodes[0]
        cli = node.parent / "package/cli.js"
        if not cli.is_file():
            raise RuntimeError("Missing Playwright bundled driver CLI")
        cli_validation_environment = {
            "PATH": str((root / "tools").resolve()),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        with tempfile.TemporaryDirectory(prefix="chen-runtime-validation-") as cwd:
            subprocess.run(
                [
                    str(python),
                    "-I",
                    "-B",
                    "-c",
                    "from pathlib import Path; import shutil, subprocess, sys; "
                    "assert sys.version_info[:2] == (3, 12); "
                    "site_packages = Path(sys.argv[1]).resolve(); "
                    "yt_dlp_cli = Path(sys.argv[2]).resolve(); "
                    "import playwright, yt_dlp, greenlet; "
                    "modules = (playwright, yt_dlp, greenlet); "
                    "assert all(Path(module.__file__).resolve().is_relative_to("
                    "site_packages) for module in modules); "
                    "assert Path(shutil.which('yt-dlp')).resolve() == yt_dlp_cli; "
                    "subprocess.run(['yt-dlp', '--version'], check=True); "
                    "print(sys.version.split()[0])",
                    str(site_packages.resolve()),
                    str(yt_dlp.resolve()),
                ],
                check=True,
                cwd=cwd,
                env=cli_validation_environment,
            )
            subprocess.run(
                [str(node), str(cli), "--version"],
                check=True,
                cwd=cwd,
                env={},
            )


class RuntimeBundleBuilder:
    def __init__(self, lock: RuntimeLock, cache_root: Path) -> None:
        self.lock = lock
        self.cache_root = Path(cache_root)

    def build(self, arch: str, output: Path) -> Path:
        if arch not in self.lock.architectures:
            raise RuntimeError(f"Unsupported runtime architecture: {arch}")

        output = Path(output).expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        final = output / arch
        rollback = output / f".{arch}.rollback"
        if _path_exists(rollback):
            if _path_exists(final):
                raise RuntimeError(
                    "Existing runtime recovery copy is present; refusing to replace it. "
                    "Restore or remove it after verifying it is no longer needed."
                )
            try:
                os.replace(rollback, final)
            except OSError:
                raise RuntimeError(
                    "Runtime recovery copy could not be restored; it was retained. "
                    "Resolve filesystem permissions and retry."
                ) from None
        temporary = Path(tempfile.mkdtemp(prefix=f".{arch}.", dir=output))
        rollback_created = False
        try:
            python_archive = cached_download(self.lock.python[arch], self.cache_root)
            wheel_archives = [
                cached_download(asset, self.cache_root)
                for asset in self.lock.wheels_for(arch)
            ]
            ffmpeg_wheel = cached_download(self.lock.ffmpeg[arch], self.cache_root)

            safe_extract_tar(python_archive, temporary)
            site_packages = temporary / "python/lib/python3.12/site-packages"
            site_packages.mkdir(parents=True, exist_ok=True)
            for wheel in wheel_archives:
                safe_extract_wheel(wheel, site_packages)
            extract_ffmpeg(ffmpeg_wheel, temporary / "tools")
            install_yt_dlp_wrapper(temporary / "tools")
            validate_runtime(temporary, arch)
            normalize_tree_metadata(temporary)

            if _path_exists(final):
                if _path_exists(rollback):
                    raise RuntimeError(
                        "Existing runtime recovery copy is present; refusing to replace it. "
                        "Restore or remove it after verifying it is no longer needed."
                    )
                os.replace(final, rollback)
                rollback_created = True
            try:
                os.replace(temporary, final)
            except OSError:
                if rollback_created:
                    try:
                        os.replace(rollback, final)
                    except OSError:
                        raise RuntimeError(
                            "Runtime replacement failed; recovery copy was retained. "
                            "Resolve filesystem permissions and retry."
                        ) from None
                    rollback_created = False
                raise
            if rollback_created:
                _remove_path(rollback)
                rollback_created = False
            return final
        finally:
            _remove_path(temporary)


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def normalize_tree_metadata(root: Path) -> None:
    root = Path(root)
    for current, directories, files in os.walk(root, topdown=False, followlinks=False):
        current_path = Path(current)
        for name in [*files, *directories]:
            path = current_path / name
            if path.is_symlink():
                os.chmod(path, 0o755, follow_symlinks=False)
            else:
                if path.is_dir():
                    path.chmod(0o755)
                elif path.is_file():
                    executable = stat.S_IMODE(path.stat().st_mode) & 0o111
                    path.chmod(0o755 if executable else 0o644)
            os.utime(
                path,
                (REPRODUCIBLE_MTIME, REPRODUCIBLE_MTIME),
                follow_symlinks=False,
            )
    root.chmod(0o755)
    os.utime(root, (REPRODUCIBLE_MTIME, REPRODUCIBLE_MTIME))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build locked arm64 and x86_64 macOS runtime bundles"
    )
    parser.add_argument("--lock", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    lock = RuntimeLock.load(Path(args.lock))
    builder = RuntimeBundleBuilder(lock, Path(args.cache))
    for architecture in lock.architectures:
        print(builder.build(architecture, Path(args.output)))


if __name__ == "__main__":
    main()
