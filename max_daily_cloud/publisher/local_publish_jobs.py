import datetime as dt
import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

from .max_daily_publisher import (
    PublisherError,
    publish_from_config,
    read_keychain_password,
)


class InvalidPublishDate(ValueError):
    pass


class PublishAlreadyRunning(RuntimeError):
    pass


STAGE_MESSAGES = {
    "preparing": "正在准备日报",
    "uploading": "正在上传视频",
    "publishing": "正在发布",
    "succeeded": "发布成功",
}

PUBLIC_SNAPSHOT_FIELDS = (
    "state",
    "stage",
    "message",
    "dailyDate",
    "reportId",
    "reportUrl",
    "shareUrl",
    "startedAt",
    "finishedAt",
)
PUBLIC_LATEST_FIELDS = (
    "dailyDate",
    "reportId",
    "reportUrl",
    "shareUrl",
    "finishedAt",
)
PUBLIC_FAILURE_MESSAGE = "发布失败，请查看本地日志后重试"

SAFE_PUBLISHER_ERROR_MESSAGES = {
    "缺少发布器设备 Token：请设置 PUBLISHER_DEVICE_TOKEN 或写入 macOS Keychain": (
        "缺少发布器设备 Token：请设置 PUBLISHER_DEVICE_TOKEN 或写入 macOS Keychain"
    ),
    "config.json 缺少 cloud_api_base": "config.json 缺少 cloud_api_base",
    "这一天没有可发布的日报素材": "这一天没有可发布的日报素材",
    "日报素材缺少 table_id，无法确定 source_table_id": (
        "日报素材缺少 table_id，无法确定 source_table_id"
    ),
    "日报素材来自多个表，不能合并发布": "日报素材来自多个表，不能合并发布",
    "日报素材缺少 id": "日报素材缺少 id",
    "云端草稿缺少 report id": "云端草稿缺少 report id",
    "发布结果缺少 report id": "发布结果缺少 report id",
    "发布结果缺少日报链接": "发布结果缺少日报链接",
}
SAFE_PUBLISHER_ERROR_PREFIXES = (
    (
        "视频超过云端免费额度，生成播放副本失败：",
        "视频超过云端免费额度，生成播放副本失败",
    ),
    ("日报素材缺少 video_path：", "日报素材缺少 video_path"),
    ("媒体文件不存在或不可读：", "媒体文件不存在或不可读"),
    ("云端草稿缺少媒体占位：", "云端草稿缺少媒体占位"),
    ("云端草稿缺少 report item：", "云端草稿缺少 report item"),
)
SENSITIVE_ERROR_MARKERS = (
    "authorization",
    "bearer",
    "token",
    "header",
    "config=",
    "config:",
    "config_path",
    "publisher_token",
    "cloud_api_base=",
)

DEFAULT_FIXED_LINK_KEYCHAIN_SERVICE = "MAX Daily Fixed Collaboration Link"


def _read_keychain_service_password(service: str) -> str:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
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


def read_fixed_collaboration_url(config_path: Path) -> str:
    try:
        config = json.loads(Path(config_path).read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(config, dict):
        return ""
    cloud_url = urlsplit(str(config.get("cloud_api_base") or "").strip())
    service = str(
        config.get("fixed_link_keychain_service")
        or DEFAULT_FIXED_LINK_KEYCHAIN_SERVICE
    ).strip()
    account = str(config.get("fixed_link_keychain_account") or "").strip()
    if not service or cloud_url.scheme != "https" or not cloud_url.netloc:
        return ""
    raw_url = (
        read_keychain_password(service, account)
        if account
        else _read_keychain_service_password(service)
    )
    parsed = urlsplit(raw_url.strip())
    if (
        parsed.scheme != "https"
        or parsed.netloc != cloud_url.netloc
        or not parsed.path.startswith("/c/")
        or len(parsed.path) <= len("/c/")
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        return ""
    return raw_url.strip()


def utc_now_text() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def public_error_message(error: BaseException) -> str:
    if not isinstance(error, PublisherError):
        return PUBLIC_FAILURE_MESSAGE
    message = str(error).strip()
    if message in SAFE_PUBLISHER_ERROR_MESSAGES:
        return SAFE_PUBLISHER_ERROR_MESSAGES[message]
    for prefix, public_message in SAFE_PUBLISHER_ERROR_PREFIXES:
        if message.startswith(prefix):
            return public_message
    if any(marker in message.lower() for marker in SENSITIVE_ERROR_MARKERS):
        return PUBLIC_FAILURE_MESSAGE
    return PUBLIC_FAILURE_MESSAGE


def local_error_message(error: BaseException) -> str:
    public_message = public_error_message(error)
    if isinstance(error, PublisherError):
        message = str(error).strip()
        if (
            message.startswith("视频超过云端免费额度，生成播放副本失败：")
            and not any(marker in message.lower() for marker in SENSITIVE_ERROR_MARKERS)
        ):
            return message[:1200]
    return "%s: %s" % (type(error).__name__, public_message)


class PublishJobManager:
    _process_lock = threading.Lock()
    _process_job_active = False

    def __init__(
        self,
        config_path: Path,
        latest_path: Path,
        publish_runner: Callable[..., Dict[str, Any]] = publish_from_config,
        share_url_reader: Optional[Callable[[], str]] = None,
    ):
        self.config_path = Path(config_path)
        self.latest_path = Path(latest_path)
        self.publish_runner = publish_runner
        self.share_url_reader = share_url_reader or (lambda: "")
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._state = self._empty_state()

    @staticmethod
    def _empty_state() -> Dict[str, Any]:
        return {
            "state": "idle",
            "stage": "idle",
            "message": "尚未发布",
            "dailyDate": "",
            "reportId": "",
            "reportUrl": "",
            "startedAt": "",
            "finishedAt": "",
        }

    def _share_url(self) -> str:
        try:
            return str(self.share_url_reader() or "")
        except Exception:
            return ""

    def _public_snapshot(self, state: Dict[str, Any]) -> Dict[str, Any]:
        snapshot = {key: state.get(key, "") for key in PUBLIC_SNAPSHOT_FIELDS}
        snapshot["shareUrl"] = self._share_url()
        return snapshot

    def start(self, daily_date: str) -> Dict[str, Any]:
        try:
            dt.date.fromisoformat(daily_date)
        except (TypeError, ValueError) as error:
            raise InvalidPublishDate("日报日期格式必须为 YYYY-MM-DD") from error
        with PublishJobManager._process_lock:
            if PublishJobManager._process_job_active:
                raise PublishAlreadyRunning("已有日报正在发布")
            PublishJobManager._process_job_active = True
            try:
                with self._lock:
                    self._state = {
                        **self._empty_state(),
                        "state": "running",
                        "stage": "preparing",
                        "message": STAGE_MESSAGES["preparing"],
                        "dailyDate": daily_date,
                        "startedAt": utc_now_text(),
                    }
                    self._thread = threading.Thread(
                        target=self._run,
                        args=(daily_date,),
                        daemon=True,
                        name="max-daily-cloud-publish",
                    )
                    self._thread.start()
                    return self._public_snapshot(self._state)
            except BaseException as error:
                with self._lock:
                    self._thread = None
                    self._state.update({
                        "state": "failed",
                        "stage": "failed",
                        "message": "无法启动发布任务",
                        "finishedAt": utc_now_text(),
                    })
                PublishJobManager._process_job_active = False
                raise RuntimeError("无法启动发布任务") from error

    def _on_progress(self, stage: str, details: Dict[str, Any]) -> None:
        message = STAGE_MESSAGES.get(stage, "正在发布")
        if stage == "uploading" and details.get("total"):
            message = "正在上传视频 %s/%s" % (
                details.get("current"),
                details.get("total"),
            )
        with self._lock:
            self._state.update({"stage": stage, "message": message})

    def _run(self, daily_date: str) -> None:
        try:
            result = self.publish_runner(
                self.config_path,
                daily_date,
                self._on_progress,
            )
            public_result = {
                "dailyDate": daily_date,
                "reportId": str(result.get("id") or ""),
                "reportUrl": str(result.get("reportUrl") or ""),
                "finishedAt": utc_now_text(),
            }
            if not public_result["reportId"] or not public_result["reportUrl"]:
                raise PublisherError("发布结果缺少日报链接")
            self._write_latest(public_result)
            with self._lock:
                self._state.update({
                    "state": "succeeded",
                    "stage": "succeeded",
                    "message": STAGE_MESSAGES["succeeded"],
                    **public_result,
                })
        except BaseException as error:
            print(
                "云端日报发布失败：%s" % local_error_message(error),
                file=sys.stderr,
                flush=True,
            )
            with self._lock:
                self._state.update({
                    "state": "failed",
                    "stage": "failed",
                    "message": public_error_message(error),
                    "finishedAt": utc_now_text(),
                })
        finally:
            with PublishJobManager._process_lock:
                PublishJobManager._process_job_active = False

    def _write_latest(self, result: Dict[str, Any]) -> None:
        self.latest_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.latest_path.with_suffix(self.latest_path.suffix + ".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
        temporary.replace(self.latest_path)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return self._public_snapshot(self._state)

    def latest(self) -> Dict[str, Any]:
        try:
            if not self.latest_path.exists():
                return {}
            data = json.loads(self.latest_path.read_text("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        latest = {key: data.get(key, "") for key in PUBLIC_LATEST_FIELDS}
        latest["shareUrl"] = self._share_url()
        return latest

    def wait_for_test(self, timeout: float) -> None:
        thread = self._thread
        if thread:
            thread.join(timeout)
