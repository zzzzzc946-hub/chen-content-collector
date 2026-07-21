import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import publisher.local_publish_jobs as local_publish_jobs
from publisher.local_publish_jobs import (
    InvalidPublishDate,
    PublishAlreadyRunning,
    PublishJobManager,
)
from publisher.max_daily_publisher import PublisherError


PUBLIC_SNAPSHOT_FIELDS = {
    "state",
    "stage",
    "message",
    "dailyDate",
    "reportId",
    "reportUrl",
    "shareUrl",
    "startedAt",
    "finishedAt",
}
PUBLIC_LATEST_FIELDS = {
    "dailyDate",
    "reportId",
    "reportUrl",
    "shareUrl",
    "finishedAt",
}


class PublishJobManagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.json"
        self.latest_path = self.root / "latest-report.json"

    def test_success_tracks_progress_and_persists_only_public_result(self):
        release = threading.Event()

        def runner(config_path, daily_date, on_progress):
            on_progress("preparing", {"dailyDate": daily_date})
            on_progress("uploading", {"current": 1, "total": 1, "title": "素材"})
            release.wait(1)
            on_progress("publishing", {"reportId": "report-1"})
            return {
                "id": "report-1",
                "reportUrl": "https://worker.example.com/r/report-1",
                "private": "publisher-secret-token",
            }

        manager = PublishJobManager(
            self.config_path,
            self.latest_path,
            publish_runner=runner,
            share_url_reader=lambda: "https://worker.example.com/c/fixed-secret",
        )
        started = manager.start("2026-07-13")
        self.assertEqual(started["state"], "running")
        self.assertEqual(set(started), PUBLIC_SNAPSHOT_FIELDS)
        with self.assertRaises(PublishAlreadyRunning):
            manager.start("2026-07-13")
        release.set()
        manager.wait_for_test(timeout=2)

        snapshot = manager.snapshot()
        self.assertEqual(snapshot["state"], "succeeded")
        self.assertEqual(snapshot["reportId"], "report-1")
        self.assertEqual(set(snapshot), PUBLIC_SNAPSHOT_FIELDS)
        latest = manager.latest()
        self.assertEqual(latest["reportUrl"], "https://worker.example.com/r/report-1")
        self.assertEqual(latest["shareUrl"], "https://worker.example.com/c/fixed-secret")
        self.assertEqual(set(latest), PUBLIC_LATEST_FIELDS)
        self.assertNotIn("publisher-secret-token", self.latest_path.read_text("utf-8"))
        self.assertNotIn("fixed-secret", self.latest_path.read_text("utf-8"))

    def test_fixed_collaboration_url_comes_from_matching_keychain_entry(self):
        self.config_path.write_text(json.dumps({
            "cloud_api_base": "https://worker.example.com",
            "fixed_link_keychain_service": "Fixed Link Service",
            "fixed_link_keychain_account": "owner@example.com",
        }), "utf-8")

        with patch.object(
            local_publish_jobs,
            "read_keychain_password",
            return_value="https://worker.example.com/c/fixed-secret",
        ) as read_password:
            share_url = local_publish_jobs.read_fixed_collaboration_url(self.config_path)

        self.assertEqual(share_url, "https://worker.example.com/c/fixed-secret")
        read_password.assert_called_once_with("Fixed Link Service", "owner@example.com")

    def test_fixed_collaboration_url_rejects_report_and_foreign_urls(self):
        self.config_path.write_text(json.dumps({
            "cloud_api_base": "https://worker.example.com",
            "fixed_link_keychain_service": "Fixed Link Service",
            "fixed_link_keychain_account": "owner@example.com",
        }), "utf-8")

        for unsafe_url in (
            "https://worker.example.com/r/report-1",
            "https://attacker.example.com/c/fixed-secret",
            "http://worker.example.com/c/fixed-secret",
        ):
            with self.subTest(unsafe_url=unsafe_url), patch.object(
                local_publish_jobs,
                "read_keychain_password",
                return_value=unsafe_url,
            ):
                self.assertEqual(
                    local_publish_jobs.read_fixed_collaboration_url(self.config_path),
                    "",
                )

    def test_failure_is_retryable_and_keeps_previous_latest_link(self):
        self.latest_path.write_text(json.dumps({
            "dailyDate": "2026-07-12",
            "reportId": "old-report",
            "reportUrl": "https://worker.example.com/r/old-report",
            "finishedAt": "2026-07-12T10:00:00+00:00",
        }), "utf-8")

        def failing_runner(config_path, daily_date, on_progress):
            raise PublisherError("媒体文件不存在或不可读：missing.mp4")

        manager = PublishJobManager(
            self.config_path,
            self.latest_path,
            publish_runner=failing_runner,
        )
        manager.start("2026-07-13")
        manager.wait_for_test(timeout=2)

        self.assertEqual(manager.snapshot()["state"], "failed")
        self.assertIn("媒体文件不存在或不可读", manager.snapshot()["message"])
        self.assertEqual(manager.latest()["reportId"], "old-report")

        replacement = PublishJobManager(
            self.config_path,
            self.latest_path,
            publish_runner=lambda config_path, daily_date, on_progress: {
                "id": "replacement-report",
                "reportUrl": "https://worker.example.com/r/replacement-report",
            },
        )
        replacement.start("2026-07-13")
        replacement.wait_for_test(timeout=2)

        self.assertEqual(replacement.snapshot()["state"], "succeeded")
        self.assertEqual(replacement.snapshot()["reportId"], "replacement-report")

    def test_publisher_config_failure_keeps_actionable_message(self):
        def failing_runner(config_path, daily_date, on_progress):
            raise PublisherError("config.json 缺少 cloud_api_base")

        manager = PublishJobManager(
            self.config_path,
            self.latest_path,
            publish_runner=failing_runner,
        )
        manager.start("2026-07-13")
        manager.wait_for_test(timeout=2)

        self.assertEqual(
            manager.snapshot()["message"],
            "config.json 缺少 cloud_api_base",
        )

    def test_known_publisher_errors_keep_only_safe_public_categories(self):
        sensitive_details = (
            " Authorization: Bearer publisher-secret-token "
            "signedUrl=https://storage.example.com/upload?signature=secret "
            "source=/private/clip.mp4"
        )
        cases = (
            (
                "缺少发布器设备 Token：请设置 PUBLISHER_DEVICE_TOKEN 或写入 macOS Keychain",
                "缺少发布器设备 Token：请设置 PUBLISHER_DEVICE_TOKEN 或写入 macOS Keychain",
            ),
            ("config.json 缺少 cloud_api_base", "config.json 缺少 cloud_api_base"),
            ("这一天没有可发布的日报素材", "这一天没有可发布的日报素材"),
            (
                "日报素材缺少 table_id，无法确定 source_table_id",
                "日报素材缺少 table_id，无法确定 source_table_id",
            ),
            ("日报素材来自多个表，不能合并发布", "日报素材来自多个表，不能合并发布"),
            ("日报素材缺少 id", "日报素材缺少 id"),
            (
                "日报素材缺少 video_path：record-secret-1" + sensitive_details,
                "日报素材缺少 video_path",
            ),
            (
                "媒体文件不存在或不可读：/private/secret-video.mp4" + sensitive_details,
                "媒体文件不存在或不可读",
            ),
            ("云端草稿缺少 report id", "云端草稿缺少 report id"),
            (
                "云端草稿缺少媒体占位：record-secret-2" + sensitive_details,
                "云端草稿缺少媒体占位",
            ),
            (
                "云端草稿缺少 report item：record-secret-3" + sensitive_details,
                "云端草稿缺少 report item",
            ),
            ("发布结果缺少 report id", "发布结果缺少 report id"),
            ("发布结果缺少日报链接", "发布结果缺少日报链接"),
            (
                "视频超过云端免费额度，生成播放副本失败：" + sensitive_details,
                "视频超过云端免费额度，生成播放副本失败",
            ),
            ("读取本地日报失败：503 upstream unavailable", "发布失败，请查看本地日志后重试"),
            ("上传分片失败：500 upload unavailable", "发布失败，请查看本地日志后重试"),
            ("云端请求失败：500 internal error", "发布失败，请查看本地日志后重试"),
            ("unexpected internal publisher state", "发布失败，请查看本地日志后重试"),
        )

        for error_message, expected_message in cases:
            with self.subTest(error_message=error_message):
                manager = PublishJobManager(
                    self.config_path,
                    self.latest_path,
                    publish_runner=lambda config_path, daily_date, on_progress,
                    message=error_message: (_ for _ in ()).throw(PublisherError(message)),
                )
                manager.start("2026-07-13")
                manager.wait_for_test(timeout=2)

                snapshot = manager.snapshot()
                self.assertEqual(snapshot["state"], "failed")
                self.assertEqual(snapshot["message"], expected_message)
                self.assertNotIn("record-secret", str(snapshot))
                self.assertNotIn("/private/", str(snapshot))
                self.assertNotIn("publisher-secret-token", str(snapshot))
                self.assertNotIn("signature=secret", str(snapshot))

    def test_publisher_error_with_secret_bearing_details_uses_fixed_message(self):
        secret = "publisher-secret-token"

        def failing_runner(config_path, daily_date, on_progress):
            raise PublisherError(
                "上传失败 Authorization: Bearer %s X-Publisher-Token: %s "
                "config={'publisher_token': '%s', 'cloud_api_base': 'https://private.example'}"
                % (secret, secret, secret)
            )

        manager = PublishJobManager(
            self.config_path,
            self.latest_path,
            publish_runner=failing_runner,
        )
        manager.start("2026-07-13")
        manager.wait_for_test(timeout=2)

        snapshot = manager.snapshot()
        self.assertEqual(snapshot["message"], "发布失败，请查看本地日志后重试")
        self.assertNotIn(secret, str(snapshot))
        self.assertNotIn("Authorization", str(snapshot))
        self.assertNotIn("Bearer", str(snapshot))
        self.assertNotIn("X-Publisher-Token", str(snapshot))
        self.assertNotIn("cloud_api_base", str(snapshot))

    def test_generic_failure_snapshot_redacts_secret_bearing_details(self):
        secret = "publisher-secret-token"

        def failing_runner(config_path, daily_date, on_progress):
            raise RuntimeError(
                "Authorization: Bearer %s config=%s" % (secret, config_path)
            )

        manager = PublishJobManager(
            self.config_path,
            self.latest_path,
            publish_runner=failing_runner,
        )
        manager.start("2026-07-13")
        manager.wait_for_test(timeout=2)

        snapshot = manager.snapshot()
        self.assertEqual(snapshot["message"], "发布失败，请查看本地日志后重试")
        self.assertNotIn(secret, str(snapshot))
        self.assertNotIn("Authorization", str(snapshot))
        self.assertNotIn(str(self.config_path), str(snapshot))

    def test_failure_logs_proxy_details_locally_but_redacts_secret_errors(self):
        proxy_detail = "unable to hash source file: /Users/test/video.mp4"
        secret = "publisher-secret-token"
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            for error in (
                PublisherError(
                    "视频超过云端免费额度，生成播放副本失败：" + proxy_detail
                ),
                PublisherError(
                    "云端请求失败 Authorization: Bearer %s" % secret
                ),
            ):
                manager = PublishJobManager(
                    self.config_path,
                    self.latest_path,
                    publish_runner=lambda config_path, daily_date, on_progress,
                    failure=error: (_ for _ in ()).throw(failure),
                )
                manager.start("2026-07-13")
                manager.wait_for_test(timeout=2)

        diagnostic = stderr.getvalue()
        self.assertIn(proxy_detail, diagnostic)
        self.assertIn("发布失败，请查看本地日志后重试", diagnostic)
        self.assertNotIn(secret, diagnostic)
        self.assertNotIn("Authorization", diagnostic)
        self.assertNotIn("Bearer", diagnostic)

    def test_two_managers_reject_contention_and_allow_a_new_job_after_completion(self):
        release = threading.Event()

        def blocking_runner(config_path, daily_date, on_progress):
            release.wait(1)
            return {
                "id": "report-1",
                "reportUrl": "https://worker.example.com/r/report-1",
            }

        def succeeding_runner(config_path, daily_date, on_progress):
            return {
                "id": "report-2",
                "reportUrl": "https://worker.example.com/r/report-2",
            }

        first = PublishJobManager(
            self.config_path,
            self.latest_path,
            publish_runner=blocking_runner,
        )
        second = PublishJobManager(
            self.config_path,
            self.latest_path,
            publish_runner=succeeding_runner,
        )

        first.start("2026-07-13")
        with self.assertRaises(PublishAlreadyRunning):
            second.start("2026-07-13")

        release.set()
        first.wait_for_test(timeout=2)
        started = second.start("2026-07-13")
        self.assertEqual(started["state"], "running")
        second.wait_for_test(timeout=2)
        self.assertEqual(second.snapshot()["reportId"], "report-2")

    def test_thread_start_failure_releases_the_process_wide_slot(self):
        manager = PublishJobManager(self.config_path, self.latest_path)

        with patch(
            "publisher.local_publish_jobs.threading.Thread.start",
            side_effect=RuntimeError("Authorization: Bearer publisher-secret-token"),
        ):
            with self.assertRaisesRegex(RuntimeError, "无法启动发布任务"):
                manager.start("2026-07-13")

        self.assertEqual(manager.snapshot()["state"], "failed")
        self.assertNotIn("publisher-secret-token", str(manager.snapshot()))

        replacement = PublishJobManager(
            self.config_path,
            self.latest_path,
            publish_runner=lambda config_path, daily_date, on_progress: {
                "id": "report-3",
                "reportUrl": "https://worker.example.com/r/report-3",
            },
        )
        replacement.start("2026-07-13")
        replacement.wait_for_test(timeout=2)
        self.assertEqual(replacement.snapshot()["state"], "succeeded")

    def test_latest_returns_empty_for_malformed_json_without_overwriting_it(self):
        malformed = "{not valid json"
        self.latest_path.write_text(malformed, "utf-8")

        self.assertEqual(
            PublishJobManager(self.config_path, self.latest_path).latest(),
            {},
        )
        self.assertEqual(self.latest_path.read_text("utf-8"), malformed)

    def test_latest_returns_empty_for_scalar_json_without_overwriting_it(self):
        scalar = '"not an object"'
        self.latest_path.write_text(scalar, "utf-8")

        self.assertEqual(
            PublishJobManager(self.config_path, self.latest_path).latest(),
            {},
        )
        self.assertEqual(self.latest_path.read_text("utf-8"), scalar)

    def test_latest_returns_empty_for_invalid_utf8_without_overwriting_bytes(self):
        original = b'{"reportId": "report-1", "invalid": "\xff"}'
        self.latest_path.write_bytes(original)

        self.assertEqual(
            PublishJobManager(self.config_path, self.latest_path).latest(),
            {},
        )
        self.assertEqual(self.latest_path.read_bytes(), original)

    def test_latest_returns_empty_for_read_errors_without_overwriting_file(self):
        original = '{"reportId": "report-1"}'
        self.latest_path.write_text(original, "utf-8")
        manager = PublishJobManager(self.config_path, self.latest_path)

        with patch.object(Path, "read_text", side_effect=OSError("disk unavailable")):
            self.assertEqual(manager.latest(), {})

        self.assertEqual(self.latest_path.read_text("utf-8"), original)

    def test_rejects_invalid_date(self):
        manager = PublishJobManager(self.config_path, self.latest_path)
        with self.assertRaises(InvalidPublishDate):
            manager.start("13/07/2026")


if __name__ == "__main__":
    unittest.main()
