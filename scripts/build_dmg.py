#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Sequence


APP_NAME = "CHEN 内容采集助手.app"
VOLUME_NAME = "CHEN 内容采集助手"
GUIDE_NAME = "首次打开说明.txt"
BUNDLE_IDENTIFIER = "com.chen.content-collector.native"
EXECUTABLE_NAME = "CHEN内容采集助手"
MINIMUM_MACOS = "12.0"
ARCHITECTURES = ("arm64", "x86_64")
CANONICAL_SOURCE_ROOT = Path(__file__).resolve().parents[1]
RELEASE_VERSION = "1.0.0"
CONTENT_SCAN_CHUNK_BYTES = 64 * 1024
ASSIGNMENT_SCAN_OVERLAP_BYTES = 64 * 1024
COOKIE_LINE_MAX_BYTES = 256 * 1024

FORBIDDEN_BASENAMES = frozenset(
    {
        ".env",
        ".feishu_token_cache.json",
        "config.json",
        "cookies.txt",
        "cookie.txt",
        "cookies.json",
        "cookie.json",
        "desktop_collector.sqlite3",
        "runtime-state.json",
    }
)
FORBIDDEN_DIRECTORY_NAMES = frozenset(
    {
        ".publisher-state",
        "browser-profile",
        "browser-profile-cdp",
    }
)
DATABASE_SUFFIXES = (
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".sqlite",
    ".sqlite-journal",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
    ".sqlite3-journal",
    ".sqlite3-shm",
    ".sqlite3-wal",
)
VIDEO_SUFFIXES = (".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm")
NETSCAPE_COOKIE_HEADER = b"# netscape http cookie file"
SECRET_ENVIRONMENT_NAME = re.compile(
    r"(?:^|_)(?:secret|token|password|api_key|access_key|private_key|credential)"
    r"(?:$|_)",
    re.IGNORECASE,
)
ASSIGNMENT_FIELD = rb"[A-Za-z_][A-Za-z0-9_-]*"
QUOTED_SECRET_ASSIGNMENT = re.compile(
    rb"(?:[\"'])?(?P<field>" + ASSIGNMENT_FIELD + rb")(?:[\"'])?"
    rb"[\t ]*(?:=|:)[\t ]*(?:[rubf]{0,2})?"
    rb"(?:\"(?P<double>[^\"\r\n]*)\"|'(?P<single>[^'\r\n]*)')",
    re.IGNORECASE,
)
CHINESE_PLACEHOLDER_PREFIX = "在这里填".encode()
SAFE_NON_SECRET_LITERALS = frozenset(
    {b"change-me", b"changeme", b"placeholder", b"replace-me"}
)
ENVIRONMENT_REFERENCE = re.compile(
    rb"(?:\$[A-Za-z_][A-Za-z0-9_]*|\$\{[A-Za-z_][A-Za-z0-9_]*\})"
)
UNQUOTED_SECRET_ASSIGNMENT = re.compile(
    rb"(?m)^[\t ]*(?:export[\t ]+)?(?P<field>"
    + ASSIGNMENT_FIELD
    + rb")[\t ]*(?:=|:)[\t ]*(?P<value>[^\r\n#]*)$",
    re.IGNORECASE,
)
PYTHON_ASSIGNMENT_AFTER_FIELD = re.compile(
    rb"[\t \r\n]*(?:=|:.*?=)", re.DOTALL
)
ASSIGNMENT_FILE_SUFFIXES = frozenset(
    {
        ".cfg",
        ".conf",
        ".env",
        ".ini",
        ".js",
        ".json",
        ".jsx",
        ".properties",
        ".py",
        ".sh",
        ".toml",
        ".ts",
        ".tsx",
        ".yaml",
        ".yml",
    }
)
UNQUOTED_ASSIGNMENT_SUFFIXES = frozenset(
    {".cfg", ".conf", ".env", ".ini", ".properties", ".toml", ".yaml", ".yml"}
)
SOURCE_CODE_SUFFIXES = frozenset({".js", ".jsx", ".py", ".sh", ".ts", ".tsx"})
REQUIRED_RESOURCES = (
    Path("orange-app-icon.icns"),
    Path("THIRD_PARTY_NOTICES.md"),
    Path("Runtime/runtime-lock.json"),
    Path("Runtime/arm64"),
    Path("Runtime/x86_64"),
    Path("CollectorPayload"),
)

CommandRunner = Callable[..., subprocess.CompletedProcess]
ReleaseVerifier = Callable[[Path], None]
RuntimeBuilder = Callable[[Path, Path, Path], None]
AppBuilder = Callable[[Path, Path, str, Path], Path]
DmgBuilder = Callable[[Path, Path, str, str], Dict[str, Any]]


def _load_script(source_root: Path, name: str):
    path = source_root / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"chen_release_{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载正式发布脚本：{path.name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assert_release_source(
    source_root: Path,
    *,
    canonical_source: Path = CANONICAL_SOURCE_ROOT,
) -> str:
    source_root = Path(source_root).expanduser().resolve()
    canonical_source = Path(canonical_source).expanduser().resolve()
    if source_root != canonical_source:
        raise RuntimeError(f"拒绝构建：唯一正式来源是 {canonical_source}。")

    try:
        def git_output(*args: str) -> str:
            return subprocess.run(
                ["git", "-C", str(source_root), *args],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        head = git_output("rev-parse", "HEAD")
        master = git_output("rev-parse", "refs/heads/master")
        if head != master or git_output("status", "--porcelain"):
            raise RuntimeError("source is not a clean master")
        return head
    except (RuntimeError, subprocess.CalledProcessError) as error:
        raise RuntimeError("拒绝构建：来源必须是干净 master。") from error


def _run_full_verification(source_root: Path) -> None:
    _load_script(source_root, "deploy_desktop_app").run_verification(source_root)


def _create_commit_snapshot(
    source_root: Path,
    commit: str,
    snapshot_root: Path,
) -> None:
    archive_path = snapshot_root.parent / "source.tar"
    snapshot_root.mkdir()
    try:
        subprocess.run(
            [
                "git",
                "-C",
                str(source_root),
                "archive",
                "--format=tar",
                f"--output={archive_path}",
                commit,
            ],
            check=True,
            capture_output=True,
        )
        with archive_path.open("rb") as archive_stream:
            archived_commit = subprocess.run(
                ["git", "get-tar-commit-id"],
                stdin=archive_stream,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        if archived_commit != commit:
            raise RuntimeError("commit snapshot identity mismatch")

        tracked_result = subprocess.run(
            [
                "git",
                "-C",
                str(source_root),
                "ls-tree",
                "-rz",
                "--name-only",
                commit,
            ],
            check=True,
            capture_output=True,
        )
        tracked_paths = {
            os.fsdecode(name)
            for name in tracked_result.stdout.split(b"\0")
            if name
        }

        with tarfile.open(archive_path, "r:") as archive:
            members = archive.getmembers()
            archived_paths = {
                member.name
                for member in members
                if member.isfile() or member.issym()
            }
            if archived_paths != tracked_paths:
                raise RuntimeError("commit snapshot does not contain every tracked entry")
            for member in members:
                member_path = Path(member.name)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise RuntimeError("commit snapshot contains an unsafe path")
                if member.issym():
                    link_path = Path(member.linkname)
                    resolved_link = member_path.parent / link_path
                    if link_path.is_absolute() or ".." in resolved_link.parts:
                        raise RuntimeError("commit snapshot contains an unsafe symlink")
                elif member.islnk():
                    raise RuntimeError("commit snapshot contains an unsafe hard link")
            archive.extractall(snapshot_root)
    except (OSError, subprocess.CalledProcessError, tarfile.TarError, RuntimeError) as error:
        raise RuntimeError("拒绝构建：无法创建已验证 commit snapshot。") from error
    finally:
        archive_path.unlink(missing_ok=True)


def _build_locked_runtimes(
    source_root: Path,
    runtime_cache: Path,
    runtime_root: Path,
) -> None:
    runtime_module = _load_script(source_root, "build_runtime_bundle")
    lock = runtime_module.RuntimeLock.load(source_root / "packaging/runtime-lock.json")
    builder = runtime_module.RuntimeBundleBuilder(lock, runtime_cache)
    for architecture in lock.architectures:
        builder.build(architecture, runtime_root)


def _build_universal_app(
    source_root: Path,
    app_path: Path,
    commit: str,
    runtime_root: Path,
) -> Path:
    return _load_script(source_root, "build_desktop_app").build_app(
        source_root,
        app_path,
        commit,
        runtime_root,
    )


def release_dmg(
    source_root: Path,
    runtime_cache: Path,
    output_dir: Path,
    *,
    canonical_source: Path = CANONICAL_SOURCE_ROOT,
    verify_fn: ReleaseVerifier = _run_full_verification,
    runtime_builder_fn: RuntimeBuilder = _build_locked_runtimes,
    app_builder_fn: AppBuilder = _build_universal_app,
    dmg_builder_fn: DmgBuilder | None = None,
) -> Dict[str, Any]:
    source_root = Path(source_root).expanduser().resolve()
    runtime_cache = Path(runtime_cache).expanduser().resolve()
    output_dir = Path(output_dir).expanduser().resolve()
    commit = assert_release_source(source_root, canonical_source=canonical_source)

    with tempfile.TemporaryDirectory(
        prefix=".dmg-release-", dir=output_dir.parent
    ) as staging_name:
        staging_root = Path(staging_name)
        verify_fn(source_root)
        verified_commit = assert_release_source(
            source_root,
            canonical_source=canonical_source,
        )
        if verified_commit != commit:
            raise RuntimeError("拒绝构建：验证后 master commit 已变化。")
        snapshot_root = staging_root / "build-source"
        _create_commit_snapshot(source_root, commit, snapshot_root)
        runtime_root = staging_root / "runtime"
        runtime_builder_fn(snapshot_root, runtime_cache, runtime_root)
        app_path = staging_root / APP_NAME
        app_path = app_builder_fn(snapshot_root, app_path, commit, runtime_root)
        image_path = output_dir / f"CHEN-内容采集助手-{RELEASE_VERSION}-universal.dmg"
        builder = dmg_builder_fn or create_dmg
        return builder(app_path, image_path, RELEASE_VERSION, commit)


def _relative(path: Path, root: Path) -> str:
    relative = path.relative_to(root)
    return relative.as_posix() if relative.parts else "."


def _reject(path: Path, root: Path) -> None:
    raise RuntimeError(f"distribution contains forbidden data: {_relative(path, root)}")


def _check_distribution_name(path: Path, root: Path, *, is_directory: bool) -> None:
    name = path.name.casefold()
    if name in FORBIDDEN_BASENAMES:
        _reject(path, root)
    if is_directory and name in FORBIDDEN_DIRECTORY_NAMES:
        _reject(path, root)
    if not is_directory and name.endswith((*DATABASE_SUFFIXES, *VIDEO_SUFFIXES)):
        _reject(path, root)


def _known_environment_secrets() -> tuple[bytes, ...]:
    values = {
        value.encode("utf-8", errors="surrogateescape")
        for name, value in os.environ.items()
        if value and SECRET_ENVIRONMENT_NAME.search(name)
    }
    return tuple(sorted(values, key=len, reverse=True))


def _personal_path_patterns() -> tuple[bytes, ...]:
    candidates = {str(Path.home())}
    environment_home = os.environ.get("HOME")
    if environment_home:
        candidates.add(environment_home)
    return tuple(
        sorted(
            {
                (candidate.rstrip("/") + "/").encode(
                    "utf-8", errors="surrogateescape"
                ).lower()
                for candidate in candidates
                if candidate.startswith("/Users/") and candidate != "/Users"
            },
            key=len,
            reverse=True,
        )
    )


def _is_bundled_runtime_source(path: Path, app_bundle_root: Path | None) -> bool:
    if app_bundle_root is None or path.suffix.casefold() not in SOURCE_CODE_SUFFIXES:
        return False
    try:
        relative = path.relative_to(app_bundle_root)
    except ValueError:
        return False
    return (
        len(relative.parts) > 4
        and relative.parts[:3] == ("Contents", "Resources", "Runtime")
        and relative.parts[3] in ARCHITECTURES
    )


def _is_assignment_file(path: Path, *, app_bundle_root: Path | None) -> bool:
    name = path.name.casefold()
    suffixes = {suffix.casefold() for suffix in path.suffixes}
    if _is_bundled_runtime_source(path, app_bundle_root):
        return False
    return bool(suffixes & ASSIGNMENT_FILE_SUFFIXES) or ".env" in name


def _assignment_parser(path: Path) -> str | None:
    for suffix in reversed(path.suffixes):
        normalized = suffix.casefold()
        if normalized == ".py":
            return "python"
        if normalized == ".json":
            return "json"
    return None


def _allows_unquoted_assignment(path: Path) -> bool:
    name = path.name.casefold()
    suffixes = {suffix.casefold() for suffix in path.suffixes}
    return bool(suffixes & UNQUOTED_ASSIGNMENT_SUFFIXES) or ".env" in name


def _is_secret_field_name(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")
    if normalized in {
        "app_secret",
        "api_key",
        "access_token",
        "device_token",
        "password",
        "private_key",
        "publisher_token",
        "publisher_device_token",
        "publisher_token_pepper",
        "secret_id",
        "secret_key",
        "service_role_key",
        "supabase_service_role_key",
    }:
        return True
    return normalized.endswith(
        (
            "_access_token",
            "_api_key",
            "_app_secret",
            "_cookie_secret",
            "_device_token",
            "_password",
            "_private_key",
            "_secret_id",
            "_secret_key",
            "_service_role_key",
            "_session_secret",
            "_token_pepper",
        )
    )


def _is_safe_secret_value(value: bytes | str) -> bool:
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    encoded = encoded.strip()
    if len(encoded) >= 2 and encoded[:1] == encoded[-1:] and encoded[:1] in {
        b'"',
        b"'",
    }:
        encoded = encoded[1:-1].strip()
    if not encoded:
        return True
    lowered = encoded.lower()
    if encoded.startswith(CHINESE_PLACEHOLDER_PREFIX):
        return True
    if lowered in SAFE_NON_SECRET_LITERALS:
        return True
    if lowered.startswith((b"your-", b"your_")):
        return True
    if ENVIRONMENT_REFERENCE.fullmatch(encoded):
        return True
    if lowered in {b"redacted", b"[redacted]", b"<redacted>"}:
        return True
    mask_characters = set(b"*x:- ")
    return bool(set(lowered) & set(b"*x")) and not (set(lowered) - mask_characters)


def _contains_forbidden_text_assignment(
    content: bytes, *, allow_unquoted: bool
) -> bool:
    for match in QUOTED_SECRET_ASSIGNMENT.finditer(content):
        field = match.group("field").decode("ascii", errors="ignore")
        value = match.group("double")
        if value is None:
            value = match.group("single")
        if _is_secret_field_name(field) and not _is_safe_secret_value(value):
            return True
    if allow_unquoted:
        for match in UNQUOTED_SECRET_ASSIGNMENT.finditer(content):
            field = match.group("field").decode("ascii", errors="ignore")
            if _is_secret_field_name(field) and not _is_safe_secret_value(
                match.group("value")
            ):
                return True
    return False


def _assignment_target_names(target: ast.AST) -> tuple[str, ...]:
    if isinstance(target, ast.Name):
        return (target.id,)
    if isinstance(target, ast.Attribute):
        return (target.attr,)
    if isinstance(target, ast.Subscript):
        if isinstance(target.slice, ast.Constant) and isinstance(target.slice.value, str):
            return (target.slice.value,)
        return ()
    if isinstance(target, (ast.List, ast.Tuple)):
        return tuple(
            name for element in target.elts for name in _assignment_target_names(element)
        )
    return ()


def _literal_secret_value(node: ast.AST | None) -> bytes | str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (bytes, str)):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_secret_value(node.left)
        right = _literal_secret_value(node.right)
        if isinstance(left, str) and isinstance(right, str):
            return left + right
        if isinstance(left, bytes) and isinstance(right, bytes):
            return left + right
    return None


def _python_has_secret_assignment_hint(content: bytes) -> bool:
    for match in re.finditer(ASSIGNMENT_FIELD, content):
        field = match.group().decode("ascii", errors="ignore")
        if _is_secret_field_name(field) and PYTHON_ASSIGNMENT_AFTER_FIELD.match(
            content, match.end()
        ):
            return True
    return False


def _python_has_forbidden_secret_assignment(content: bytes) -> bool | None:
    try:
        tree = ast.parse(content.decode("utf-8-sig"))
    except (SyntaxError, UnicodeDecodeError, ValueError):
        return None

    for node in ast.walk(tree):
        assignments: list[tuple[str, ast.AST | None]] = []
        if isinstance(node, ast.Assign):
            assignments.extend(
                (name, node.value)
                for target in node.targets
                for name in _assignment_target_names(target)
            )
        elif isinstance(node, ast.AnnAssign):
            assignments.extend(
                (name, node.value) for name in _assignment_target_names(node.target)
            )
        elif isinstance(node, ast.Dict):
            assignments.extend(
                (key.value, value)
                for key, value in zip(node.keys, node.values)
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            )
        elif isinstance(node, ast.Call):
            assignments.extend(
                (keyword.arg, keyword.value)
                for keyword in node.keywords
                if keyword.arg is not None
            )
        for field, value_node in assignments:
            value = _literal_secret_value(value_node)
            if (
                _is_secret_field_name(field)
                and value is not None
                and not _is_safe_secret_value(value)
            ):
                return True
    return False


def _json_has_forbidden_secret_assignment(content: bytes) -> bool | None:
    try:
        payload = json.loads(content.decode("utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    def contains_forbidden(value: Any) -> bool:
        if isinstance(value, dict):
            for field, field_value in value.items():
                if isinstance(field, str) and _is_secret_field_name(field):
                    if isinstance(field_value, str):
                        if not _is_safe_secret_value(field_value):
                            return True
                    elif field_value not in (None, False):
                        return True
                if contains_forbidden(field_value):
                    return True
        elif isinstance(value, list):
            return any(contains_forbidden(item) for item in value)
        return False

    return contains_forbidden(payload)


def _is_netscape_cookie_line(line: bytes) -> bool:
    fields = line.rstrip(b"\r").split(b"\t")
    if len(fields) != 7:
        return False
    domain, include_subdomains, path, secure, expires, name, _ = fields
    if domain.startswith(b"#HttpOnly_"):
        domain = domain[len(b"#HttpOnly_") :]
    return bool(
        domain
        and not any(character in domain for character in b" \r\n")
        and include_subdomains.upper() in {b"TRUE", b"FALSE"}
        and path.startswith(b"/")
        and secure.upper() in {b"TRUE", b"FALSE"}
        and expires.isdigit()
        and name
    )


def _scan_cookie_line(line: bytes, header_seen: bool) -> tuple[bool, bool]:
    if NETSCAPE_COOKIE_HEADER in line.lower():
        header_seen = True
    return header_seen, header_seen and _is_netscape_cookie_line(line)


def _check_file_content(
    path: Path,
    root: Path,
    environment_secrets: Sequence[bytes],
    *,
    app_bundle_root: Path | None,
) -> None:
    personal_paths = _personal_path_patterns()
    overlap_size = max(
        ASSIGNMENT_SCAN_OVERLAP_BYTES,
        *(len(value) for value in environment_secrets),
        *(len(value) for value in personal_paths),
    )
    overlap = b""
    assignment_file = _is_assignment_file(path, app_bundle_root=app_bundle_root)
    parser = _assignment_parser(path) if assignment_file else None
    python_source = bytearray() if parser == "python" else None
    json_source = bytearray() if parser == "json" else None
    cookie_header_seen = False
    cookie_line_buffer = b""
    discarding_cookie_line = False
    try:
        with path.open("rb") as source:
            while chunk := source.read(CONTENT_SCAN_CHUNK_BYTES):
                if python_source is not None:
                    python_source.extend(chunk)
                if json_source is not None:
                    json_source.extend(chunk)
                window = overlap + chunk
                lowered = window.lower()
                if any(pattern in lowered for pattern in personal_paths):
                    _reject(path, root)
                if any(value in window for value in environment_secrets):
                    _reject(path, root)
                cookie_parts = chunk.split(b"\n")
                for index, part in enumerate(cookie_parts):
                    line_complete = index < len(cookie_parts) - 1
                    if discarding_cookie_line:
                        if line_complete:
                            discarding_cookie_line = False
                        continue
                    if len(cookie_line_buffer) + len(part) > COOKIE_LINE_MAX_BYTES:
                        cookie_line_buffer = b""
                        discarding_cookie_line = not line_complete
                        continue
                    cookie_line_buffer += part
                    if line_complete:
                        cookie_header_seen, found_cookie = _scan_cookie_line(
                            cookie_line_buffer, cookie_header_seen
                        )
                        cookie_line_buffer = b""
                        if found_cookie:
                            _reject(path, root)
                if (
                    assignment_file
                    and python_source is None
                    and json_source is None
                ):
                    if _contains_forbidden_text_assignment(
                        window, allow_unquoted=_allows_unquoted_assignment(path)
                    ):
                        _reject(path, root)
                overlap = window[-overlap_size:]
        if not discarding_cookie_line:
            cookie_header_seen, found_cookie = _scan_cookie_line(
                cookie_line_buffer, cookie_header_seen
            )
            if found_cookie:
                _reject(path, root)
        if python_source is not None:
            ast_result = _python_has_forbidden_secret_assignment(bytes(python_source))
            if ast_result is True:
                _reject(path, root)
            if ast_result is None:
                source_bytes = bytes(python_source)
                if _python_has_secret_assignment_hint(source_bytes):
                    _reject(path, root)
        if json_source is not None:
            json_result = _json_has_forbidden_secret_assignment(bytes(json_source))
            if json_result is not False:
                _reject(path, root)
    except OSError as error:
        raise RuntimeError(
            f"unable to inspect distribution entry: {_relative(path, root)}"
        ) from error


def _check_symlink(
    path: Path,
    root: Path,
    resolved_root: Path,
    *,
    allow_applications_link: bool,
) -> None:
    try:
        target = os.readlink(path)
    except OSError as error:
        raise RuntimeError(
            f"unable to inspect distribution entry: {_relative(path, root)}"
        ) from error
    if (
        allow_applications_link
        and path == root / "Applications"
        and target == "/Applications"
    ):
        return
    if os.path.isabs(target):
        _reject(path, root)
    try:
        resolved_target = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise RuntimeError(
            f"distribution contains forbidden data: {_relative(path, root)}"
        ) from error
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError:
        _reject(path, root)


def scan_distribution_tree(
    root: Path,
    *,
    allow_applications_link: bool = False,
    app_bundle_root: Path | None = None,
) -> None:
    root = Path(root)
    app_bundle_root = Path(app_bundle_root) if app_bundle_root is not None else None
    if app_bundle_root is not None:
        try:
            app_bundle_root.relative_to(root)
        except ValueError as error:
            raise RuntimeError("App scan context must be inside distribution root") from error
        if (
            app_bundle_root.name != APP_NAME
            or not app_bundle_root.is_dir()
            or app_bundle_root.is_symlink()
        ):
            raise RuntimeError("App scan context is invalid")
    environment_secrets = _known_environment_secrets()
    try:
        root_mode = root.lstat().st_mode
    except FileNotFoundError as error:
        raise RuntimeError("distribution tree does not exist") from error
    if stat.S_ISLNK(root_mode):
        raise RuntimeError("distribution root must not be a symlink")
    if stat.S_ISREG(root_mode):
        _check_distribution_name(root, root.parent, is_directory=False)
        _check_file_content(
            root, root.parent, environment_secrets, app_bundle_root=app_bundle_root
        )
        return
    if not stat.S_ISDIR(root_mode):
        raise RuntimeError("distribution root must be a directory")
    resolved_root = root.resolve(strict=True)

    def raise_walk_error(error: OSError) -> None:
        error_path = Path(error.filename) if error.filename else root
        try:
            relative = _relative(error_path, root)
        except ValueError:
            relative = error_path.name or "."
        raise RuntimeError(
            f"unable to traverse distribution entry: {relative}"
        ) from error

    for current_name, directories, files in os.walk(
        root, topdown=True, onerror=raise_walk_error, followlinks=False
    ):
        current = Path(current_name)
        directories.sort()
        files.sort()
        for name in [*directories, *files]:
            path = current / name
            mode = path.lstat().st_mode
            _check_distribution_name(path, root, is_directory=stat.S_ISDIR(mode))
            if stat.S_ISLNK(mode):
                _check_symlink(
                    path,
                    root,
                    resolved_root,
                    allow_applications_link=allow_applications_link,
                )
            elif stat.S_ISREG(mode):
                _check_file_content(
                    path, root, environment_secrets, app_bundle_root=app_bundle_root
                )
            elif not stat.S_ISDIR(mode):
                _reject(path, root)


def prepare_staging(app_path: Path, guide_path: Path, staging: Path) -> Path:
    app_path = Path(app_path)
    guide_path = Path(guide_path)
    staging = Path(staging)
    if app_path.name != APP_NAME or not app_path.is_dir() or app_path.is_symlink():
        raise RuntimeError(f"expected source App named {APP_NAME}")
    if not guide_path.is_file() or guide_path.is_symlink():
        raise RuntimeError(f"missing {GUIDE_NAME}")
    if staging.exists() or staging.is_symlink():
        raise RuntimeError("temporary staging path already exists")

    staging.mkdir(parents=True)
    shutil.copytree(app_path, staging / APP_NAME, symlinks=True)
    (staging / "Applications").symlink_to("/Applications")
    shutil.copy2(guide_path, staging / GUIDE_NAME)
    return staging


def _run(
    command: Sequence[str | Path], command_runner: CommandRunner
) -> subprocess.CompletedProcess:
    normalized = [str(part) for part in command]
    try:
        return command_runner(
            normalized,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        operation = Path(normalized[0]).name
        if operation == "hdiutil" and len(normalized) > 1:
            operation = f"hdiutil {normalized[1]}"
        raise RuntimeError(f"distribution command failed: {operation}") from error


def _load_info(app_path: Path) -> Dict[str, Any]:
    try:
        return plistlib.loads((app_path / "Contents/Info.plist").read_bytes())
    except (OSError, plistlib.InvalidFileException) as error:
        raise RuntimeError("mounted App has no valid Info.plist") from error


def verify_mounted_dmg(
    mountpoint: Path,
    commit: str,
    *,
    command_runner: CommandRunner | None = None,
) -> None:
    mountpoint = Path(mountpoint)
    runner = command_runner or subprocess.run
    scan_distribution_tree(
        mountpoint,
        allow_applications_link=True,
        app_bundle_root=mountpoint / APP_NAME,
    )
    expected_layout = {APP_NAME, "Applications", GUIDE_NAME}
    try:
        actual_layout = {path.name for path in mountpoint.iterdir()}
    except OSError as error:
        raise RuntimeError("unable to inspect mounted DMG") from error
    if actual_layout != expected_layout:
        raise RuntimeError("mounted DMG layout is not exact")

    applications = mountpoint / "Applications"
    if not applications.is_symlink() or os.readlink(applications) != "/Applications":
        raise RuntimeError("mounted DMG Applications link is invalid")
    guide = mountpoint / GUIDE_NAME
    if not guide.is_file() or guide.is_symlink():
        raise RuntimeError(f"mounted DMG is missing {GUIDE_NAME}")
    try:
        guide_text = guide.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise RuntimeError("mounted DMG opening guide is invalid") from error
    if not all(
        phrase in guide_text for phrase in ("未经 Apple 公证", "右键", "打开")
    ):
        raise RuntimeError("mounted DMG opening guide is incomplete")

    app_path = mountpoint / APP_NAME
    info = _load_info(app_path)
    expected_info = {
        "CFBundleIdentifier": BUNDLE_IDENTIFIER,
        "CFBundleExecutable": EXECUTABLE_NAME,
        "ChenSourceCommit": commit,
        "LSMinimumSystemVersion": MINIMUM_MACOS,
    }
    for key, expected in expected_info.items():
        if info.get(key) != expected:
            label = "commit" if key == "ChenSourceCommit" else key
            raise RuntimeError(f"mounted App {label} is invalid")

    executable = app_path / "Contents/MacOS" / EXECUTABLE_NAME
    if not executable.is_file() or executable.is_symlink():
        raise RuntimeError("mounted App executable is missing")
    architecture_result = _run(
        ["/usr/bin/lipo", "-archs", executable], runner
    )
    if set(architecture_result.stdout.split()) != set(ARCHITECTURES):
        raise RuntimeError("mounted App is not exact universal2")

    resources = app_path / "Contents/Resources"
    for relative in REQUIRED_RESOURCES:
        if not (resources / relative).exists():
            raise RuntimeError(f"mounted App resource is missing: {relative.as_posix()}")
    _run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", app_path],
        runner,
    )
    signature_result = _run(
        ["/usr/bin/codesign", "-dv", "--verbose=4", app_path], runner
    )
    signature_details = f"{signature_result.stdout}\n{signature_result.stderr}"
    if "Signature=adhoc" not in signature_details:
        raise RuntimeError("mounted App does not have an ad-hoc signature")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_path(output: Path, version: str) -> Path:
    expected_suffix = f"-{version}-universal.dmg"
    if output.name.endswith(expected_suffix):
        prefix = output.name[: -len(expected_suffix)]
        return output.with_name(f"{prefix}-{version}-build-manifest.json")
    return output.with_name(f"{output.stem}-build-manifest.json")


def _validate_release_inputs(output: Path, version: str, commit: str) -> None:
    if output.suffix != ".dmg" or output.name != Path(output.name).name:
        raise RuntimeError("output must be a .dmg file")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise RuntimeError("version must use numeric semantic versioning")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("commit must be a full lowercase git SHA")


def _publish_artifacts(pairs: Sequence[tuple[Path, Path]], work: Path) -> None:
    backup_root = Path(
        tempfile.mkdtemp(prefix=".dmg-rollback-", dir=work.parent)
    )
    backups: list[tuple[Path, Path]] = []
    published: list[Path] = []
    active_target: Path | None = None
    try:
        for _, target in pairs:
            active_target = target
            if target.exists() or target.is_symlink():
                backup = backup_root / f"previous-{len(backups)}"
                os.replace(target, backup)
                backups.append((backup, target))
        for temporary, target in pairs:
            active_target = target
            os.replace(temporary, target)
            published.append(target)
    except Exception as publish_error:
        rollback_failures: list[tuple[str, Path]] = []
        for target in reversed(published):
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                rollback_failures.append(("remove", target))
        for backup, target in reversed(backups):
            if backup.exists() or backup.is_symlink():
                try:
                    os.replace(backup, target)
                except OSError:
                    rollback_failures.append(("restore", target))
        remaining_backups = [
            (backup, target)
            for backup, target in backups
            if backup.exists() or backup.is_symlink()
        ]
        if rollback_failures:
            failures = ", ".join(
                f"{action} {target.name}" for action, target in rollback_failures
            )
            backup_state = ", ".join(
                f"{backup.name}->{target.name}"
                for backup, target in remaining_backups
            ) or "none"
            failed_name = active_target.name if active_target is not None else "unknown"
            raise RuntimeError(
                f"artifact publish failed at {failed_name}; rollback incomplete: "
                f"{failures}; backup state {backup_root.name}: {backup_state}"
            ) from publish_error
        backup_root.rmdir()
        raise
    else:
        for backup, _ in backups:
            backup.unlink()
        backup_root.rmdir()


def create_dmg(
    app_path: Path,
    output: Path,
    version: str,
    commit: str,
    *,
    guide_path: Path | None = None,
    command_runner: CommandRunner | None = None,
) -> Dict[str, Any]:
    app_path = Path(app_path)
    output = Path(output).expanduser().resolve()
    guide_path = Path(guide_path) if guide_path else Path(__file__).resolve().parents[1] / "packaging" / GUIDE_NAME
    runner = command_runner or subprocess.run
    _validate_release_inputs(output, version, commit)
    output.parent.mkdir(parents=True, exist_ok=True)

    scan_distribution_tree(app_path, app_bundle_root=app_path)
    scan_distribution_tree(guide_path)
    checksum_path = output.with_name(output.name + ".sha256")
    manifest_path = _manifest_path(output, version)

    with tempfile.TemporaryDirectory(prefix=".dmg-build-", dir=output.parent) as work_name:
        work = Path(work_name)
        staging = prepare_staging(app_path, guide_path, work / "staging")
        scan_distribution_tree(
            staging,
            allow_applications_link=True,
            app_bundle_root=staging / APP_NAME,
        )
        temporary_dmg = work / output.name
        mountpoint = work / "mount"
        mountpoint.mkdir()
        attached = False
        try:
            _run(
                [
                    "/usr/bin/hdiutil",
                    "create",
                    "-fs",
                    "HFS+",
                    "-volname",
                    VOLUME_NAME,
                    "-srcfolder",
                    staging,
                    "-format",
                    "UDZO",
                    temporary_dmg,
                ],
                runner,
            )
            _run(["/usr/bin/hdiutil", "verify", temporary_dmg], runner)
            attach_command = [
                "/usr/bin/hdiutil",
                "attach",
                "-readonly",
                "-nobrowse",
                "-mountpoint",
                mountpoint,
                temporary_dmg,
            ]
            try:
                _run(attach_command, runner)
            except RuntimeError:
                try:
                    _run(["/usr/bin/hdiutil", "detach", mountpoint], runner)
                except RuntimeError:
                    pass
                raise
            attached = True
            verify_mounted_dmg(mountpoint, commit, command_runner=runner)
        finally:
            if attached:
                _run(["/usr/bin/hdiutil", "detach", mountpoint], runner)

        digest = _sha256(temporary_dmg)
        manifest: Dict[str, Any] = {
            "version": version,
            "commit": commit,
            "bundle_identifier": BUNDLE_IDENTIFIER,
            "architectures": list(ARCHITECTURES),
            "minimum_macos": MINIMUM_MACOS,
            "signature": "adhoc",
            "notarized": False,
            "dmg_sha256": digest,
        }
        temporary_checksum = work / checksum_path.name
        temporary_manifest = work / manifest_path.name
        temporary_checksum.write_text(
            f"{digest}  {output.name}\n", encoding="utf-8"
        )
        temporary_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        scan_distribution_tree(temporary_checksum)
        scan_distribution_tree(temporary_manifest)
        _publish_artifacts(
            (
                (temporary_dmg, output),
                (temporary_checksum, checksum_path),
                (temporary_manifest, manifest_path),
            ),
            work,
        )
    return manifest


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从干净 master 构建并验证正式 DMG")
    parser.add_argument("--source", required=True)
    parser.add_argument("--runtime-cache", required=True)
    parser.add_argument("--output", required=True)
    return parser


def main() -> None:
    args = build_argument_parser().parse_args()
    manifest = release_dmg(
        Path(args.source),
        Path(args.runtime_cache),
        Path(args.output),
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
