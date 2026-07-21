import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from publisher.media_proxy import (
    AUDIO_BITRATE_BPS,
    FALLBACK_PAYLOAD_BYTES,
    MAX_UPLOAD_BYTES,
    MediaProxyError,
    POLICY_VERSION,
    prepare_media_for_upload,
)


def oversized_file(path: Path) -> Path:
    path.write_bytes(b"source")
    with path.open("r+b") as handle:
        handle.truncate(MAX_UPLOAD_BYTES + 1)
    return path


class FakeRunner:
    def __init__(self, output_sizes=None, fail=False):
        self.commands = []
        self.output_sizes = list(output_sizes or [1024 * 1024])
        self.fail = fail

    def __call__(self, command, **kwargs):
        self.commands.append((list(command), kwargs))
        if self.fail:
            return subprocess.CompletedProcess(command, 1, stderr="ffmpeg failed")

        output = Path(command[-1])
        if str(output) != "/dev/null":
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"x" * self.output_sizes.pop(0))
        return subprocess.CompletedProcess(command, 0, stderr="")


class MediaProxyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"source")
        self.cache = self.root / "cache"
        self.ffmpeg = self.root / "ffmpeg"
        self.ffmpeg.write_bytes(b"fake ffmpeg")

    def tearDown(self):
        self.tmp.cleanup()

    def test_small_file_bypasses_ffmpeg_and_keeps_source_path(self):
        runner = FakeRunner()

        result = prepare_media_for_upload(
            self.source, "00:10", self.cache, ffmpeg_path=self.ffmpeg, command_runner=runner
        )

        self.assertEqual(result, self.source)
        self.assertEqual(runner.commands, [])

    def test_audio_only_proxy_is_used_when_it_fits(self):
        oversized_file(self.source)
        runner = FakeRunner([1024 * 1024])

        result = prepare_media_for_upload(
            self.source, "00:10", self.cache, ffmpeg_path=self.ffmpeg, command_runner=runner
        )

        self.assertTrue(result.is_file())
        self.assertEqual(result.parent, self.cache)
        self.assertEqual(
            result.name,
            f"{hashlib.sha256(self.source.read_bytes()).hexdigest()}-{POLICY_VERSION}.mp4",
        )
        self.assertEqual(len(runner.commands), 1)
        command = runner.commands[0][0]
        for fragment in (
            "-map",
            "0:v:0",
            "0:a:0?",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            "-movflags",
            "+faststart",
        ):
            self.assertIn(fragment, command)

    def test_oversized_audio_proxy_falls_back_to_two_pass_h264(self):
        oversized_file(self.source)
        runner = FakeRunner([MAX_UPLOAD_BYTES + 1, 1024 * 1024])

        result = prepare_media_for_upload(
            self.source, "01:00", self.cache, ffmpeg_path=self.ffmpeg, command_runner=runner
        )

        self.assertTrue(result.is_file())
        self.assertEqual(len(runner.commands), 3)
        first_pass, second_pass = runner.commands[1][0], runner.commands[2][0]
        expected_video_bitrate = int(FALLBACK_PAYLOAD_BYTES * 8 / 60) - AUDIO_BITRATE_BPS
        self.assertIn("-pass", first_pass)
        self.assertEqual(first_pass[first_pass.index("-pass") + 1], "1")
        self.assertEqual(second_pass[second_pass.index("-pass") + 1], "2")
        first_log = first_pass[first_pass.index("-passlogfile") + 1]
        second_log = second_pass[second_pass.index("-passlogfile") + 1]
        self.assertEqual(first_log, second_log)
        self.assertIn("libx264", first_pass)
        self.assertIn("libx264", second_pass)
        self.assertEqual(first_pass[first_pass.index("-preset") + 1], "veryfast")
        self.assertEqual(second_pass[second_pass.index("-preset") + 1], "veryfast")
        self.assertEqual(first_pass[first_pass.index("-b:v") + 1], str(expected_video_bitrate))
        self.assertEqual(second_pass[second_pass.index("-b:v") + 1], str(expected_video_bitrate))
        self.assertIn("-an", first_pass)
        self.assertEqual(first_pass[first_pass.index("-f") + 1], "mp4")
        self.assertEqual(first_pass[-1], "/dev/null")
        self.assertEqual(second_pass[second_pass.index("-map") + 3], "0:a:0?")

    def test_valid_cached_proxy_skips_ffmpeg_on_repeat(self):
        oversized_file(self.source)
        first_runner = FakeRunner([1024 * 1024])
        first = prepare_media_for_upload(
            self.source, "00:10", self.cache, ffmpeg_path=self.ffmpeg, command_runner=first_runner
        )

        def fail_runner(*args, **kwargs):
            raise AssertionError("cached proxy should skip ffmpeg")

        second = prepare_media_for_upload(
            self.source, "00:10", self.cache, ffmpeg_path=self.ffmpeg, command_runner=fail_runner
        )

        self.assertEqual(second, first)

    def test_valid_cached_proxy_bypasses_invalid_duration(self):
        oversized_file(self.source)
        digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.cache.mkdir()
        cached_proxy = self.cache / f"{digest}-{POLICY_VERSION}.mp4"
        cached_proxy.write_bytes(b"x" * 1024)

        result = prepare_media_for_upload(
            self.source,
            "01:60",
            self.cache,
            ffmpeg_path=self.ffmpeg,
            command_runner=lambda *args, **kwargs: self.fail("cache should skip ffmpeg"),
        )

        self.assertEqual(result, cached_proxy)

    def test_audio_only_success_bypasses_invalid_duration(self):
        oversized_file(self.source)
        runner = FakeRunner([1024 * 1024])

        result = prepare_media_for_upload(
            self.source, "01:60", self.cache, ffmpeg_path=self.ffmpeg, command_runner=runner
        )

        self.assertTrue(result.is_file())
        self.assertEqual(len(runner.commands), 1)

    def test_invalid_duration_fails_after_oversized_audio_proxy(self):
        oversized_file(self.source)
        runner = FakeRunner([MAX_UPLOAD_BYTES + 1, 1024 * 1024])

        with self.assertRaises(MediaProxyError):
            prepare_media_for_upload(
                self.source, "01:60", self.cache, ffmpeg_path=self.ffmpeg, command_runner=runner
            )

        self.assertEqual(len(runner.commands), 1)

    def test_empty_or_oversized_cached_proxy_is_replaced(self):
        oversized_file(self.source)
        digest = hashlib.sha256(self.source.read_bytes()).hexdigest()

        for name, size in (("empty", 0), ("oversized", MAX_UPLOAD_BYTES + 1)):
            with self.subTest(name=name):
                cache = self.root / f"cache-{name}"
                cache.mkdir()
                cached_proxy = cache / f"{digest}-{POLICY_VERSION}.mp4"
                cached_proxy.write_bytes(b"x" * size)
                runner = FakeRunner([1024 * 1024])

                result = prepare_media_for_upload(
                    self.source, "01:60", cache, ffmpeg_path=self.ffmpeg, command_runner=runner
                )

                self.assertEqual(result, cached_proxy)
                self.assertEqual(result.stat().st_size, 1024 * 1024)
                self.assertEqual(len(runner.commands), 1)

    def test_filesystem_oserrors_are_media_proxy_errors_with_causes(self):
        oversized_file(self.source)
        cases = (
            ("cache mkdir", "pathlib.Path.mkdir", PermissionError("mkdir denied")),
            ("source hash", "publisher.media_proxy.source_sha256", PermissionError("read denied")),
            ("temporary directory", "tempfile.TemporaryDirectory", PermissionError("temp denied")),
        )

        for name, target, error in cases:
            with self.subTest(name=name):
                with mock.patch(target, side_effect=error):
                    with self.assertRaises(MediaProxyError) as raised:
                        prepare_media_for_upload(
                            self.source,
                            "00:10",
                            self.cache,
                            ffmpeg_path=self.ffmpeg,
                            command_runner=FakeRunner(),
                        )
                self.assertIs(raised.exception.__cause__, error)

    def test_final_proxy_replace_oserror_is_media_proxy_error_with_cause(self):
        oversized_file(self.source)
        runner = FakeRunner([MAX_UPLOAD_BYTES + 1, 1024 * 1024])
        original_replace = Path.replace
        error = PermissionError("replace denied")

        def replace(path, target):
            if path.name == "h264-proxy.mp4":
                raise error
            return original_replace(path, target)

        with mock.patch("pathlib.Path.replace", new=replace):
            with self.assertRaises(MediaProxyError) as raised:
                prepare_media_for_upload(
                    self.source,
                    "00:10",
                    self.cache,
                    ffmpeg_path=self.ffmpeg,
                    command_runner=runner,
                )

        self.assertIs(raised.exception.__cause__, error)

    def test_missing_ffmpeg_and_oversized_output_fail_explicitly(self):
        oversized_file(self.source)
        with self.assertRaises(MediaProxyError) as missing:
            prepare_media_for_upload(
                self.source,
                "00:10",
                self.cache,
                ffmpeg_path=self.root / "missing-ffmpeg",
                command_runner=FakeRunner(),
            )
        self.assertIn("ffmpeg", str(missing.exception).lower())

        runner = FakeRunner([MAX_UPLOAD_BYTES + 1, MAX_UPLOAD_BYTES + 1])
        with self.assertRaises(MediaProxyError) as too_large:
            prepare_media_for_upload(
                self.source, "00:10", self.cache, ffmpeg_path=self.ffmpeg, command_runner=runner
            )
        self.assertIn("size", str(too_large.exception).lower())

    def test_source_bytes_and_size_are_never_modified(self):
        oversized_file(self.source)
        before_size = self.source.stat().st_size
        before_digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        runner = FakeRunner([1024 * 1024])

        prepare_media_for_upload(
            self.source, "00:10", self.cache, ffmpeg_path=self.ffmpeg, command_runner=runner
        )

        self.assertEqual(self.source.stat().st_size, before_size)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before_digest)


if __name__ == "__main__":
    unittest.main()
