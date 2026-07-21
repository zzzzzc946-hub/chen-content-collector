#!/usr/bin/env python3
import argparse
import hashlib
import json
import mimetypes
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin
from urllib.request import ProxyHandler, Request, build_opener

from .media_proxy import MediaProxyError, prepare_media_for_upload


DEFAULT_LOCAL_API_BASE = "http://127.0.0.1:51216"
DEFAULT_PART_SIZE = 5 * 1024 * 1024


class PublisherError(RuntimeError):
    pass


class CloudRequestError(PublisherError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


ProgressCallback = Callable[[str, Dict[str, Any]], None]


def emit_progress(
    callback: Optional[ProgressCallback],
    stage: str,
    **details: Any,
) -> None:
    if callback:
        callback(stage, details)


@dataclass
class PublisherConfig:
    local_api_base: str
    cloud_api_base: str
    publisher_token: str
    state_path: Path
    nas_media_root: Optional[Path] = None
    keychain_service: str = "MAX Daily Cloud Publisher"
    keychain_account: str = "publisher-device-token"

    @classmethod
    def load(cls, path: Path) -> "PublisherConfig":
        path = Path(path).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        cloud_data_root = path.parent.parent.resolve()
        data_root = cloud_data_root.parent
        path = path.resolve()
        data = json.loads(path.read_text("utf-8"))
        token = os.environ.get("PUBLISHER_DEVICE_TOKEN")
        service = str(data.get("keychain_service") or "MAX Daily Cloud Publisher")
        account = str(data.get("keychain_account") or "publisher-device-token")
        if not token:
            token = read_keychain_password(service, account)
        if not token:
            raise PublisherError("缺少发布器设备 Token：请设置 PUBLISHER_DEVICE_TOKEN 或写入 macOS Keychain")
        cloud_api_base = str(data.get("cloud_api_base") or "").strip()
        if not cloud_api_base:
            raise PublisherError("config.json 缺少 cloud_api_base")
        nas_media_root = data.get("nas_media_root")
        configured_state_path = Path(
            str(data.get("state_path") or ".publisher-state/state.json")
        ).expanduser()
        if configured_state_path.is_absolute():
            state_path = configured_state_path
        else:
            legacy_state_path = (data_root / configured_state_path).resolve()
            config_derived_state_path = (
                cloud_data_root / configured_state_path
            ).resolve()
            if not legacy_state_path.is_relative_to(data_root) or not (
                config_derived_state_path.is_relative_to(cloud_data_root)
            ):
                raise PublisherError(
                    "Relative state_path must remain beneath the cloud data root "
                    "or its legacy data root: "
                    f"{configured_state_path}"
                )
            legacy_exists = legacy_state_path.exists()
            config_derived_exists = config_derived_state_path.exists()
            if legacy_exists and config_derived_exists:
                try:
                    same_state_file = legacy_state_path.samefile(
                        config_derived_state_path
                    )
                except OSError as error:
                    raise PublisherError(
                        "Unable to compare publisher state files: "
                        f"{legacy_state_path} and {config_derived_state_path}"
                    ) from error
                if not same_state_file:
                    raise PublisherError(
                        "Conflicting publisher state files exist at both the legacy "
                        f"data-root path {legacy_state_path} and config-derived path "
                        f"{config_derived_state_path}; reconcile them before publishing"
                    )
                if legacy_state_path != config_derived_state_path:
                    try:
                        config_derived_state_path.unlink()
                    except OSError as error:
                        raise PublisherError(
                            "Unable to collapse duplicate publisher state path "
                            f"{config_derived_state_path} into {legacy_state_path}"
                        ) from error
            state_path = (
                legacy_state_path
                if legacy_exists or not config_derived_exists
                else config_derived_state_path
            )
        return cls(
            local_api_base=str(data.get("local_api_base") or DEFAULT_LOCAL_API_BASE).rstrip("/"),
            cloud_api_base=cloud_api_base.rstrip("/"),
            publisher_token=token,
            state_path=state_path,
            nas_media_root=Path(str(nas_media_root)).expanduser() if nas_media_root else None,
            keychain_service=service,
            keychain_account=account,
        )


def read_keychain_password(service: str, account: str) -> str:
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                service,
                "-a",
                account,
                "-w",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except FileNotFoundError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


class JsonStateStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"uploads": {}}
        return json.loads(self.path.read_text("utf-8"))

    def _write(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), "utf-8")
        tmp_path.replace(self.path)

    def get_upload(self, media_id: str) -> Optional[Dict[str, Any]]:
        return self._read().get("uploads", {}).get(media_id)

    def save_upload(
        self,
        media_id: str,
        upload_id: str,
        object_key: str,
        size: int,
        part_size: int = DEFAULT_PART_SIZE,
    ) -> None:
        data = self._read()
        uploads = data.setdefault("uploads", {})
        current = uploads.get(media_id, {})
        same_upload = (
            current.get("upload_id") == upload_id
            and current.get("size") == size
            and current.get("part_size") == part_size
        )
        uploads[media_id] = {
            "upload_id": upload_id,
            "object_key": object_key,
            "size": size,
            "part_size": part_size,
            "parts": current.get("parts", {}) if same_upload else {},
        }
        self._write(data)

    def save_part(self, media_id: str, part_number: int, etag: str) -> None:
        data = self._read()
        upload = data.setdefault("uploads", {}).setdefault(media_id, {"parts": {}})
        upload.setdefault("parts", {})[str(part_number)] = etag
        self._write(data)

    def get_parts(self, media_id: str) -> Dict[int, str]:
        upload = self.get_upload(media_id) or {}
        return {int(key): str(value) for key, value in upload.get("parts", {}).items()}


class LocalDailyApi:
    def __init__(self, base_url: str = DEFAULT_LOCAL_API_BASE, opener=None, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.opener = opener or build_opener(ProxyHandler({}))
        self.timeout = timeout

    def get_daily(self, daily_date: str) -> Dict[str, Any]:
        query = urlencode({"date": daily_date})
        return self._json("GET", "/api/daily?%s" % query)

    def _json(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, json.JSONDecodeError) as error:
            raise PublisherError("读取本地日报失败：%s" % error) from error


class CloudApi:
    def __init__(self, base_url: str, publisher_token: str, opener=None, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.publisher_token = publisher_token
        self.opener = opener or build_opener(ProxyHandler({}))
        self.timeout = timeout

    def upsert_draft(self, daily_date: str, source_table_id: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        payload_items = [
            {
                "localRecordId": item["localRecordId"],
                "title": item.get("title", ""),
                "caption": item.get("caption", ""),
                "sourceUrl": item.get("sourceUrl", ""),
                "maxDailyCard": item.get("maxDailyCard", ""),
                "maxFeedback": item.get("maxFeedback", ""),
                "reviewStatus": item.get("reviewStatus", "draft"),
                "itemOrder": item.get("itemOrder", 0),
            }
            for item in items
        ]
        return self._json(
            "PUT",
            "/api/publisher/reports",
            {
                "dailyDate": daily_date,
                "sourceTableId": source_table_id,
                "items": payload_items,
            },
        )

    def create_upload(
        self,
        report_id: str,
        report_item_id: str,
        filename: str,
        content_type: str,
        byte_size: int,
    ) -> Dict[str, Any]:
        return self._json(
            "POST",
            "/api/media/uploads",
            {
                "reportId": report_id,
                "reportItemId": report_item_id,
                "filename": filename,
                "contentType": content_type,
                "byteSize": byte_size,
            },
        )

    def list_parts(self, upload_id: str) -> List[Dict[str, Any]]:
        payload = self._json("GET", "/api/media/uploads/%s/parts" % quote(upload_id, safe=""))
        return list(payload.get("parts") or [])

    def upload_part(self, upload_id: str, part_number: int, body: bytes) -> str:
        path = "/api/media/uploads/%s/parts/%s" % (quote(upload_id, safe=""), part_number)
        request = self._request("PUT", path, body, content_type="application/octet-stream")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                etag = response.getheader("ETag") or response.getheader("etag")
                if not etag:
                    try:
                        payload = json.loads(response.read().decode("utf-8"))
                    except json.JSONDecodeError:
                        payload = {}
                    etag = payload.get("etag")
                if not etag:
                    raise PublisherError("云端未返回上传分片 ETag")
                return str(etag).strip('"')
        except (HTTPError, URLError, OSError) as error:
            raise PublisherError("上传分片失败：%s" % format_http_error(error)) from error

    def complete_upload(self, upload_id: str, sha256_hex: str) -> Dict[str, Any]:
        return self._json(
            "POST",
            "/api/media/uploads/%s/complete" % quote(upload_id, safe=""),
            {"sha256": sha256_hex},
        )

    def abort_upload(self, upload_id: str) -> None:
        self._json(
            "DELETE",
            "/api/media/uploads/%s" % quote(upload_id, safe=""),
        )

    def upload_signed_file(
        self,
        signed_upload_url: str,
        media_path: Path,
        content_type: str,
    ) -> None:
        request = Request(
            signed_upload_url,
            data=Path(media_path).read_bytes(),
            method="PUT",
            headers={
                "Content-Type": content_type,
                "User-Agent": "MAX-Daily-Publisher/1.0",
                "x-upsert": "true",
            },
        )
        try:
            with self.opener.open(request, timeout=max(self.timeout, 600)):
                return
        except (HTTPError, URLError, OSError) as error:
            raise PublisherError("上传 Supabase 视频失败：%s" % format_http_error(error)) from error

    def publish_report(self, report_id: str, expected_draft_version: int) -> Dict[str, Any]:
        return self._json(
            "POST",
            "/api/publisher/reports/%s/publish" % quote(report_id, safe=""),
            {"expectedDraftVersion": expected_draft_version},
        )

    def _json(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = self._request(method, path, data)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
                if not raw:
                    return {}
                return json.loads(raw.decode("utf-8"))
        except HTTPError as error:
            raise CloudRequestError(
                error.code,
                "云端请求失败：%s" % format_http_error(error),
            ) from error
        except (URLError, OSError, json.JSONDecodeError) as error:
            raise PublisherError("云端请求失败：%s" % format_http_error(error)) from error

    def _request(self, method: str, path: str, data: Optional[bytes], content_type: str = "application/json") -> Request:
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        return Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": content_type,
                "User-Agent": "MAX-Daily-Publisher/1.0",
                "X-Publisher-Token": self.publisher_token,
            },
        )


class DailyPublisher:
    def __init__(
        self,
        local_api: LocalDailyApi,
        cloud_api: CloudApi,
        state_store: JsonStateStore,
        nas_media_root: Optional[Path] = None,
        media_preparer: Callable[..., Path] = prepare_media_for_upload,
    ):
        self.local_api = local_api
        self.cloud_api = cloud_api
        self.state_store = state_store
        self.nas_media_root = Path(nas_media_root).expanduser() if nas_media_root else None
        self.media_preparer = media_preparer

    def publish(
        self,
        daily_date: str,
        on_progress: Optional[ProgressCallback] = None,
    ) -> Dict[str, Any]:
        emit_progress(on_progress, "preparing", dailyDate=daily_date)
        snapshot = self.local_api.get_daily(daily_date)
        source_table_id, items = normalize_daily_snapshot(snapshot)
        draft = self.cloud_api.upsert_draft(daily_date, source_table_id, items)
        cloud_items_by_local_id = {
            str(item.get("localRecordId")): item
            for item in draft.get("items", [])
            if item.get("localRecordId")
        }
        report_id = str(draft.get("id") or "")
        if not report_id:
            raise PublisherError("云端草稿缺少 report id")
        total = len(items)
        for index, item in enumerate(items, start=1):
            emit_progress(
                on_progress,
                "uploading",
                current=index,
                total=total,
                title=item.get("title") or item["localRecordId"],
            )
            cloud_item = cloud_items_by_local_id.get(item["localRecordId"]) or {}
            media_id = str(cloud_item.get("mediaId") or "")
            report_item_id = str(cloud_item.get("id") or "")
            if not media_id:
                raise PublisherError("云端草稿缺少媒体占位：%s" % item["localRecordId"])
            if not report_item_id:
                raise PublisherError("云端草稿缺少 report item：%s" % item["localRecordId"])
            source_path = Path(item["mediaPath"])
            if not source_path.is_file() or not os.access(source_path, os.R_OK):
                raise PublisherError("媒体文件不存在或不可读：%s" % source_path)
            try:
                upload_path = self.media_preparer(
                    source_path,
                    item.get("durationText", ""),
                    self.state_store.path.parent / "media-proxies",
                )
            except MediaProxyError as error:
                raise PublisherError(
                    "视频超过云端免费额度，生成播放副本失败：%s" % str(error)[:1000]
                ) from error
            self.upload_media(report_id, media_id, report_item_id, upload_path)
        refreshed_draft = self.cloud_api.upsert_draft(
            daily_date,
            source_table_id,
            items,
        )
        emit_progress(on_progress, "publishing", reportId=report_id)
        result = self.cloud_api.publish_report(
            report_id,
            int(refreshed_draft.get("draftVersion") or 0),
        )
        emit_progress(on_progress, "succeeded", reportId=report_id)
        return result

    def upload_media(
        self,
        report_id: str,
        media_id: str,
        report_item_id: str,
        media_path: Path,
    ) -> None:
        media_path = Path(media_path)
        if not media_path.is_file() or not os.access(media_path, os.R_OK):
            raise PublisherError("媒体文件不存在或不可读：%s" % media_path)
        size = media_path.stat().st_size
        content_type = mimetypes.guess_type(str(media_path))[0] or "application/octet-stream"
        upload = self.state_store.get_upload(media_id)
        try:
            created = self.cloud_api.create_upload(
                report_id,
                report_item_id,
                media_path.name,
                content_type,
                size,
            )
        except PublisherError as error:
            old_upload_id = str((upload or {}).get("upload_id") or "")
            old_size = (upload or {}).get("size")
            if (
                getattr(error, "status_code", None) != 409
                or not old_upload_id
                or old_size == size
            ):
                raise
            self.cloud_api.abort_upload(old_upload_id)
            created = self.cloud_api.create_upload(
                report_id,
                report_item_id,
                media_path.name,
                content_type,
                size,
            )
        if not upload or upload.get("size") != size or upload.get("upload_id") != created.get("uploadId"):
            upload = {
                "upload_id": str(created["uploadId"]),
                "object_key": str(created.get("objectKey") or ""),
                "size": size,
                "part_size": int(created.get("chunkSize") or created.get("partSize") or DEFAULT_PART_SIZE),
            }
            self.state_store.save_upload(
                media_id,
                upload["upload_id"],
                upload["object_key"],
                size,
                upload["part_size"],
            )
        upload_id = str(upload["upload_id"])
        object_key = str(upload.get("object_key") or "")
        if self.nas_media_root:
            copy_media_to_nas(media_path, self.nas_media_root, object_key)
            self.cloud_api.complete_upload(upload_id, sha256_file(media_path))
            return
        signed_upload_url = str(created.get("signedUploadUrl") or "")
        if signed_upload_url:
            self.cloud_api.upload_signed_file(
                signed_upload_url,
                media_path,
                content_type,
            )
            self.cloud_api.complete_upload(upload_id, sha256_file(media_path))
            return
        part_size = int(upload.get("part_size") or DEFAULT_PART_SIZE)
        completed = self.state_store.get_parts(media_id)
        for remote_part in self.cloud_api.list_parts(upload_id):
            if "partNumber" in remote_part and "etag" in remote_part:
                completed[int(remote_part["partNumber"])] = str(remote_part["etag"]).strip('"')
        with media_path.open("rb") as file_obj:
            for part_number, chunk in iter_file_parts(file_obj, part_size):
                if part_number in completed:
                    continue
                etag = self.cloud_api.upload_part(upload_id, part_number, chunk)
                completed[part_number] = etag
                self.state_store.save_part(media_id, part_number, etag)
        self.cloud_api.complete_upload(upload_id, sha256_file(media_path))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_media_to_nas(source_path: Path, nas_media_root: Path, object_key: str) -> Path:
    if not object_key or object_key.startswith("/") or ".." in Path(object_key).parts:
        raise PublisherError("云端返回了不安全的媒体对象路径")
    target = Path(nas_media_root) / object_key
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.stat().st_size == source_path.stat().st_size:
        return target
    shutil.copy2(source_path, target)
    return target


def iter_file_parts(file_obj, part_size: int) -> Iterable[tuple]:
    part_number = 1
    while True:
        chunk = file_obj.read(part_size)
        if not chunk:
            break
        yield part_number, chunk
        part_number += 1


def normalize_daily_snapshot(snapshot: Dict[str, Any]) -> tuple:
    raw_items = list(snapshot.get("items") or [])
    selected = [item for item in raw_items if item.get("daily_selected", True)]
    if not selected:
        raise PublisherError("这一天没有可发布的日报素材")
    table_ids = {str(item.get("table_id") or "").strip() for item in selected}
    table_ids.discard("")
    if not table_ids:
        raise PublisherError("日报素材缺少 table_id，无法确定 source_table_id")
    if len(table_ids) != 1:
        raise PublisherError("日报素材来自多个表，不能合并发布")
    normalized = []
    for index, item in enumerate(selected):
        local_record_id = str(item.get("id") or "").strip()
        if not local_record_id:
            raise PublisherError("日报素材缺少 id")
        media_path = str(item.get("video_path") or "").strip()
        if not media_path:
            raise PublisherError("日报素材缺少 video_path：%s" % local_record_id)
        normalized.append(
            {
                "localRecordId": local_record_id,
                "title": str(item.get("title") or ""),
                "caption": str(item.get("caption") or ""),
                "sourceUrl": str(item.get("source_url") or ""),
                "maxDailyCard": str(item.get("max_daily_card") or ""),
                "maxFeedback": str(item.get("max_feedback") or ""),
                "reviewStatus": str(item.get("review_status") or "draft"),
                "itemOrder": int(item.get("daily_sort") or index),
                "mediaPath": media_path,
                "durationText": str(item.get("duration") or ""),
            }
        )
    return next(iter(table_ids)), normalized


def format_http_error(error: BaseException) -> str:
    if isinstance(error, HTTPError):
        return "%s %s" % (error.code, error.reason)
    return str(error)


def build_publisher(config_path: Path) -> DailyPublisher:
    return build_publisher_from_config(PublisherConfig.load(config_path))


def build_publisher_from_config(config: PublisherConfig) -> DailyPublisher:
    return DailyPublisher(
        LocalDailyApi(config.local_api_base),
        CloudApi(config.cloud_api_base, config.publisher_token),
        JsonStateStore(config.state_path),
        nas_media_root=config.nas_media_root,
    )


def publish_from_config(
    config_path: Path,
    daily_date: str,
    on_progress: Optional[ProgressCallback] = None,
) -> Dict[str, Any]:
    config = PublisherConfig.load(config_path)
    result = build_publisher_from_config(config).publish(
        daily_date,
        on_progress=on_progress,
    )
    report_id = str(result.get("id") or "")
    if not report_id:
        raise PublisherError("发布结果缺少 report id")
    return {
        **result,
        "reportUrl": "%s/r/%s" % (
            config.cloud_api_base.rstrip("/"),
            quote(report_id, safe=""),
        ),
    }


def default_publisher_config_path(
    environ: Optional[Mapping[str, str]] = None,
) -> Path:
    values = os.environ if environ is None else environ
    configured = str(values.get("CHEN_COLLECTOR_DATA_ROOT") or "").strip()
    if configured:
        return (
            Path(configured).expanduser().resolve()
            / "max_daily_cloud"
            / "publisher"
            / "config.json"
        )
    return Path(__file__).resolve().with_name("config.json")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="发布 MAX 外部情报口喷日报到云端协同网页")
    parser.add_argument("date", help="日报日期，格式 YYYY-MM-DD")
    parser.add_argument(
        "--config",
        default=str(default_publisher_config_path()),
        help="发布器配置文件路径",
    )
    args = parser.parse_args(argv)
    try:
        result = publish_from_config(Path(args.config), args.date)
    except PublisherError as error:
        print("发布失败：%s" % error, file=sys.stderr)
        return 1
    print("发布完成：%s" % result.get("id", ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
