import json
import os
import tempfile
import unittest
from hashlib import sha256
from unittest.mock import Mock, patch
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler

from publisher.max_daily_publisher import (
    CloudApi,
    DailyPublisher,
    JsonStateStore,
    LocalDailyApi,
    PublisherConfig,
    PublisherError,
    default_publisher_config_path,
    main,
    publish_from_config,
)
from publisher.media_proxy import MediaProxyError


class FakeLocalApi:
    def __init__(self, media_path):
        self.media_path = media_path

    def get_daily(self, daily_date):
        return {
            "date": daily_date,
            "count": 2,
            "items": [
                {
                    "id": "row-1",
                    "table_id": "table-a",
                    "source_url": "https://example.com/1",
                    "title": "第一条",
                    "caption": "caption 1",
                    "video_path": str(self.media_path),
                    "max_daily_card": "card 1",
                    "max_feedback": "",
                    "duration": "19:14",
                    "daily_sort": 2,
                    "daily_selected": True,
                },
                {
                    "id": "row-2",
                    "table_id": "table-a",
                    "source_url": "https://example.com/2",
                    "title": "第二条",
                    "caption": "caption 2",
                    "video_path": str(self.media_path),
                    "max_daily_card": "card 2",
                    "max_feedback": "feedback",
                    "daily_sort": 1,
                    "daily_selected": True,
                },
            ],
            "dates": [{"date": daily_date, "count": 2}],
        }


class FakeCloudApi:
    def __init__(self):
        self.reports = {}
        self.draft_items = []
        self.uploads = {}
        self.create_uploads = []
        self.uploaded_part_numbers = []
        self.uploaded_parts = []
        self.signed_upload_url = ""
        self.signed_uploads = []
        self.completed_uploads = []
        self.aborted_uploads = []

    def upsert_draft(self, daily_date, source_table_id, items):
        self.draft_items.append(items)
        key = (daily_date, source_table_id)
        report = self.reports.setdefault(
            key,
            {"id": "report-1", "draftVersion": 0, "items": {}},
        )
        report["draftVersion"] += 1
        for item in items:
            report["items"].setdefault(
                item["localRecordId"],
                {
                    "id": "item-" + item["localRecordId"],
                    "localRecordId": item["localRecordId"],
                    "mediaId": "media-" + item["localRecordId"],
                },
            )
        return {
            "id": report["id"],
            "draftVersion": report["draftVersion"],
            "items": list(report["items"].values()),
        }

    def create_upload(self, report_id, report_item_id, filename, content_type, byte_size):
        upload_id = "upload-" + report_item_id
        create_upload = {
            "reportItemId": report_item_id,
            "filename": filename,
            "contentType": content_type,
            "byteSize": byte_size,
        }
        self.create_uploads.append(create_upload)
        self.uploads[upload_id] = create_upload
        return {
            "uploadId": upload_id,
            "objectKey": "reports/%s/items/%s/source.mp4" % (report_id, report_item_id),
            "partSize": 5,
            "signedUploadUrl": self.signed_upload_url,
        }

    def list_parts(self, upload_id):
        return []

    def upload_part(self, upload_id, part_number, body):
        self.uploaded_part_numbers.append(part_number)
        self.uploaded_parts.append((upload_id, part_number, body))
        return "etag-%s" % part_number

    def upload_signed_file(self, signed_upload_url, media_path, content_type):
        self.signed_uploads.append({
            "url": signed_upload_url,
            "path": Path(media_path),
            "contentType": content_type,
            "bytes": Path(media_path).read_bytes(),
        })

    def complete_upload(self, upload_id, sha256_hex):
        self.completed_uploads.append((upload_id, sha256_hex))

    def abort_upload(self, upload_id):
        self.aborted_uploads.append(upload_id)

    def publish_report(self, report_id, expected_draft_version):
        return {"id": report_id, "status": "published", "draftVersion": expected_draft_version}

    def item_count(self, report_id):
        return len(next(report["items"] for report in self.reports.values() if report["id"] == report_id))


class RecordingOpener:
    def __init__(self):
        self.requests = []
        self.responses = []

    def add_json(self, payload):
        self.responses.append(FakeResponse(json.dumps(payload).encode("utf-8")))

    def open(self, request, timeout=30):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("missing fake response")
        return self.responses.pop(0)


class FakeResponse:
    def __init__(self, body=b"", headers=None):
        self.body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.body

    def getheader(self, name, default=None):
        return self.headers.get(name, default)


class DailyPublisherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        self.media_path = self.tmp_path / "clip.mp4"
        self.media_path.write_bytes(b"abcdefghijk")
        self.state_store = JsonStateStore(self.tmp_path / "state.json")
        self.local_api = FakeLocalApi(self.media_path)
        self.cloud_api = FakeCloudApi()

    def test_repeat_publish_updates_same_report_without_duplicate_items(self):
        publisher = DailyPublisher(self.local_api, self.cloud_api, self.state_store)

        first = publisher.publish("2026-07-10")
        second = publisher.publish("2026-07-10")

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.cloud_api.item_count(first["id"]), 2)

    def test_publish_reports_ordered_non_secret_progress(self):
        events = []
        publisher = DailyPublisher(self.local_api, self.cloud_api, self.state_store)

        result = publisher.publish(
            "2026-07-10",
            on_progress=lambda stage, details: events.append((stage, details)),
        )

        self.assertEqual(result["id"], "report-1")
        self.assertEqual(events[0][0], "preparing")
        self.assertEqual([event[0] for event in events].count("uploading"), 2)
        self.assertEqual(events[-2][0], "publishing")
        self.assertEqual(events[-1][0], "succeeded")
        self.assertEqual(events[-1][1]["reportId"], "report-1")
        self.assertNotIn("publisher-secret-token", json.dumps(events))

    def test_publish_uploads_proxy_filename_content_type_bytes_and_sha_without_cloud_local_fields(self):
        calls = []

        def fake_preparer(source_path, duration_text, cache_root):
            calls.append((source_path, duration_text, cache_root))
            proxy = cache_root / "proxy.mp4"
            proxy.parent.mkdir(parents=True, exist_ok=True)
            proxy.write_bytes(b"proxy-bytes")
            return proxy

        DailyPublisher(
            self.local_api,
            self.cloud_api,
            self.state_store,
            media_preparer=fake_preparer,
        ).publish("2026-07-10")

        self.assertEqual(calls[0][0], self.media_path)
        self.assertEqual(calls[0][1], "19:14")
        self.assertEqual(
            calls[0][2],
            self.state_store.path.parent / "media-proxies",
        )
        proxy_bytes = b"proxy-bytes"
        self.assertNotEqual(proxy_bytes, self.media_path.read_bytes())
        self.assertEqual(len(self.cloud_api.create_uploads), 2)
        for create_upload in self.cloud_api.create_uploads:
            self.assertEqual(create_upload["filename"], "proxy.mp4")
            self.assertEqual(create_upload["contentType"], "video/mp4")
            self.assertEqual(create_upload["byteSize"], len(proxy_bytes))

        bodies_by_upload = {}
        for upload_id, _part_number, body in self.cloud_api.uploaded_parts:
            bodies_by_upload.setdefault(upload_id, []).append(body)
        self.assertEqual(
            {upload_id: b"".join(bodies) for upload_id, bodies in bodies_by_upload.items()},
            {
                "upload-item-row-1": proxy_bytes,
                "upload-item-row-2": proxy_bytes,
            },
        )
        self.assertEqual(
            self.cloud_api.completed_uploads,
            [
                ("upload-item-row-1", sha256(proxy_bytes).hexdigest()),
                ("upload-item-row-2", sha256(proxy_bytes).hexdigest()),
            ],
        )

    def test_publish_signed_upload_receives_proxy_path_content_type_bytes_and_sha(self):
        proxy_bytes = b"signed-proxy-bytes"
        self.cloud_api.signed_upload_url = "https://storage.example.com/signed?signature=secret"

        def fake_preparer(source_path, duration_text, cache_root):
            proxy = cache_root / "proxy.mp4"
            proxy.parent.mkdir(parents=True, exist_ok=True)
            proxy.write_bytes(proxy_bytes)
            return proxy

        DailyPublisher(
            self.local_api,
            self.cloud_api,
            self.state_store,
            media_preparer=fake_preparer,
        ).publish("2026-07-10")

        self.assertEqual(self.cloud_api.uploaded_parts, [])
        self.assertEqual(len(self.cloud_api.signed_uploads), 2)
        for signed_upload in self.cloud_api.signed_uploads:
            self.assertEqual(signed_upload["path"].name, "proxy.mp4")
            self.assertEqual(signed_upload["contentType"], "video/mp4")
            self.assertEqual(signed_upload["bytes"], proxy_bytes)
            self.assertNotEqual(signed_upload["bytes"], self.media_path.read_bytes())
        self.assertEqual(
            self.cloud_api.completed_uploads,
            [
                ("upload-item-row-1", sha256(proxy_bytes).hexdigest()),
                ("upload-item-row-2", sha256(proxy_bytes).hexdigest()),
            ],
        )

    def test_publish_nas_copies_proxy_bytes_to_object_key_and_completes_proxy_sha(self):
        proxy_bytes = b"nas-proxy-bytes"
        nas_root = self.tmp_path / "nas-media"

        def fake_preparer(source_path, duration_text, cache_root):
            proxy = cache_root / "proxy.mp4"
            proxy.parent.mkdir(parents=True, exist_ok=True)
            proxy.write_bytes(proxy_bytes)
            return proxy

        DailyPublisher(
            self.local_api,
            self.cloud_api,
            self.state_store,
            nas_media_root=nas_root,
            media_preparer=fake_preparer,
        ).publish("2026-07-10")

        for report_item_id in ("item-row-1", "item-row-2"):
            object_path = nas_root / ("reports/report-1/items/%s/source.mp4" % report_item_id)
            self.assertEqual(object_path.read_bytes(), proxy_bytes)
            self.assertNotEqual(object_path.read_bytes(), self.media_path.read_bytes())
        self.assertEqual(self.cloud_api.uploaded_parts, [])
        self.assertEqual(
            self.cloud_api.completed_uploads,
            [
                ("upload-item-row-1", sha256(proxy_bytes).hexdigest()),
                ("upload-item-row-2", sha256(proxy_bytes).hexdigest()),
            ],
        )

    def test_media_proxy_error_keeps_cli_details_and_does_not_complete_upload(self):
        def failing_preparer(source_path, duration_text, cache_root):
            raise MediaProxyError("ffmpeg failed: stderr /private/media/clip.mp4")

        with self.assertRaisesRegex(
            PublisherError,
            "视频超过云端免费额度，生成播放副本失败：ffmpeg failed",
        ):
            DailyPublisher(
                self.local_api,
                self.cloud_api,
                self.state_store,
                media_preparer=failing_preparer,
            ).publish("2026-07-10")

        self.assertEqual(self.cloud_api.completed_uploads, [])

    def test_resume_upload_skips_completed_parts(self):
        self.state_store.save_upload(
            media_id="media-row-1",
            upload_id="upload-item-row-1",
            object_key="reports/report-1/items/item-row-1/source.mp4",
            size=self.media_path.stat().st_size,
            part_size=5,
        )
        self.state_store.save_part("media-row-1", 1, "etag-1")

        DailyPublisher(self.local_api, self.cloud_api, self.state_store).upload_media(
            report_id="report-1",
            media_id="media-row-1",
            report_item_id="item-row-1",
            media_path=self.media_path,
        )

        self.assertNotIn(1, self.cloud_api.uploaded_part_numbers)
        self.assertIn(2, self.cloud_api.uploaded_part_numbers)
        self.assertEqual(
            self.cloud_api.completed_uploads[-1],
            ("upload-item-row-1", sha256(self.media_path.read_bytes()).hexdigest()),
        )

    def test_changed_proxy_size_aborts_conflicting_upload_and_clears_old_parts(self):
        class ConflictingCloudApi(FakeCloudApi):
            def __init__(self):
                super().__init__()
                self.create_attempts = 0

            def create_upload(self, *args, **kwargs):
                self.create_attempts += 1
                if not self.aborted_uploads:
                    error = PublisherError("云端请求失败：409 Conflict")
                    error.status_code = 409
                    raise error
                return super().create_upload(*args, **kwargs)

        cloud_api = ConflictingCloudApi()
        self.state_store.save_upload(
            media_id="media-row-1",
            upload_id="old-upload",
            object_key="reports/report-1/items/item-row-1/old.mp4",
            size=99,
            part_size=5,
        )
        self.state_store.save_part("media-row-1", 1, "old-etag")

        DailyPublisher(self.local_api, cloud_api, self.state_store).upload_media(
            report_id="report-1",
            media_id="media-row-1",
            report_item_id="item-row-1",
            media_path=self.media_path,
        )

        self.assertEqual(cloud_api.create_attempts, 2)
        self.assertEqual(cloud_api.aborted_uploads, ["old-upload"])
        self.assertIn(1, cloud_api.uploaded_part_numbers)
        self.assertEqual(
            self.state_store.get_upload("media-row-1")["upload_id"],
            "upload-item-row-1",
        )

    def test_nas_media_root_copies_file_to_cloud_object_key_without_part_upload(self):
        nas_root = self.tmp_path / "nas-media"

        DailyPublisher(
            self.local_api,
            self.cloud_api,
            self.state_store,
            nas_media_root=nas_root,
        ).upload_media(
            report_id="report-1",
            media_id="media-row-1",
            report_item_id="item-row-1",
            media_path=self.media_path,
        )

        expected_path = nas_root / "reports/report-1/items/item-row-1/source.mp4"
        self.assertEqual(expected_path.read_bytes(), self.media_path.read_bytes())
        self.assertEqual(self.cloud_api.uploaded_part_numbers, [])
        self.assertEqual(
            self.cloud_api.completed_uploads[-1],
            ("upload-item-row-1", sha256(self.media_path.read_bytes()).hexdigest()),
        )

    def test_missing_media_fails_explicitly_and_keeps_draft_retryable(self):
        missing = self.tmp_path / "missing.mp4"
        self.local_api.media_path = missing

        with self.assertRaisesRegex(PublisherError, "媒体文件不存在或不可读"):
            DailyPublisher(self.local_api, self.cloud_api, self.state_store).publish("2026-07-10")

        self.assertEqual(self.cloud_api.completed_uploads, [])

    def test_state_store_never_persists_raw_token(self):
        self.state_store.save_upload(
            media_id="media-1",
            upload_id="upload-1",
            object_key="object-key",
            size=100,
        )
        self.state_store.save_part("media-1", 1, "etag-1")

        raw = self.state_store.path.read_text("utf-8")

        self.assertNotIn("publisher-secret-token", raw)
        self.assertNotIn("token", raw.lower())

    def test_rejects_mixed_source_table_ids_before_cloud_write(self):
        original_get_daily = self.local_api.get_daily

        def mixed(daily_date):
            snapshot = original_get_daily(daily_date)
            snapshot["items"][1]["table_id"] = "table-b"
            return snapshot

        self.local_api.get_daily = mixed

        with self.assertRaisesRegex(PublisherError, "日报素材来自多个表"):
            DailyPublisher(self.local_api, self.cloud_api, self.state_store).publish("2026-07-10")

        self.assertEqual(self.cloud_api.reports, {})


class HttpApiTest(unittest.TestCase):
    def test_default_clients_do_not_inherit_system_proxy_settings(self):
        local_api = LocalDailyApi()
        cloud_api = CloudApi("https://worker.example.com", "x" * 32)

        for opener in (local_api.opener, cloud_api.opener):
            proxy_handlers = [
                handler
                for handler in opener.handlers
                if isinstance(handler, ProxyHandler)
            ]
            self.assertTrue(
                all(handler.proxies == {} for handler in proxy_handlers)
            )

    def test_cloud_api_uses_worker_publisher_endpoints_and_token_header(self):
        opener = RecordingOpener()
        opener.add_json({"id": "report-1", "draftVersion": 1, "items": []})
        opener.add_json({"uploadId": "upload-1", "partSize": 5, "objectKey": "key"})
        opener.add_json({"parts": [{"partNumber": 1, "etag": "old"}]})
        opener.add_json({})
        opener.responses.append(FakeResponse())
        opener.add_json({"id": "report-1", "status": "published"})
        api = CloudApi("https://worker.example.com", "publisher-secret-token", opener=opener)

        api.upsert_draft("2026-07-10", "table-a", [])
        api.create_upload("report-1", "item-1", "clip.mp4", "video/mp4", 10)
        api.list_parts("upload-1")
        api.complete_upload("upload-1", "a" * 64)
        api.abort_upload("upload-1")
        api.publish_report("report-1", 1)

        self.assertEqual(
            [request.full_url for request in opener.requests],
            [
                "https://worker.example.com/api/publisher/reports",
                "https://worker.example.com/api/media/uploads",
                "https://worker.example.com/api/media/uploads/upload-1/parts",
                "https://worker.example.com/api/media/uploads/upload-1/complete",
                "https://worker.example.com/api/media/uploads/upload-1",
                "https://worker.example.com/api/publisher/reports/report-1/publish",
            ],
        )
        create_upload_body = json.loads(opener.requests[1].data.decode("utf-8"))
        complete_body = json.loads(opener.requests[3].data.decode("utf-8"))
        self.assertEqual(create_upload_body["reportItemId"], "item-1")
        self.assertEqual(create_upload_body["byteSize"], 10)
        self.assertNotIn("mediaId", create_upload_body)
        self.assertNotIn("size", create_upload_body)
        self.assertEqual(complete_body, {"sha256": "a" * 64})
        for request in opener.requests:
            self.assertEqual(request.get_header("X-publisher-token"), "publisher-secret-token")
            self.assertEqual(
                request.get_header("User-agent"),
                "MAX-Daily-Publisher/1.0",
            )

    def test_upsert_draft_excludes_local_media_fields_from_worker_request(self):
        opener = RecordingOpener()
        opener.add_json({"id": "report-1", "draftVersion": 1, "items": []})
        api = CloudApi("https://worker.example.com", "publisher-secret-token", opener=opener)

        api.upsert_draft(
            "2026-07-10",
            "table-a",
            [{
                "localRecordId": "row-1",
                "title": "第一条",
                "mediaPath": "/private/clip.mp4",
                "durationText": "19:14",
            }],
        )

        payload = json.loads(opener.requests[0].data.decode("utf-8"))
        self.assertNotIn("mediaPath", payload["items"][0])
        self.assertNotIn("durationText", payload["items"][0])

    def test_local_daily_api_reads_requested_date(self):
        opener = RecordingOpener()
        opener.add_json({"date": "2026-07-10", "items": []})
        api = LocalDailyApi("http://127.0.0.1:51216", opener=opener)

        self.assertEqual(api.get_daily("2026-07-10")["date"], "2026-07-10")

        self.assertEqual(
            opener.requests[0].full_url,
            "http://127.0.0.1:51216/api/daily?date=2026-07-10",
        )

    def test_cloud_api_surfaces_http_failures(self):
        class FailingOpener:
            def open(self, request, timeout=30):
                raise HTTPError(request.full_url, 500, "server exploded", {}, None)

        api = CloudApi("https://worker.example.com", "publisher-secret-token", opener=FailingOpener())

        with self.assertRaisesRegex(PublisherError, "500 server exploded"):
            api.publish_report("report-1", 1)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)

    def test_publish_from_config_returns_worker_report_url(self):
        config = PublisherConfig(
            local_api_base="http://127.0.0.1:51216",
            cloud_api_base="https://worker.example.com",
            publisher_token="publisher-secret-token",
            state_path=self.tmp_path / "state.json",
        )
        fake_publisher = Mock()
        fake_publisher.publish.return_value = {"id": "report-1", "status": "published"}

        with patch.object(PublisherConfig, "load", return_value=config), patch(
            "publisher.max_daily_publisher.build_publisher_from_config",
            return_value=fake_publisher,
        ):
            result = publish_from_config(self.tmp_path / "config.json", "2026-07-10")

        self.assertEqual(result["reportUrl"], "https://worker.example.com/r/report-1")
        self.assertNotIn("publisher-secret-token", json.dumps(result))

    def test_config_reads_token_from_environment_without_storing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "local_api_base": "http://127.0.0.1:51216",
                        "cloud_api_base": "https://worker.example.com",
                        "state_path": str(Path(tmp) / "state.json"),
                    }
                ),
                "utf-8",
            )
            old_token = os.environ.get("PUBLISHER_DEVICE_TOKEN")
            os.environ["PUBLISHER_DEVICE_TOKEN"] = "publisher-secret-token"
            try:
                config = PublisherConfig.load(config_path)
            finally:
                if old_token is None:
                    os.environ.pop("PUBLISHER_DEVICE_TOKEN", None)
                else:
                    os.environ["PUBLISHER_DEVICE_TOKEN"] = old_token

            self.assertEqual(config.publisher_token, "publisher-secret-token")
            self.assertNotIn("publisher-secret-token", config_path.read_text("utf-8"))

    def test_bundle_config_without_existing_state_uses_stable_data_root_path(self):
        config_path = self.tmp_path / "data" / "max_daily_cloud" / "publisher" / "config.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            json.dumps({"cloud_api_base": "https://worker.example.com"}),
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"PUBLISHER_DEVICE_TOKEN": "publisher-secret-token"}):
            config = PublisherConfig.load(config_path)

        self.assertEqual(
            config.state_path,
            (self.tmp_path / "data" / ".publisher-state" / "state.json").resolve(),
        )

    def test_upgrade_keeps_existing_state_in_legacy_data_root_path(self):
        config_path = self.tmp_path / "data" / "max_daily_cloud" / "publisher" / "config.json"
        legacy_state_path = self.tmp_path / "data" / ".publisher-state" / "custom.json"
        config_path.parent.mkdir(parents=True)
        legacy_state_path.parent.mkdir(parents=True)
        legacy_state_path.write_text('{"uploads": {}}', encoding="utf-8")
        config_path.write_text(
            json.dumps(
                {
                    "cloud_api_base": "https://worker.example.com",
                    "state_path": ".publisher-state/custom.json",
                }
            ),
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"PUBLISHER_DEVICE_TOKEN": "publisher-secret-token"}):
            config = PublisherConfig.load(config_path)

        self.assertEqual(
            config.state_path,
            legacy_state_path.resolve(),
        )

    def test_config_uses_existing_config_derived_state_when_legacy_state_is_absent(self):
        config_path = self.tmp_path / "data" / "max_daily_cloud" / "publisher" / "config.json"
        config_derived_state_path = (
            self.tmp_path / "data" / "max_daily_cloud" / ".publisher-state" / "state.json"
        )
        config_path.parent.mkdir(parents=True)
        config_derived_state_path.parent.mkdir(parents=True)
        config_derived_state_path.write_text('{"uploads": {}}', encoding="utf-8")
        config_path.write_text(
            json.dumps({"cloud_api_base": "https://worker.example.com"}),
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"PUBLISHER_DEVICE_TOKEN": "publisher-secret-token"}):
            config = PublisherConfig.load(config_path)

        self.assertEqual(config.state_path, config_derived_state_path.resolve())

    def test_config_rejects_conflicting_legacy_and_config_derived_state_files(self):
        config_path = self.tmp_path / "data" / "max_daily_cloud" / "publisher" / "config.json"
        legacy_state_path = self.tmp_path / "data" / ".publisher-state" / "state.json"
        config_derived_state_path = (
            self.tmp_path / "data" / "max_daily_cloud" / ".publisher-state" / "state.json"
        )
        config_path.parent.mkdir(parents=True)
        legacy_state_path.parent.mkdir(parents=True)
        config_derived_state_path.parent.mkdir(parents=True)
        legacy_state_path.write_text('{"uploads": {"legacy": {}}}', encoding="utf-8")
        config_derived_state_path.write_text('{"uploads": {"new": {}}}', encoding="utf-8")
        config_path.write_text(
            json.dumps({"cloud_api_base": "https://worker.example.com"}),
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"PUBLISHER_DEVICE_TOKEN": "publisher-secret-token"}):
            with self.assertRaisesRegex(PublisherError, r"Conflicting publisher state files"):
                PublisherConfig.load(config_path)

    def test_config_collapses_same_file_alias_to_stable_legacy_path(self):
        config_path = self.tmp_path / "data" / "max_daily_cloud" / "publisher" / "config.json"
        legacy_state_path = self.tmp_path / "data" / ".publisher-state" / "state.json"
        config_derived_state_path = (
            self.tmp_path / "data" / "max_daily_cloud" / ".publisher-state" / "state.json"
        )
        config_path.parent.mkdir(parents=True)
        legacy_state_path.parent.mkdir(parents=True)
        config_derived_state_path.parent.mkdir(parents=True)
        legacy_state_path.write_text('{"uploads": {}}', encoding="utf-8")
        os.link(legacy_state_path, config_derived_state_path)
        config_path.write_text(
            json.dumps({"cloud_api_base": "https://worker.example.com"}),
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"PUBLISHER_DEVICE_TOKEN": "publisher-secret-token"}):
            config = PublisherConfig.load(config_path)
            JsonStateStore(config.state_path).save_upload(
                "media-1",
                "upload-1",
                "reports/report-1/media/media-1.mp4",
                123,
            )
            reloaded = PublisherConfig.load(config_path)

        self.assertEqual(reloaded.state_path, legacy_state_path.resolve())
        self.assertFalse(config_derived_state_path.exists())
        self.assertEqual(JsonStateStore(reloaded.state_path).get_upload("media-1")["size"], 123)

    def test_symlinked_config_keeps_default_and_relative_state_in_lexical_cloud_root(self):
        config_path = self.tmp_path / "data" / "max_daily_cloud" / "publisher" / "config.json"
        external_config_path = self.tmp_path / "external" / "config.json"
        config_path.parent.mkdir(parents=True)
        external_config_path.parent.mkdir(parents=True)
        config_path.symlink_to(external_config_path)

        cases = (
            (None, ".publisher-state/state.json"),
            (".publisher-state/custom.json", ".publisher-state/custom.json"),
        )
        for configured_state_path, expected_relative_path in cases:
            with self.subTest(state_path=configured_state_path):
                data = {"cloud_api_base": "https://worker.example.com"}
                if configured_state_path is not None:
                    data["state_path"] = configured_state_path
                external_config_path.write_text(json.dumps(data), encoding="utf-8")

                with patch.dict(os.environ, {"PUBLISHER_DEVICE_TOKEN": "publisher-secret-token"}):
                    config = PublisherConfig.load(config_path)

                self.assertEqual(
                    config.state_path,
                    (self.tmp_path / "data" / expected_relative_path).resolve(),
                )

    def test_config_relative_state_path_rejects_cloud_data_root_escape(self):
        config_path = self.tmp_path / "data" / "max_daily_cloud" / "publisher" / "config.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            json.dumps(
                {
                    "cloud_api_base": "https://worker.example.com",
                    "state_path": "../../outside/state.json",
                }
            ),
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"PUBLISHER_DEVICE_TOKEN": "publisher-secret-token"}):
            with self.assertRaisesRegex(PublisherError, r"state_path.*cloud data root"):
                PublisherConfig.load(config_path)

    def test_config_absolute_state_path_is_preserved(self):
        config_path = self.tmp_path / "data" / "max_daily_cloud" / "publisher" / "config.json"
        configured_state_path = self.tmp_path / "shared-state" / "state.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            json.dumps(
                {
                    "cloud_api_base": "https://worker.example.com",
                    "state_path": str(configured_state_path),
                }
            ),
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"PUBLISHER_DEVICE_TOKEN": "publisher-secret-token"}):
            config = PublisherConfig.load(config_path)

        self.assertEqual(config.state_path, configured_state_path)

    def test_default_publisher_config_path_uses_collector_data_root(self):
        self.assertEqual(
            default_publisher_config_path({"CHEN_COLLECTOR_DATA_ROOT": str(self.tmp_path)}),
            self.tmp_path.resolve() / "max_daily_cloud" / "publisher" / "config.json",
        )

    def test_default_publisher_config_path_preserves_source_tree_fallback(self):
        self.assertEqual(
            default_publisher_config_path({}),
            Path(__file__).resolve().with_name("config.json"),
        )

    def test_publisher_cli_uses_data_root_default_config(self):
        expected_path = self.tmp_path.resolve() / "max_daily_cloud" / "publisher" / "config.json"
        with patch.dict(
            os.environ,
            {"CHEN_COLLECTOR_DATA_ROOT": str(self.tmp_path)},
        ), patch(
            "publisher.max_daily_publisher.publish_from_config",
            return_value={"id": "report-1"},
        ) as publish, patch("builtins.print"):
            result = main(["2026-07-20"])

        self.assertEqual(result, 0)
        publish.assert_called_once_with(expected_path, "2026-07-20")

    def test_publisher_cli_explicit_config_overrides_data_root_default(self):
        explicit_path = self.tmp_path / "explicit-config.json"
        with patch.dict(
            os.environ,
            {"CHEN_COLLECTOR_DATA_ROOT": str(self.tmp_path)},
        ), patch(
            "publisher.max_daily_publisher.publish_from_config",
            return_value={"id": "report-1"},
        ) as publish, patch("builtins.print"):
            result = main(["2026-07-20", "--config", str(explicit_path)])

        self.assertEqual(result, 0)
        publish.assert_called_once_with(explicit_path, "2026-07-20")


if __name__ == "__main__":
    unittest.main()
