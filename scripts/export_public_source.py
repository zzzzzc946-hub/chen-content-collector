#!/usr/bin/env python3
"""Create a history-free, public-source archive from a Git commit."""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any


PRIVATE_PREFIXES = (
    ".codex/",
    ".superpowers/",
    "docs/project/",
    "docs/superpowers/",
    "graphify-out/",
)
PRIVATE_PATHS = frozenset({"AGENTS.md"})
FORBIDDEN_NAMES = frozenset(
    {
        ".env",
        ".dev.vars",
        ".feishu_token_cache.json",
        "config.json",
        "desktop_collector.sqlite3",
    }
)
FORBIDDEN_SUFFIXES = (
    ".db",
    ".dmg",
    ".key",
    ".pem",
    ".p12",
    ".sqlite",
    ".sqlite3",
)
PERSONAL_PATH_MARKERS = (str(Path.home()).encode() + b"/",)
CANONICAL_SOURCE_ROOT_PATTERN = re.compile(
    rb'CANONICAL_SOURCE_ROOT = Path\("[^"]+"\)'
)
NAS_MEDIA_ROOT_PATTERN = re.compile(rb'("nas_media_root"\s*:\s*)"[^"]*"')
MANIFEST_NAME = "PUBLIC-SOURCE-MANIFEST.json"


def _git_output(source_root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(source_root), *args],
        check=True,
        capture_output=True,
    ).stdout


def _exclusion_reason(path: PurePosixPath, content: bytes) -> str | None:
    normalized = path.as_posix()
    if normalized in PRIVATE_PATHS or normalized.startswith(PRIVATE_PREFIXES):
        return "private_documentation"
    name = path.name.casefold()
    if name in FORBIDDEN_NAMES or name.endswith(FORBIDDEN_SUFFIXES):
        return "runtime_or_secret_data"
    if any(marker in content for marker in PERSONAL_PATH_MARKERS):
        return "personal_absolute_path"
    return None


def _public_content(path: PurePosixPath, content: bytes) -> bytes:
    if path.as_posix() == "scripts/build_dmg.py":
        content = CANONICAL_SOURCE_ROOT_PATTERN.sub(
            b"CANONICAL_SOURCE_ROOT = Path(__file__).resolve().parents[1]", content
        )
    if path.as_posix() == "max_daily_cloud/publisher/config.example.json":
        content = NAS_MEDIA_ROOT_PATTERN.sub(
            rb'\1"/path/to/daily-videos"', content
        )
    return content


def _validated_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise RuntimeError("commit archive contains an unsafe path")
    return path


def export_public_source(
    source_root: Path,
    output_path: Path,
    *,
    ref: str = "HEAD",
) -> dict[str, Any]:
    """Write a filtered tar.gz archive and return its public manifest."""
    source_root = Path(source_root).resolve()
    output_path = Path(output_path).resolve()
    commit = _git_output(source_root, "rev-parse", ref).decode().strip()
    archive_bytes = _git_output(source_root, "archive", "--format=tar", commit)
    included: list[tuple[PurePosixPath, bytes]] = []
    excluded: list[dict[str, str]] = []

    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
        for member in archive.getmembers():
            path = _validated_path(member.name)
            if not member.isfile():
                if not member.isdir():
                    excluded.append({"path": path.as_posix(), "reason": "non_regular_file"})
                continue
            stream = archive.extractfile(member)
            if stream is None:
                raise RuntimeError("commit archive member cannot be read")
            content = _public_content(path, stream.read())
            reason = _exclusion_reason(path, content)
            if reason:
                excluded.append({"path": path.as_posix(), "reason": reason})
                continue
            included.append((path, content))

    manifest = {
        "format": 1,
        "commit": commit,
        "included_count": len(included),
        "excluded_count": len(excluded),
        "excluded": sorted(excluded, key=lambda item: item["path"]),
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output_path, "w:gz", format=tarfile.PAX_FORMAT) as output:
        for path, content in sorted(included, key=lambda item: item[0].as_posix()):
            info = tarfile.TarInfo(path.as_posix())
            info.size = len(content)
            info.mode = 0o644
            info.mtime = 0
            output.addfile(info, io.BytesIO(content))
        manifest_info = tarfile.TarInfo(MANIFEST_NAME)
        manifest_info.size = len(manifest_bytes)
        manifest_info.mode = 0o644
        manifest_info.mtime = 0
        output.addfile(manifest_info, io.BytesIO(manifest_bytes))
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ref", default="HEAD")
    args = parser.parse_args()
    manifest = export_public_source(args.source, args.output, ref=args.ref)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
