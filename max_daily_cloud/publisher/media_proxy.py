import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional


MAX_UPLOAD_BYTES = 45 * 1024 * 1024
FALLBACK_PAYLOAD_BYTES = 42 * 1024 * 1024
AUDIO_BITRATE_BPS = 64_000
MIN_VIDEO_BITRATE_BPS = 80_000
POLICY_VERSION = "v1"


class MediaProxyError(RuntimeError):
    pass


def resolve_ffmpeg(explicit: Optional[Path] = None) -> Path:
    if explicit is not None:
        path = Path(explicit)
        if path.is_file():
            return path
        raise MediaProxyError(f"ffmpeg not found: {path}")

    found = shutil.which("ffmpeg")
    if found:
        return Path(found)

    fallback = Path.home() / ".local" / "bin" / "ffmpeg"
    if fallback.is_file():
        return fallback
    raise MediaProxyError("ffmpeg not found")


def parse_duration_seconds(value: str) -> float:
    parts = value.split(":")
    if len(parts) not in (2, 3) or any(not part.isdigit() for part in parts):
        raise MediaProxyError(f"invalid duration: {value}")

    numbers = [int(part) for part in parts]
    if numbers[-1] >= 60 or (len(numbers) == 3 and numbers[1] >= 60):
        raise MediaProxyError(f"invalid duration: {value}")

    seconds = numbers[-1] + numbers[-2] * 60
    if len(numbers) == 3:
        seconds += numbers[0] * 3600
    if seconds <= 0:
        raise MediaProxyError(f"invalid duration: {value}")
    return float(seconds)


def source_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_proxy(path: Path) -> bool:
    try:
        return path.is_file() and 0 < path.stat().st_size <= MAX_UPLOAD_BYTES
    except OSError:
        return False


def _ffmpeg_error(result: subprocess.CompletedProcess) -> str:
    details = (result.stderr or result.stdout or "").strip()
    return details[-500:]


def _run_ffmpeg(
    command: list[str],
    command_runner: Callable[..., subprocess.CompletedProcess],
) -> subprocess.CompletedProcess:
    try:
        result = command_runner(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise MediaProxyError(f"ffmpeg execution failed: {str(exc)[-500:]}") from exc
    if result.returncode != 0:
        raise MediaProxyError(f"ffmpeg failed: {_ffmpeg_error(result)}")
    return result


def _proxy_output_is_valid(path: Path) -> bool:
    return _valid_proxy(path)


def _replace_proxy(source: Path, target: Path) -> Path:
    try:
        return source.replace(target)
    except OSError as exc:
        raise MediaProxyError(f"unable to store media proxy: {target}") from exc


def prepare_media_for_upload(
    source_path: Path,
    duration_text: str,
    cache_root: Path,
    *,
    ffmpeg_path: Optional[Path] = None,
    command_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Path:
    source_path = Path(source_path)
    cache_root = Path(cache_root)
    try:
        source_size = source_path.stat().st_size
    except OSError as exc:
        raise MediaProxyError(f"source file unavailable: {source_path}") from exc

    if source_size <= MAX_UPLOAD_BYTES:
        return source_path

    try:
        cache_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MediaProxyError(f"unable to create media proxy cache: {cache_root}") from exc
    try:
        source_digest = source_sha256(source_path)
    except OSError as exc:
        raise MediaProxyError(f"unable to hash source file: {source_path}") from exc
    cached_proxy = cache_root / f"{source_digest}-{POLICY_VERSION}.mp4"
    if _valid_proxy(cached_proxy):
        return cached_proxy

    ffmpeg = resolve_ffmpeg(ffmpeg_path)
    try:
        with tempfile.TemporaryDirectory(prefix="media-proxy-", dir=cache_root) as temp_name:
            temp_root = Path(temp_name)
            audio_proxy = temp_root / "audio-proxy.mp4"
            audio_command = [
                str(ffmpeg),
                "-y",
                "-i",
                str(source_path),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "64k",
                "-movflags",
                "+faststart",
                str(audio_proxy),
            ]

            audio_ok = False
            try:
                _run_ffmpeg(audio_command, command_runner)
                audio_ok = _proxy_output_is_valid(audio_proxy)
            except MediaProxyError:
                audio_ok = False

            if audio_ok:
                _replace_proxy(audio_proxy, cached_proxy)
                return cached_proxy

            duration_seconds = parse_duration_seconds(duration_text)
            video_bitrate = int(FALLBACK_PAYLOAD_BYTES * 8 / duration_seconds) - AUDIO_BITRATE_BPS
            if video_bitrate < MIN_VIDEO_BITRATE_BPS:
                raise MediaProxyError("calculated video bitrate is below the minimum")

            passlogfile = temp_root / "h264-pass"
            first_pass = [
                str(ffmpeg),
                "-y",
                "-i",
                str(source_path),
                "-map",
                "0:v:0",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-b:v",
                str(video_bitrate),
                "-pass",
                "1",
                "-passlogfile",
                str(passlogfile),
                "-an",
                "-f",
                "mp4",
                "/dev/null",
            ]
            second_pass = [
                str(ffmpeg),
                "-y",
                "-i",
                str(source_path),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-b:v",
                str(video_bitrate),
                "-pass",
                "2",
                "-passlogfile",
                str(passlogfile),
                "-c:a",
                "aac",
                "-b:a",
                "64k",
                "-movflags",
                "+faststart",
                str(temp_root / "h264-proxy.mp4"),
            ]

            _run_ffmpeg(first_pass, command_runner)
            _run_ffmpeg(second_pass, command_runner)
            final_proxy = Path(second_pass[-1])
            if not _proxy_output_is_valid(final_proxy):
                raise MediaProxyError("ffmpeg proxy output is empty or exceeds upload size")
            _replace_proxy(final_proxy, cached_proxy)
            return cached_proxy
    except OSError as exc:
        raise MediaProxyError("media proxy temporary storage failed") from exc
