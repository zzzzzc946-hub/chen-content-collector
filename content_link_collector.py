#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
作品链接采集器

把飞书多维表格第一列的抖音 / 小红书 / B站 / 视频号链接解析成标题、逐字稿、封面、时长、
互动数和发布时间，再写回原记录。

只使用 Python 标准库，便于直接双击或在本机 Terminal 运行。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import copy
import csv
import datetime as dt
import hashlib
import hmac
import html
import http.server
import importlib
import importlib.util
import ipaddress
import json
import mimetypes
import os
import platform
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, NamedTuple, Optional, Tuple

def collector_code_root() -> Path:
    return Path(__file__).resolve().parent


def collector_data_root(environ: Optional[Mapping[str, str]] = None) -> Path:
    values = os.environ if environ is None else environ
    configured = str(values.get("CHEN_COLLECTOR_DATA_ROOT") or "").strip()
    return Path(configured).expanduser().resolve() if configured else collector_code_root()


def publisher_code_root() -> Path:
    return collector_code_root() / "max_daily_cloud" / "publisher"


CODE_ROOT = collector_code_root()
DATA_ROOT = collector_data_root()
HERE = DATA_ROOT
CONFIG_PATH = DATA_ROOT / "config.json"
ENV_PATH = DATA_ROOT / ".env"
TOKEN_CACHE = DATA_ROOT / ".feishu_token_cache.json"
DESKTOP_DB_PATH = DATA_ROOT / "desktop_collector.sqlite3"
DEPLOYMENT_MANIFEST_PATH = DATA_ROOT / "deployment_manifest.json"


def validate_publisher_namespace() -> None:
    namespace = "max_daily_cloud"
    expected_root = (CODE_ROOT / namespace).resolve()
    canonical_publisher_namespace = f"{namespace}.publisher"
    expected_publisher_root = publisher_code_root().resolve()
    for module_name, module in list(sys.modules.items()):
        loaded_file = getattr(module, "__file__", None)
        if loaded_file:
            loaded_path = Path(str(loaded_file)).resolve()
            is_publisher_file = (
                loaded_path == expected_publisher_root
                or loaded_path.is_relative_to(expected_publisher_root)
            )
            is_canonical_publisher = (
                module_name == canonical_publisher_namespace
                or module_name.startswith(f"{canonical_publisher_namespace}.")
            )
            if is_publisher_file and not is_canonical_publisher:
                raise ImportError(
                    f"publisher alias module {module_name} is already loaded from {loaded_path}; "
                    f"restart without the alias and import it through "
                    f"{canonical_publisher_namespace} to avoid duplicate module identities."
                )

        if module_name != namespace and not module_name.startswith(f"{namespace}."):
            continue

        origins = []
        if loaded_file:
            origins.append(Path(str(loaded_file)).resolve())
        loaded_paths = getattr(module, "__path__", None)
        if loaded_paths:
            origins.extend(Path(str(entry)).resolve() for entry in loaded_paths)
        if origins and all(
            origin == expected_root or origin.is_relative_to(expected_root)
            for origin in origins
        ):
            continue

        loaded_from = ", ".join(str(origin) for origin in origins) or "no filesystem origin"
        raise ImportError(
            f"Publisher namespace collision for {module_name}: loaded from {loaded_from}; "
            f"expected modules beneath {expected_root}. Restart without the foreign module."
        )


def load_publisher_components() -> Tuple[Any, Any, Any, Any]:
    module_name = "max_daily_cloud.publisher.local_publish_jobs"
    expected_path = publisher_code_root() / "local_publish_jobs.py"
    validate_publisher_namespace()
    existing = sys.modules.get(module_name)
    if existing is not None:
        loaded_path = Path(str(getattr(existing, "__file__", ""))).resolve()
        if loaded_path != expected_path:
            raise ImportError(
                f"Publisher module {module_name} is already loaded from {loaded_path}, "
                f"expected {expected_path}"
            )
    else:
        code_root = str(CODE_ROOT)
        sys.path[:] = [entry for entry in sys.path if entry != code_root]
        sys.path.insert(0, code_root)

    module = importlib.import_module(module_name)
    validate_publisher_namespace()
    loaded_path = Path(str(getattr(module, "__file__", ""))).resolve()
    if loaded_path != expected_path:
        raise ImportError(
            f"Publisher module {module_name} loaded from {loaded_path}, expected {expected_path}"
        )
    return (
        module.InvalidPublishDate,
        module.PublishAlreadyRunning,
        module.PublishJobManager,
        module.read_fixed_collaboration_url,
    )


(
    InvalidPublishDate,
    PublishAlreadyRunning,
    PublishJobManager,
    read_fixed_collaboration_url,
) = load_publisher_components()

DEFAULT_BASE_URL = "https://open.feishu.cn"
FEISHU_KEYCHAIN_SERVICE = "CHEN Content Collector Feishu"
FEISHU_KEYCHAIN_ACCOUNT = "app-secret"
PUBLISHER_KEYCHAIN_SERVICE = "MAX Daily Cloud Publisher"
PUBLISHER_KEYCHAIN_ACCOUNT = "publisher-device-token"
KEYCHAIN_READ_FAILED_ERROR = "无法读取 macOS Keychain，请检查钥匙串权限后重试"
SETUP_SAVE_FAILED_ERROR = "保存首次设置失败，原有配置已恢复，请重试"
SETUP_ROLLBACK_FAILED_ERROR = "首次设置保存失败且自动恢复未完成，请重新打开应用后重试"
SETUP_TRANSACTION_LOCK = threading.RLock()
BROWSER_FALLBACK_LOCK = threading.Lock()
DEFAULT_EVENT_LISTENER_LABEL = "com.chen.content-link-collector.event-listener"
DEFAULT_BROWSER_EXECUTABLES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]
DESKTOP_PROFILE_SESSIONS: Dict[str, Dict[str, Any]] = {}
DESKTOP_PROFILE_SESSIONS_LOCK = threading.Lock()
DESKTOP_PROFILE_ENRICH_SEMAPHORE = threading.Semaphore(1)
DESKTOP_DOWNLOAD_WORKERS: Dict[str, set[str]] = {}
DESKTOP_DOWNLOAD_WORKERS_LOCK = threading.Lock()
DESKTOP_DOWNLOAD_MAX_CONCURRENCY = 2
DESKTOP_ACTIVE_DOWNLOAD_STATUSES = ("queued", "preparing", "downloading", "merging")
DESKTOP_TERMINAL_DOWNLOAD_STATUSES = {"completed", "failed", "cancelled"}


DEFAULT_FIELDS = {
    "url": "作品链接",
    "platform": "平台",
    "title": "作品标题",
    "caption": "文案",
    "cover": "封面",
    "cover_url": "封面图链接",
    "duration": "时长",
    "likes": "点赞",
    "comments": "评论",
    "shares": "分享",
    "published_at": "发布时间",
    "status": "抓取状态",
    "fetched_at": "抓取时间",
    "error": "错误信息",
}

RETRY_TRANSCRIPT_STATUSES = {"待转写", "转写中", "网络异常"}
RETRY_LOGIN_STATUSES = {"等待登录"}
BROWSER_RETRY_STATUSES = {"浏览器未就绪", "浏览器连接异常"}
LOGIN_STATUSES = {"需登录", "需Cookie"}
WAITING_LOGIN_STATUS = "等待登录"
BROWSER_NOT_READY_STATUS = "浏览器未就绪"
BROWSER_CONNECTION_STATUS = "浏览器连接异常"
DESKTOP_QUEUE_STATUSES = {"待采集"}
DESKTOP_QUEUE_WORKER_STARTED: set[str] = set()
DESKTOP_QUEUE_WORKER_LOCK = threading.Lock()
MOBILE_INBOX_SYSTEM_KEY = "mobile_inbox"
MOBILE_INBOX_TABLE_NAME = "手机待扒取"
MOBILE_INBOX_DEFAULT_SOURCE = "手机收集箱"
DESKTOP_MOBILE_INBOX_WORKER_STARTED: set[str] = set()
DESKTOP_MOBILE_INBOX_WORKER_LOCK = threading.Lock()

LOGIN_URLS = {
    "抖音": "https://www.douyin.com/",
    "小红书": "https://www.xiaohongshu.com/",
    "B站": "https://www.bilibili.com/",
    "视频号": "https://channels.weixin.qq.com/",
    "YouTube": "https://www.youtube.com/",
    "Instagram": "https://www.instagram.com/",
}

FIELD_SPECS = [
    ("作品链接", 1, None),
    ("平台", 1, None),
    ("作品标题", 1, None),
    ("文案", 1, None),
    ("封面", 1, None),
    ("封面图链接", 1, None),
    ("时长", 1, None),
    ("点赞", 2, None),
    ("评论", 2, None),
    ("分享", 2, None),
    ("发布时间", 1, None),
    ("抓取状态", 1, None),
    ("抓取时间", 1, None),
    ("错误信息", 1, None),
]

HOLD_STATUSES = {
    "成功",
    "需登录",
    "需Cookie",
    "图文作品",
    "无音频",
    "下载失败",
    "转写失败",
    "平台限制",
    "VPN/网络异常",
    "YouTube下载受限",
    "yt-dlp缺失",
    "字幕缺失",
    "待人工确认",
    "需ASR",
    "ASR失败",
    "待人工处理",
    "待Downie人工下载",
}

TRANSCRIPT_KEYS = [
    "transcript",
    "subtitle",
    "subtitles",
    "captionText",
    "caption_text",
    "asrText",
    "asr_text",
    "voiceText",
    "voice_text",
]

YOUTUBE_NETWORK_STATUSES = {"VPN/网络异常", "网络异常"}

DESKTOP_ITEM_FIELDS = [
    "platform",
    "source_url",
    "source_type",
    "title",
    "caption",
    "cover_url",
    "duration",
    "likes",
    "comments",
    "shares",
    "published_at",
    "status",
    "error",
    "raw_metadata_json",
]

DESKTOP_DAILY_COLUMNS = {
    "video_path": "TEXT NOT NULL DEFAULT ''",
    "max_daily_card": "TEXT NOT NULL DEFAULT ''",
    "daily_date": "TEXT NOT NULL DEFAULT ''",
    "daily_selected": "INTEGER NOT NULL DEFAULT 0",
    "daily_sort": "INTEGER NOT NULL DEFAULT 0",
    "max_feedback": "TEXT NOT NULL DEFAULT ''",
}

TEXT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def now_text() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def default_browser_executable_path() -> str:
    for candidate in DEFAULT_BROWSER_EXECUTABLES:
        if Path(candidate).exists():
            return candidate
    return ""


class KeychainPasswordSnapshot(NamedTuple):
    found: bool
    value: str = ""


def read_keychain_password_snapshot(service: str, account: str) -> KeychainPasswordSnapshot:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception:
        raise RuntimeError(KEYCHAIN_READ_FAILED_ERROR) from None
    if result.returncode == 0:
        return KeychainPasswordSnapshot(True, result.stdout.rstrip("\r\n"))
    if result.returncode == 44:
        return KeychainPasswordSnapshot(False)
    raise RuntimeError(KEYCHAIN_READ_FAILED_ERROR) from None


def read_keychain_password(service: str, account: str) -> str:
    try:
        snapshot = read_keychain_password_snapshot(service, account)
    except RuntimeError:
        return ""
    return snapshot.value if snapshot.found else ""


def write_keychain_password(service: str, account: str, value: str) -> None:
    try:
        subprocess.run(
            ["security", "add-generic-password", "-U", "-s", service, "-a", account, "-w", value],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception:
        raise RuntimeError("无法写入 macOS Keychain，请检查钥匙串权限后重试") from None


def delete_keychain_password(service: str, account: str) -> None:
    result = subprocess.run(
        ["security", "delete-generic-password", "-s", service, "-a", account],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode not in {0, 44}:
        raise RuntimeError("无法删除 macOS Keychain 条目")


class SetupSaveError(RuntimeError):
    pass


class SetupRollbackError(RuntimeError):
    pass


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def runtime_diagnostics() -> Dict[str, Any]:
    return {
        "python": {
            "ok": sys.version_info >= (3, 12),
            "version": platform.python_version(),
        },
        "playwright": {"ok": _module_available("playwright")},
        "yt_dlp": {"ok": bool(shutil.which("yt-dlp"))},
        "ffmpeg": {"ok": bool(shutil.which("ffmpeg"))},
        "browser": {"ok": bool(default_browser_executable_path())},
    }


def setup_status(cfg: Dict[str, Any]) -> Dict[str, Any]:
    config = cfg if isinstance(cfg, dict) else {}
    feishu = config.get("feishu") if isinstance(config.get("feishu"), dict) else {}
    publisher = config.get("publisher") if isinstance(config.get("publisher"), dict) else {}
    setup = config.get("setup") if isinstance(config.get("setup"), dict) else {}
    local_only = bool(setup.get("local_only"))
    feishu_configured = all(
        bool(str(feishu.get(key) or "").strip())
        for key in ("app_id", "app_secret", "app_token", "table_id")
    )
    publisher_configured = bool(
        str(publisher.get("cloud_api_base") or "").strip()
        and str(publisher.get("device_token") or "").strip()
    )
    missing = [] if feishu_configured or local_only else ["feishu"]
    return {
        "complete": not missing,
        "local_only": local_only,
        "missing": missing,
        "groups": {
            "feishu": {"configured": feishu_configured},
            "publisher": {"configured": publisher_configured, "optional": True},
        },
        "diagnostics": runtime_diagnostics(),
    }


SETUP_STRING_FIELDS = {
    "feishu_app_id",
    "feishu_app_secret",
    "feishu_app_token",
    "feishu_table_id",
    "feishu_mobile_inbox_table_id",
    "feishu_base_url",
    "publisher_worker_url",
    "publisher_device_token",
}


def _setup_url(value: str, field_name: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{field_name} 必须是 HTTP 或 HTTPS 地址")
    return value.rstrip("/")


def _read_setup_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("现有配置文件不是有效的 JSON 对象") from error
    if not isinstance(payload, dict):
        raise ValueError("现有配置文件不是有效的 JSON 对象")
    return payload


def _atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            os.chmod(temporary, mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_write_bytes(path, data, mode=0o600)


def _snapshot_file(path: Path) -> Dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {"exists": False, "data": b"", "mode": 0o600}
    return {
        "exists": True,
        "data": target.read_bytes(),
        "mode": target.stat().st_mode & 0o7777,
    }


def _restore_file(path: Path, snapshot: Dict[str, Any]) -> None:
    target = Path(path)
    if snapshot["exists"]:
        mode = int(snapshot["mode"])
        data = bytes(snapshot["data"])
        if (
            target.exists()
            and target.read_bytes() == data
            and target.stat().st_mode & 0o7777 == mode
        ):
            return
        _atomic_write_bytes(target, data, mode=mode)
        return
    if target.exists():
        target.unlink()


def _keychain_reference(
    config: Dict[str, Any],
    service_key: str,
    account_key: str,
) -> Optional[Tuple[str, str]]:
    service = str(config.get(service_key) or "").strip()
    account = str(config.get(account_key) or "").strip()
    return (service, account) if service and account else None


def _read_setup_keychain_snapshot(
    reader: Any,
    service: str,
    account: str,
) -> KeychainPasswordSnapshot:
    try:
        value = reader(service, account)
    except Exception:
        raise RuntimeError(KEYCHAIN_READ_FAILED_ERROR) from None
    if isinstance(value, KeychainPasswordSnapshot):
        return value
    if isinstance(value, str):
        return KeychainPasswordSnapshot(bool(value), value)
    raise RuntimeError(KEYCHAIN_READ_FAILED_ERROR) from None


def _read_setup_keychain_value(reader: Any, service: str, account: str) -> str:
    snapshot = _read_setup_keychain_snapshot(reader, service, account)
    return snapshot.value if snapshot.found else ""


@contextlib.contextmanager
def _runtime_config_update(runtime_cfg: Dict[str, Any], loaded: Dict[str, Any]):
    previous = copy.deepcopy(runtime_cfg)
    try:
        runtime_cfg.clear()
        runtime_cfg.update(loaded)
    except Exception:
        try:
            runtime_cfg.clear()
            runtime_cfg.update(previous)
        except Exception:
            raise SetupRollbackError(SETUP_ROLLBACK_FAILED_ERROR) from None
        raise
    try:
        yield
    except Exception:
        try:
            runtime_cfg.clear()
            runtime_cfg.update(previous)
        except Exception:
            raise SetupRollbackError(SETUP_ROLLBACK_FAILED_ERROR) from None
        raise


def _validated_setup_payload(payload: Any, existing: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("请求内容必须是 JSON 对象")
    unknown = set(payload) - SETUP_STRING_FIELDS - {"local_only"}
    if unknown:
        raise ValueError("首次设置包含不支持的字段")
    if "local_only" in payload and not isinstance(payload["local_only"], bool):
        raise ValueError("local_only 必须是布尔值")
    local_only = bool(payload.get("local_only"))
    if local_only and any(key in payload for key in SETUP_STRING_FIELDS):
        raise ValueError("本地模式不能同时提交远端配置")

    values: Dict[str, Any] = {"local_only": local_only}
    for key in SETUP_STRING_FIELDS:
        value = payload.get(key, "")
        if not isinstance(value, str):
            raise ValueError(f"{key} 必须是字符串")
        value = value.strip()
        if "\x00" in value or len(value) > 8192:
            raise ValueError(f"{key} 格式无效")
        values[key] = value
    if local_only:
        return values

    existing_feishu = existing.get("feishu") if isinstance(existing.get("feishu"), dict) else {}
    existing_feishu_reference = bool(
        existing_feishu.get("app_secret_keychain_service")
        and existing_feishu.get("app_secret_keychain_account")
    )
    required = {
        "feishu_app_id": "飞书 App ID",
        "feishu_app_token": "飞书 Base Token",
        "feishu_table_id": "飞书数据表 ID",
    }
    missing = [label for key, label in required.items() if not values[key]]
    if not values["feishu_app_secret"] and not existing_feishu_reference:
        missing.append("飞书 App Secret")
    if missing:
        raise ValueError("缺少必填配置：" + "、".join(missing))

    publisher_values = (
        bool(values["publisher_worker_url"]),
        bool(values["publisher_device_token"]),
    )
    existing_publisher = (
        existing.get("publisher") if isinstance(existing.get("publisher"), dict) else {}
    )
    existing_publisher_reference = bool(
        existing_publisher.get("device_token_keychain_service")
        and existing_publisher.get("device_token_keychain_account")
    )
    if publisher_values[0] and not (publisher_values[1] or existing_publisher_reference):
        raise ValueError("配置云端 Worker 时必须提供发布设备 Token")
    if publisher_values[1] and not publisher_values[0]:
        raise ValueError("配置发布设备 Token 时必须提供云端 Worker 地址")
    values["feishu_base_url"] = _setup_url(
        values["feishu_base_url"] or DEFAULT_BASE_URL,
        "飞书 API 地址",
    )
    if values["publisher_worker_url"]:
        values["publisher_worker_url"] = _setup_url(
            values["publisher_worker_url"],
            "云端 Worker 地址",
        )
    return values


def save_setup(
    payload: Any,
    config_path: Path = CONFIG_PATH,
    *,
    keychain_reader: Any = None,
    keychain_writer: Any = None,
    keychain_deleter: Any = None,
    runtime_config_updater: Any = None,
) -> Dict[str, Any]:
    with SETUP_TRANSACTION_LOCK:
        return _save_setup_transaction(
            payload,
            config_path,
            keychain_reader=keychain_reader,
            keychain_writer=keychain_writer,
            keychain_deleter=keychain_deleter,
            runtime_config_updater=runtime_config_updater,
        )


def _save_setup_transaction(
    payload: Any,
    config_path: Path,
    *,
    keychain_reader: Any,
    keychain_writer: Any,
    keychain_deleter: Any,
    runtime_config_updater: Any,
) -> Dict[str, Any]:
    target = Path(config_path)
    existing = _read_setup_config(target)
    values = _validated_setup_payload(payload, existing)
    reader = keychain_reader or read_keychain_password_snapshot
    writer = keychain_writer or write_keychain_password
    deleter = keychain_deleter or delete_keychain_password
    publisher_path = target.parent / "max_daily_cloud" / "publisher" / "config.json"

    existing_feishu = (
        existing.get("feishu") if isinstance(existing.get("feishu"), dict) else {}
    )
    existing_publisher = (
        existing.get("publisher") if isinstance(existing.get("publisher"), dict) else {}
    )
    existing_feishu_reference = _keychain_reference(
        existing_feishu,
        "app_secret_keychain_service",
        "app_secret_keychain_account",
    )
    existing_publisher_reference = _keychain_reference(
        existing_publisher,
        "device_token_keychain_service",
        "device_token_keychain_account",
    )
    keychain_snapshots: Dict[Tuple[str, str], KeychainPasswordSnapshot] = {}
    keychain_mutations: list[Tuple[str, str, str]] = []
    feishu_reference = existing_feishu_reference
    publisher_reference = existing_publisher_reference

    if not values["local_only"]:
        if values["feishu_app_secret"]:
            feishu_reference = (FEISHU_KEYCHAIN_SERVICE, FEISHU_KEYCHAIN_ACCOUNT)
            keychain_snapshots[feishu_reference] = _read_setup_keychain_snapshot(
                reader,
                *feishu_reference,
            )
            keychain_mutations.append((*feishu_reference, values["feishu_app_secret"]))
        else:
            if feishu_reference is None:
                raise ValueError("缺少飞书 App Secret")
            existing_secret = _read_setup_keychain_snapshot(reader, *feishu_reference)
            if not existing_secret.found or not existing_secret.value.strip():
                raise ValueError("macOS Keychain 中缺少飞书 App Secret，请重新输入")

        if values["publisher_worker_url"]:
            if values["publisher_device_token"]:
                publisher_reference = (
                    PUBLISHER_KEYCHAIN_SERVICE,
                    PUBLISHER_KEYCHAIN_ACCOUNT,
                )
                keychain_snapshots[publisher_reference] = _read_setup_keychain_snapshot(
                    reader,
                    *publisher_reference,
                )
                keychain_mutations.append(
                    (*publisher_reference, values["publisher_device_token"])
                )
            else:
                if publisher_reference is None:
                    raise ValueError("配置云端 Worker 时必须提供发布设备 Token")
                existing_token = _read_setup_keychain_snapshot(reader, *publisher_reference)
                if not existing_token.found or not existing_token.value.strip():
                    raise ValueError("macOS Keychain 中缺少发布设备 Token，请重新输入")

    updated = json.loads(json.dumps(existing))
    setup = updated.setdefault("setup", {})
    if not isinstance(setup, dict):
        setup = {}
        updated["setup"] = setup
    setup["local_only"] = values["local_only"]

    publisher_config = None
    if not values["local_only"]:
        if feishu_reference is None:
            raise ValueError("缺少飞书 App Secret")
        feishu = updated.setdefault("feishu", {})
        if not isinstance(feishu, dict):
            feishu = {}
            updated["feishu"] = feishu
        feishu.update(
            {
                "app_id": values["feishu_app_id"],
                "app_token": values["feishu_app_token"],
                "table_id": values["feishu_table_id"],
                "mobile_inbox_table_id": values["feishu_mobile_inbox_table_id"],
                "base_url": values["feishu_base_url"],
                "app_secret_keychain_service": feishu_reference[0],
                "app_secret_keychain_account": feishu_reference[1],
            }
        )
        feishu.pop("app_secret", None)

        if values["publisher_worker_url"]:
            if publisher_reference is None:
                raise ValueError("配置云端 Worker 时必须提供发布设备 Token")
            publisher = updated.setdefault("publisher", {})
            if not isinstance(publisher, dict):
                publisher = {}
                updated["publisher"] = publisher
            publisher.update(
                {
                    "cloud_api_base": values["publisher_worker_url"],
                    "device_token_keychain_service": publisher_reference[0],
                    "device_token_keychain_account": publisher_reference[1],
                }
            )
            publisher.pop("device_token", None)
            publisher_config = {
                "local_api_base": "http://127.0.0.1:51216",
                "cloud_api_base": values["publisher_worker_url"],
                "state_path": ".publisher-state/state.json",
                "keychain_service": publisher_reference[0],
                "keychain_account": publisher_reference[1],
            }

    file_snapshots = {
        target: _snapshot_file(target),
        publisher_path: _snapshot_file(publisher_path),
    }
    attempted_keychain: list[Tuple[str, str]] = []
    try:
        for service, account, secret in keychain_mutations:
            attempted_keychain.append((service, account))
            writer(service, account, secret)
        if publisher_config is not None:
            _atomic_write_json(publisher_path, publisher_config)
        _atomic_write_json(target, updated)

        runtime_cfg = load_config(target, keychain_reader=reader)
        runtime_update = (
            runtime_config_updater(runtime_cfg)
            if runtime_config_updater is not None
            else contextlib.nullcontext()
        )
        with runtime_update:
            result = setup_status(runtime_cfg)
            if not result["complete"]:
                raise SetupSaveError(SETUP_SAVE_FAILED_ERROR)
            if publisher_config is not None and not result["groups"]["publisher"]["configured"]:
                raise SetupSaveError(SETUP_SAVE_FAILED_ERROR)
        return result
    except Exception as error:
        rollback_failed = isinstance(error, SetupRollbackError)
        for service, account in reversed(attempted_keychain):
            try:
                previous = keychain_snapshots[(service, account)]
                if previous.found:
                    writer(service, account, previous.value)
                else:
                    deleter(service, account)
            except Exception:
                rollback_failed = True
        for path, snapshot in reversed(list(file_snapshots.items())):
            try:
                _restore_file(path, snapshot)
            except Exception:
                rollback_failed = True
        if rollback_failed:
            raise SetupRollbackError(SETUP_ROLLBACK_FAILED_ERROR) from None
        raise SetupSaveError(SETUP_SAVE_FAILED_ERROR) from None


def desktop_connect(db_path: Path = DESKTOP_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def desktop_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def desktop_column_names(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def desktop_db_init(db_path: Path = DESKTOP_DB_PATH) -> None:
    with desktop_connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS collection_tables (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                default_platform TEXT NOT NULL DEFAULT '抖音',
                view_mode TEXT NOT NULL DEFAULT 'table',
                system_key TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS collected_items (
                id TEXT PRIMARY KEY,
                table_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                source_url TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'single',
                title TEXT NOT NULL DEFAULT '',
                caption TEXT NOT NULL DEFAULT '',
                cover_url TEXT NOT NULL DEFAULT '',
                duration TEXT NOT NULL DEFAULT '',
                likes INTEGER,
                comments INTEGER,
                shares INTEGER,
                published_at TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '待开始',
                error TEXT NOT NULL DEFAULT '',
                raw_metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(table_id, source_url)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_collected_items_table_updated ON collected_items(table_id, updated_at DESC)"
        )
        existing_table_columns = desktop_column_names(conn, "collection_tables")
        if "system_key" not in existing_table_columns:
            conn.execute("ALTER TABLE collection_tables ADD COLUMN system_key TEXT NOT NULL DEFAULT ''")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_collection_tables_system_key
            ON collection_tables(system_key)
            WHERE system_key != ''
            """
        )
        existing_item_columns = desktop_column_names(conn, "collected_items")
        for name, spec in DESKTOP_DAILY_COLUMNS.items():
            if name not in existing_item_columns:
                conn.execute(f"ALTER TABLE collected_items ADD COLUMN {name} {spec}")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_reports (
                id TEXT PRIMARY KEY,
                table_id TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS desktop_settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS video_download_batches (
                id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                directory TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS video_download_tasks (
                id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                stage TEXT NOT NULL DEFAULT '等待下载',
                downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                total_bytes INTEGER NOT NULL DEFAULT 0,
                progress REAL NOT NULL DEFAULT 0,
                error_code TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                output_path TEXT NOT NULL DEFAULT '',
                method TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_video_download_active_item
            ON video_download_tasks(item_id)
            WHERE status IN ('queued', 'preparing', 'downloading', 'merging')
            """
        )
        count = conn.execute("SELECT COUNT(*) FROM collection_tables").fetchone()[0]
        if not count:
            ts = now_text()
            conn.execute(
                """
                INSERT INTO collection_tables(id, name, default_platform, view_mode, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), "默认采集表", "抖音", "table", ts, ts),
            )


def desktop_list_tables(db_path: Path = DESKTOP_DB_PATH) -> List[Dict[str, Any]]:
    desktop_db_init(db_path)
    with desktop_connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT t.*,
                   (SELECT COUNT(*) FROM collected_items i WHERE i.table_id = t.id) AS item_count
            FROM collection_tables t
            ORDER BY datetime(t.updated_at) DESC, datetime(t.created_at) ASC
            """
        ).fetchall()
    return [desktop_row_to_dict(row) for row in rows]


def desktop_table_name_exists(conn: sqlite3.Connection, name: str, exclude_id: str = "") -> bool:
    if exclude_id:
        row = conn.execute(
            "SELECT 1 FROM collection_tables WHERE name = ? AND id != ? LIMIT 1",
            (name, exclude_id),
        ).fetchone()
    else:
        row = conn.execute("SELECT 1 FROM collection_tables WHERE name = ? LIMIT 1", (name,)).fetchone()
    return row is not None


def desktop_unique_table_name(conn: sqlite3.Connection, base_name: str) -> str:
    base = (base_name or "").strip() or "新采集表"
    if not desktop_table_name_exists(conn, base):
        return base
    index = 2
    while True:
        candidate = f"{base} {index}"
        if not desktop_table_name_exists(conn, candidate):
            return candidate
        index += 1


def desktop_table_is_system(table: Any) -> bool:
    try:
        return bool(table["system_key"])
    except Exception:
        return bool((table or {}).get("system_key")) if isinstance(table, dict) else False


def desktop_table_with_count(conn: sqlite3.Connection, table_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """
        SELECT t.*,
               (SELECT COUNT(*) FROM collected_items i WHERE i.table_id = t.id) AS item_count
        FROM collection_tables t
        WHERE t.id = ?
        """,
        (table_id,),
    ).fetchone()
    return desktop_row_to_dict(row) if row else None


def desktop_ensure_mobile_inbox_table(db_path: Path = DESKTOP_DB_PATH) -> Dict[str, Any]:
    desktop_db_init(db_path)
    ts = now_text()
    with desktop_connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM collection_tables WHERE system_key = ? LIMIT 1",
            (MOBILE_INBOX_SYSTEM_KEY,),
        ).fetchone()
        if row is None:
            row = conn.execute(
                """
                SELECT * FROM collection_tables
                WHERE name = ? AND system_key = ''
                ORDER BY datetime(created_at) ASC
                LIMIT 1
                """,
                (MOBILE_INBOX_TABLE_NAME,),
            ).fetchone()
        if row is None:
            table_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO collection_tables(
                    id, name, default_platform, view_mode, system_key, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (table_id, MOBILE_INBOX_TABLE_NAME, "未知", "table", MOBILE_INBOX_SYSTEM_KEY, ts, ts),
            )
        else:
            table_id = str(row["id"])
            conn.execute(
                """
                UPDATE collection_tables
                SET name = ?, default_platform = ?, system_key = ?, updated_at = ?
                WHERE id = ?
                """,
                (MOBILE_INBOX_TABLE_NAME, "未知", MOBILE_INBOX_SYSTEM_KEY, ts, table_id),
            )
        table = desktop_table_with_count(conn, table_id)
    if not table:
        raise RuntimeError("手机待扒取固定表创建失败")
    return table


def desktop_rename_table(db_path: Path, table_id: str, name: str) -> Dict[str, Any]:
    desktop_db_init(db_path)
    safe_name = (name or "").strip()
    if not table_id:
        raise ValueError("缺少 table_id")
    if not safe_name:
        raise ValueError("表格名字不能为空")
    ts = now_text()
    with desktop_connect(db_path) as conn:
        current = conn.execute("SELECT * FROM collection_tables WHERE id = ?", (table_id,)).fetchone()
        if current is None:
            raise ValueError("没有找到这个采集表格")
        if desktop_table_is_system(current):
            raise ValueError("固定表不能改名")
        if desktop_table_name_exists(conn, safe_name, table_id):
            raise ValueError("已经有同名采集表格")
        conn.execute(
            "UPDATE collection_tables SET name = ?, updated_at = ? WHERE id = ?",
            (safe_name, ts, table_id),
        )
        row = conn.execute(
            """
            SELECT t.*,
                   (SELECT COUNT(*) FROM collected_items i WHERE i.table_id = t.id) AS item_count
            FROM collection_tables t
            WHERE t.id = ?
            """,
            (table_id,),
        ).fetchone()
    if row is None:
        raise ValueError("没有找到这个采集表格")
    return desktop_row_to_dict(row)


def desktop_delete_table(db_path: Path, table_id: str) -> Dict[str, Any]:
    desktop_db_init(db_path)
    if not table_id:
        raise ValueError("缺少 table_id")
    with desktop_connect(db_path) as conn:
        row = conn.execute("SELECT * FROM collection_tables WHERE id = ?", (table_id,)).fetchone()
        if row is None:
            raise ValueError("没有找到这个采集表格")
        if desktop_table_is_system(row):
            raise ValueError("固定表不能删除")
        count = conn.execute("SELECT COUNT(*) FROM collection_tables").fetchone()[0]
        if count <= 1:
            raise ValueError("至少保留一张采集表格")
        conn.execute("DELETE FROM collected_items WHERE table_id = ?", (table_id,))
        conn.execute("DELETE FROM collection_tables WHERE id = ?", (table_id,))
        next_row = conn.execute(
            """
            SELECT t.*,
                   (SELECT COUNT(*) FROM collected_items i WHERE i.table_id = t.id) AS item_count
            FROM collection_tables t
            ORDER BY datetime(t.updated_at) DESC, datetime(t.created_at) ASC
            LIMIT 1
            """
        ).fetchone()
    return {"deleted_id": table_id, "next_table": desktop_row_to_dict(next_row) if next_row else None}


def desktop_create_table(
    db_path: Path = DESKTOP_DB_PATH,
    name: str = "",
    default_platform: str = "抖音",
) -> Dict[str, Any]:
    desktop_db_init(db_path)
    safe_platform = (default_platform or "").strip() or "抖音"
    table_id = str(uuid.uuid4())
    ts = now_text()
    with desktop_connect(db_path) as conn:
        safe_name = desktop_unique_table_name(conn, (name or "").strip() or "新采集表")
        conn.execute(
            """
            INSERT INTO collection_tables(id, name, default_platform, view_mode, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (table_id, safe_name, safe_platform, "table", ts, ts),
        )
        row = conn.execute("SELECT *, 0 AS item_count FROM collection_tables WHERE id = ?", (table_id,)).fetchone()
    return desktop_row_to_dict(row)


def desktop_int_or_none(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return parse_count(value)


def desktop_save_item(
    db_path: Path = DESKTOP_DB_PATH,
    table_id: str = "",
    item: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    desktop_db_init(db_path)
    data = item or {}
    item_id = data.get("id") or str(uuid.uuid4())
    ts = now_text()
    values = {field: data.get(field, "") for field in DESKTOP_ITEM_FIELDS}
    values["likes"] = desktop_int_or_none(values.get("likes"))
    values["comments"] = desktop_int_or_none(values.get("comments"))
    values["shares"] = desktop_int_or_none(values.get("shares"))
    if not values["raw_metadata_json"]:
        values["raw_metadata_json"] = "{}"
    with desktop_connect(db_path) as conn:
        existing_row = conn.execute(
            "SELECT raw_metadata_json FROM collected_items WHERE table_id = ? AND source_url = ?",
            (table_id, values["source_url"]),
        ).fetchone()
        if existing_row is not None and values["raw_metadata_json"] != "{}":
            try:
                existing_metadata = json.loads(str(existing_row["raw_metadata_json"] or "{}"))
                incoming_metadata = json.loads(str(values["raw_metadata_json"] or "{}"))
            except Exception:
                existing_metadata = incoming_metadata = {}
            if isinstance(existing_metadata, dict) and isinstance(incoming_metadata, dict):
                for key, value in existing_metadata.items():
                    if key.startswith("mobile_") or (
                        key == "source" and value == "手机收集箱"
                    ):
                        incoming_metadata.setdefault(key, value)
                values["raw_metadata_json"] = json.dumps(incoming_metadata, ensure_ascii=False, default=str)
        conn.execute(
            """
            INSERT INTO collected_items(
                id, table_id, platform, source_url, source_type, title, caption, cover_url,
                duration, likes, comments, shares, published_at, status, error,
                raw_metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(table_id, source_url) DO UPDATE SET
                platform = CASE WHEN excluded.platform != '' THEN excluded.platform ELSE collected_items.platform END,
                source_type = CASE WHEN excluded.source_type != '' THEN excluded.source_type ELSE collected_items.source_type END,
                title = CASE WHEN excluded.title != '' THEN excluded.title ELSE collected_items.title END,
                caption = CASE WHEN excluded.caption != '' THEN excluded.caption ELSE collected_items.caption END,
                cover_url = CASE WHEN excluded.cover_url != '' THEN excluded.cover_url ELSE collected_items.cover_url END,
                duration = CASE WHEN excluded.duration != '' THEN excluded.duration ELSE collected_items.duration END,
                likes = COALESCE(excluded.likes, collected_items.likes),
                comments = COALESCE(excluded.comments, collected_items.comments),
                shares = COALESCE(excluded.shares, collected_items.shares),
                published_at = CASE WHEN excluded.published_at != '' THEN excluded.published_at ELSE collected_items.published_at END,
                status = excluded.status,
                error = excluded.error,
                raw_metadata_json = CASE WHEN excluded.raw_metadata_json != '{}' THEN excluded.raw_metadata_json ELSE collected_items.raw_metadata_json END,
                updated_at = excluded.updated_at
            """,
            (
                item_id,
                table_id,
                values["platform"],
                values["source_url"],
                values["source_type"] or "single",
                values["title"],
                values["caption"],
                values["cover_url"],
                values["duration"],
                values["likes"],
                values["comments"],
                values["shares"],
                values["published_at"],
                values["status"] or "待开始",
                values["error"],
                values["raw_metadata_json"],
                ts,
                ts,
            ),
        )
        conn.execute("UPDATE collection_tables SET updated_at = ? WHERE id = ?", (ts, table_id))
        row = conn.execute(
            "SELECT * FROM collected_items WHERE table_id = ? AND source_url = ?",
            (table_id, values["source_url"]),
        ).fetchone()
    return desktop_row_to_dict(row)


def desktop_list_items(db_path: Path = DESKTOP_DB_PATH, table_id: str = "") -> List[Dict[str, Any]]:
    desktop_db_init(db_path)
    with desktop_connect(db_path) as conn:
        if table_id:
            rows = conn.execute(
                "SELECT * FROM collected_items WHERE table_id = ? ORDER BY datetime(updated_at) DESC",
                (table_id,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM collected_items ORDER BY datetime(updated_at) DESC").fetchall()
    return [desktop_row_to_dict(row) for row in rows]


def desktop_get_item(db_path: Path, item_id: str) -> Dict[str, Any]:
    desktop_db_init(db_path)
    if not item_id:
        raise ValueError("缺少 item_id")
    with desktop_connect(db_path) as conn:
        row = conn.execute("SELECT * FROM collected_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise ValueError("没有找到这条采集结果")
    return desktop_row_to_dict(row)


def desktop_daily_card_text(item: Dict[str, Any]) -> str:
    title = item.get("title") or "未命名素材"
    caption = desktop_caption_excerpt(item.get("caption") or "", 900)
    metrics = desktop_metric_text(item)
    return "\n".join(
        [
            "【MAX可口喷卡片】",
            "",
            "来源信息：",
            f"{item.get('platform') or ''} / {item.get('source_url') or ''} / {metrics} / 标题：{title}",
            "",
            "话题 / 母题：",
            "",
            "材料摘要：",
            caption or "暂无文案/逐字稿。",
            "",
            "可口喷判断：",
            "",
            "目标用户为什么会关心：",
            "",
        ]
    ).strip()


def desktop_daily_add_items(
    db_path: Path,
    table_id: str,
    item_ids: List[str],
    daily_date: str = "",
) -> Dict[str, Any]:
    desktop_db_init(db_path)
    selected = [str(item_id) for item_id in item_ids if str(item_id)]
    if not selected:
        return {"ok": True, "count": 0}
    date_text = str(daily_date or dt.date.today().isoformat())
    updated = 0
    ts = now_text()
    with desktop_connect(db_path) as conn:
        for item_id in selected:
            row = conn.execute("SELECT * FROM collected_items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                continue
            item = desktop_row_to_dict(row)
            card = item.get("max_daily_card") or desktop_daily_card_text(item)
            sort_row = conn.execute(
                "SELECT COALESCE(MAX(daily_sort), 0) FROM collected_items WHERE table_id = ? AND daily_date = ?",
                (table_id or item.get("table_id") or "", date_text),
            ).fetchone()
            next_sort = int(sort_row[0] or 0) + 1
            conn.execute(
                """
                UPDATE collected_items
                SET daily_selected = 1, daily_date = ?, daily_sort = ?, max_daily_card = ?, updated_at = ?
                WHERE id = ?
                """,
                (date_text, next_sort, card, ts, item_id),
            )
            updated += 1
    return {"ok": True, "count": updated, "date": date_text}


def desktop_daily_remove_items(db_path: Path, item_ids: List[str]) -> Dict[str, Any]:
    desktop_db_init(db_path)
    selected = [str(item_id) for item_id in item_ids if str(item_id)]
    if not selected:
        return {"ok": True, "count": 0}
    ts = now_text()
    with desktop_connect(db_path) as conn:
        for item_id in selected:
            conn.execute(
                """
                UPDATE collected_items
                SET daily_selected = 0, daily_date = '', daily_sort = 0, updated_at = ?
                WHERE id = ?
                """,
                (ts, item_id),
            )
    return {"ok": True, "count": len(selected)}


def desktop_daily_update_card(db_path: Path, item_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    desktop_db_init(db_path)
    editable = {"max_daily_card", "max_feedback", "daily_sort", "daily_date"}
    clean: Dict[str, Any] = {}
    for key, value in (updates or {}).items():
        if key not in editable:
            continue
        clean[key] = int(value or 0) if key == "daily_sort" else str(value or "")
    if not item_id:
        raise ValueError("缺少 item_id")
    if not clean:
        raise ValueError("没有可更新的日报字段")
    ts = now_text()
    assignments = ", ".join([f"{key} = ?" for key in clean])
    values = list(clean.values())
    values.extend([ts, item_id])
    with desktop_connect(db_path) as conn:
        conn.execute(f"UPDATE collected_items SET {assignments}, updated_at = ? WHERE id = ?", values)
        row = conn.execute("SELECT * FROM collected_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise ValueError("没有找到这条日报卡片")
    return desktop_row_to_dict(row)


def desktop_daily_cards(db_path: Path, table_id: str = "", daily_date: str = "") -> List[Dict[str, Any]]:
    desktop_db_init(db_path)
    params: List[Any] = []
    where = ["daily_selected = 1"]
    if table_id:
        where.append("table_id = ?")
        params.append(table_id)
    if daily_date:
        where.append("daily_date = ?")
        params.append(daily_date)
    with desktop_connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM collected_items
            WHERE {' AND '.join(where)}
            ORDER BY daily_date DESC, daily_sort ASC, datetime(updated_at) DESC
            """,
            params,
        ).fetchall()
    return [desktop_row_to_dict(row) for row in rows]


def desktop_daily_summary(db_path: Path, table_id: str = "", daily_date: str = "") -> Dict[str, Any]:
    all_cards = desktop_daily_cards(db_path, table_id, "")
    dates: Dict[str, int] = {}
    for item in all_cards:
        date_key = str(item.get("daily_date") or "")
        if date_key:
            dates[date_key] = dates.get(date_key, 0) + 1
    selected_date = daily_date or (all_cards[0].get("daily_date") if all_cards else dt.date.today().isoformat())
    cards = [item for item in all_cards if str(item.get("daily_date") or "") == selected_date] if selected_date else all_cards
    return {
        "ok": True,
        "date": selected_date,
        "count": len(cards),
        "items": cards,
        "dates": [{"date": date, "count": count} for date, count in sorted(dates.items(), reverse=True)],
    }


def desktop_daily_video_path(db_path: Path, item_id: str) -> Path:
    item = desktop_get_item(db_path, item_id)
    value = str(item.get("video_path") or "").strip()
    if not value:
        raise FileNotFoundError("这条素材尚未下载视频")
    path = Path(value).expanduser()
    if not path.is_file():
        raise FileNotFoundError("本地视频文件不存在，请重新下载")
    return path


def desktop_update_item(db_path: Path, item_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    desktop_db_init(db_path)
    editable = {
        "platform",
        "title",
        "caption",
        "duration",
        "likes",
        "comments",
        "shares",
        "published_at",
        "status",
        "error",
        "max_daily_card",
    }
    clean: Dict[str, Any] = {}
    for key, value in updates.items():
        if key not in editable:
            continue
        if key in {"likes", "comments", "shares"}:
            clean[key] = desktop_int_or_none(value)
        elif key == "max_daily_card":
            clean[key] = str(value or "")
        else:
            clean[key] = str(value or "").strip()
    if not item_id:
        raise ValueError("缺少 item_id")
    if not clean:
        raise ValueError("没有可更新的字段")
    ts = now_text()
    assignments = ", ".join([f"{key} = ?" for key in clean])
    values = list(clean.values())
    values.extend([ts, item_id])
    with desktop_connect(db_path) as conn:
        conn.execute(
            f"UPDATE collected_items SET {assignments}, updated_at = ? WHERE id = ?",
            values,
        )
        row = conn.execute("SELECT * FROM collected_items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise ValueError("没有找到这条采集结果")
        conn.execute("UPDATE collection_tables SET updated_at = ? WHERE id = ?", (ts, row["table_id"]))
    return desktop_row_to_dict(row)


def desktop_status_from_meta(meta: Dict[str, Any], caption: str = "") -> str:
    if meta.get("content_type") in {"image", "images", "note"} or meta.get("duration") == "图文":
        return "图文作品"
    if meta.get("content_type") == "video" and not caption and not media_url_or_empty(meta):
        return "需ASR"
    return "成功"


def desktop_item_from_meta(
    url: str,
    source_type: str,
    meta: Dict[str, Any],
    caption: str,
    status: str,
    error: str = "",
) -> Dict[str, Any]:
    return {
        "platform": meta.get("platform") or detect_platform(url) or "未知",
        "source_url": meta.get("source_url") or url,
        "source_type": source_type,
        "title": meta.get("title") or "",
        "caption": caption or meta.get("caption") or "",
        "cover_url": meta.get("cover_url") or "",
        "duration": meta.get("duration") or "",
        "likes": meta.get("likes"),
        "comments": meta.get("comments"),
        "shares": meta.get("shares"),
        "published_at": meta.get("published_at") or "",
        "status": status,
        "error": error,
        "raw_metadata_json": json.dumps(meta, ensure_ascii=False, default=str),
    }


def desktop_asr_available(cfg: Dict[str, Any]) -> bool:
    backend = ((cfg.get("asr") or {}).get("backend") or "local").lower()
    if backend in {"tencent", "tencent_auto"}:
        tencent_cfg = cfg.get("tencent_asr") or {}
        return bool(
            (os.environ.get("TENCENTCLOUD_SECRET_ID") or tencent_cfg.get("secret_id") or "").strip()
            and (os.environ.get("TENCENTCLOUD_SECRET_KEY") or tencent_cfg.get("secret_key") or "").strip()
        )
    if backend == "openai":
        try:
            return bool(load_openai_key(cfg))
        except SystemExit:
            return False
    if backend == "local":
        try:
            ffmpeg_path()
        except RuntimeError:
            return False
        if not ffmpeg_path():
            return False
        try:
            import whisper  # type: ignore # noqa: F401
            return True
        except ImportError:
            return False
    return False


def youtube_safety_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    raw = dict(cfg.get("youtube_safety") or {})
    raw.setdefault("enabled", True)
    raw.setdefault("preflight", True)
    raw.setdefault("connectivity_check", False)
    raw.setdefault("connectivity_url", "https://www.youtube.com/generate_204")
    raw.setdefault("throttle_seconds", 3.0)
    raw.setdefault("max_consecutive_network_failures", 2)
    raw.setdefault("open_browser_before_scrape", True)
    raw.setdefault("browser_gate_timeout", 12)
    return raw


def is_youtube_target(url: str = "", platform: str = "") -> bool:
    return platform == "YouTube" or detect_platform(url) == "YouTube"


def youtube_preflight_check(cfg: Dict[str, Any]) -> Dict[str, Any]:
    safety = youtube_safety_config(cfg)
    if not safety.get("enabled", True) or not safety.get("preflight", True):
        return {"ok": True, "status": "跳过", "error": ""}
    if not ytdlp_path():
        return {
            "ok": False,
            "status": "yt-dlp缺失",
            "error": "本机没有找到 yt-dlp，无法稳定抓取 YouTube 元数据。请先安装或修复 yt-dlp。",
        }
    if safety.get("connectivity_check"):
        try:
            req = urllib.request.Request(str(safety.get("connectivity_url") or "https://www.youtube.com/"), headers=TEXT_HEADERS)
            with urllib.request.urlopen(req, timeout=8) as resp:
                if resp.status >= 500:
                    raise RuntimeError(f"YouTube 连通性检查返回 HTTP {resp.status}")
        except Exception as e:
            return {
                "ok": False,
                "status": "VPN/网络异常",
                "error": f"YouTube 连通性预检失败，请确认 VPN 已连接且出口稳定：{str(e)[:500]}",
            }
    return {"ok": True, "status": "可采集", "error": ""}


def ytdlp_cookie_file_path(cfg: Dict[str, Any]) -> Optional[Path]:
    ytdlp_cfg = cfg.get("yt_dlp") or {}
    cookies_file = ytdlp_cfg.get("cookies_file") or ""
    cookie_path = Path(cookies_file).expanduser() if cookies_file else None
    if cookie_path and not cookie_path.is_absolute():
        cookie_path = HERE / cookie_path
    if cookie_path and not cookie_path.exists() and (HERE / "cookies.txt").exists():
        cookie_path = HERE / "cookies.txt"
    return cookie_path


def browser_name_from_executable(path: str) -> str:
    text = str(path or "").lower()
    if "microsoft edge" in text or "/edge" in text:
        return "edge"
    if "google chrome" in text or "/chrome" in text:
        return "chrome"
    if "brave" in text:
        return "brave"
    if "chromium" in text:
        return "chromium"
    if "firefox" in text:
        return "firefox"
    return ""


def browser_name_from_ytdlp_cookie_source(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return re.split(r"[:+]", text, maxsplit=1)[0].strip()


def youtube_diagnose_config(cfg: Dict[str, Any], url: str = "") -> Dict[str, Any]:
    ytdlp_cfg = cfg.get("yt_dlp") or {}
    cookie_path = ytdlp_cookie_file_path(cfg)
    strategies = ytdlp_download_strategies(url or "https://www.youtube.com/watch?v=dummy", cfg)
    login_browser = browser_name_from_executable(str((cfg.get("browser_fallback") or {}).get("executable_path") or ""))
    cookie_browser = browser_name_from_ytdlp_cookie_source(str(ytdlp_cfg.get("cookies_from_browser") or ""))
    browser_cookie_mismatch = bool(login_browser and cookie_browser and login_browser != cookie_browser)
    cookie_advice = (
        f"专用登录浏览器是 {login_browser}，但 yt-dlp 读取的是 {cookie_browser} Cookie；建议统一到同一个浏览器。"
        if browser_cookie_mismatch
        else ""
    )
    return {
        "yt_dlp_path": bool(ytdlp_path()),
        "proxy_configured": bool(str(ytdlp_cfg.get("proxy") or "").strip()),
        "cookies_file_configured": bool(str(ytdlp_cfg.get("cookies_file") or "").strip()),
        "cookies_file_exists": bool(cookie_path and cookie_path.exists()),
        "cookies_from_browser": bool(str(ytdlp_cfg.get("cookies_from_browser") or "").strip()),
        "login_browser": login_browser,
        "cookie_browser": cookie_browser,
        "browser_cookie_mismatch": browser_cookie_mismatch,
        "cookie_advice": cookie_advice,
        "po_token_configured": bool(str(ytdlp_cfg.get("youtube_po_token") or "").strip()),
        "po_token_provider_enabled": bool(ytdlp_cfg.get("youtube_po_token_provider")),
        "retry_strategies": [s or "default" for s in strategies],
    }


def youtube_check(ok: bool, status: str, message: str = "") -> Dict[str, Any]:
    return {"ok": bool(ok), "status": status, "message": message}


def youtube_download_needs_po_token(error: Any) -> bool:
    text = str(error).lower()
    return any(token in text for token in ("403", "forbidden", "fragment", "po token", "gvs"))


def youtube_diagnose_url(url: str, cfg: Dict[str, Any], probe_download: bool = False) -> Dict[str, Any]:
    normalized = normalize_url(url) or str(url or "").strip()
    result: Dict[str, Any] = {
        "url": normalized,
        "platform": detect_platform(normalized),
        "ok": False,
        "status": "待人工确认",
        "recommended_action": "",
        "checks": {},
        "config": youtube_diagnose_config(cfg, normalized),
        "metadata": {},
    }
    if not is_youtube_target(normalized, result["platform"]):
        result["status"] = "非YouTube链接"
        result["recommended_action"] = "请粘贴 YouTube 视频或 Shorts 链接。"
        return result

    preflight = youtube_preflight_check(cfg)
    result["checks"]["preflight"] = youtube_check(
        bool(preflight.get("ok")),
        str(preflight.get("status") or ""),
        str(preflight.get("error") or ""),
    )
    if not preflight.get("ok"):
        result["status"] = preflight.get("status") or "预检失败"
        result["recommended_action"] = preflight.get("error") or "请先修复 YouTube 采集预检问题。"
        return result

    try:
        meta = extract_with_ytdlp(normalized, cfg)
    except Exception as e:
        status = classify_processing_error(e)
        result["checks"]["metadata"] = youtube_check(False, status, str(e)[:800])
        result["status"] = status
        config_advice = str((result.get("config") or {}).get("cookie_advice") or "")
        result["recommended_action"] = (
            config_advice
            or "yt-dlp 元数据阶段失败；优先检查 Cookie、VPN 节点和 YouTube 登录态。"
        )
        return result

    result["metadata"] = {
        "title": meta.get("title") or "",
        "duration": meta.get("duration") or "",
        "has_media_url": bool(media_url_or_empty(meta)),
    }
    result["checks"]["metadata"] = youtube_check(bool(meta.get("title") or media_url_or_empty(meta)), "元数据可用")
    if str(meta.get("caption") or "").strip():
        result["checks"]["captions"] = youtube_check(True, "官方字幕可用")
        result["ok"] = True
        result["status"] = "字幕可用"
        result["recommended_action"] = "该视频已暴露官方字幕/文字稿，可直接采集逐字稿。"
        return result

    result["checks"]["captions"] = youtube_check(False, "无官方字幕", "YouTube 未暴露 subtitles / automatic captions。")
    if not probe_download:
        result["status"] = "需下载音频"
        result["recommended_action"] = "该视频无官方字幕；如需逐字稿，请运行下载探测，或先配置稳定 Cookie / PO Token。"
        return result

    try:
        probe_cfg = cfg
        if probe_download:
            probe_cfg = dict(cfg)
            ytdlp_cfg = dict(probe_cfg.get("yt_dlp") or {})
            ytdlp_cfg["_download_probe"] = True
            ytdlp_cfg.setdefault("download_probe_timeout", 45)
            ytdlp_cfg.setdefault("download_probe_max_attempts", 6)
            probe_cfg["yt_dlp"] = ytdlp_cfg
        path = download_media_with_ytdlp(normalized, probe_cfg)
        parent = path.parent
        shutil.rmtree(parent, ignore_errors=True)
        result["checks"]["download_probe"] = youtube_check(True, "音频可下载")
        result["ok"] = True
        result["status"] = "可进入ASR"
        result["recommended_action"] = "音频下载通道可用，可以进入 ASR 转写。"
        return result
    except Exception as e:
        if youtube_download_needs_po_token(e):
            result["status"] = "下载需PO Token"
            result["recommended_action"] = "音频分片下载被 YouTube 拦截；优先配置 PO Token Provider / youtube_po_token，并确认 VPN 出口稳定。"
        else:
            result["status"], result["recommended_action"] = youtube_desktop_error_status(classify_processing_error(e), e)
        result["checks"]["download_probe"] = youtube_check(False, result["status"], str(e)[:800])
        return result


def youtube_should_open_browser_before_scrape(cfg: Dict[str, Any]) -> bool:
    safety = youtube_safety_config(cfg)
    fallback_cfg = cfg.get("browser_fallback")
    return bool(
        safety.get("enabled", True)
        and safety.get("open_browser_before_scrape", True)
        and isinstance(fallback_cfg, dict)
        and fallback_cfg.get("enabled", True)
    )


def youtube_prepare_browser_for_scrape(url: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    if not youtube_should_open_browser_before_scrape(cfg):
        return {"ok": True, "opened": False, "status": "跳过"}
    fallback_cfg = browser_fallback_config(cfg)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        launch_cdp_browser(fallback_cfg, url)
        return {"ok": True, "opened": True, "status": "已打开"}

    safety = youtube_safety_config(cfg)
    timeout_ms = max(3000, int(float(safety.get("browser_gate_timeout") or 12) * 1000))
    with BROWSER_FALLBACK_LOCK:
        launch_cdp_browser(fallback_cfg, url)
        with sync_playwright() as p:
            browser = connect_cdp_browser_with_recovery(p, fallback_cfg, "YouTube")
            context = browser.contexts[0] if browser.contexts else browser.new_context(viewport={"width": 1280, "height": 900})
            existing_pages = set(getattr(context, "pages", []) or [])
            page = cdp_page_for_url(context, url)
            page_was_existing_video = page in existing_pages and youtube_video_id(getattr(page, "url", "")) == youtube_video_id(url)
            should_keep_page = False
            try:
                if youtube_video_id(getattr(page, "url", "")) != youtube_video_id(url):
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(1500)
                body_text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                lowered = str(body_text).lower()
                if "确认你不是聊天机器人" in body_text or "sign in to confirm" in lowered:
                    should_keep_page = True
                    raise RuntimeError("YouTube 要求登录验证：请在专用浏览器里完成 YouTube 登录/机器人验证后重试。")
                return {"ok": True, "opened": True, "status": "可采集"}
            finally:
                if not should_keep_page and not page_was_existing_video:
                    try:
                        page.close()
                    except Exception:
                        pass
                if not fallback_cfg.get("keep_open", True):
                    browser.close()


def youtube_desktop_error_status(status: str, error: Any) -> Tuple[str, str]:
    text = str(error)
    lowered = text.lower()
    if status == "网络异常":
        return "VPN/网络异常", f"YouTube 访问失败，请确认 VPN 已连接且网络出口稳定：{text[:900]}"
    if status == "需登录" and ("sign in to confirm" in lowered or "not a bot" in lowered or "机器人验证" in text):
        return (
            "需登录",
            "YouTube 官方文字稿为空，已进入音频 ASR 兜底，但 yt-dlp 被 YouTube 登录/机器人验证拦截。"
            f"请刷新 YouTube 登录态/Cookie，确认 VPN 节点可播放该视频；若仍失败，需要配置 yt-dlp PO Token 后再试：{text[:650]}",
        )
    if status == "下载失败" and ("403" in text or "fragment" in lowered or "forbidden" in lowered):
        return "YouTube下载受限", f"YouTube 音频下载被限制，常见原因是 VPN 出口风控、Cookie 失效，或 YouTube 新的 PO Token 校验。请先切换 VPN 节点、刷新 YouTube 登录态/Cookie；如果仍失败，需要配置 yt-dlp PO Token 后再试：{text[:800]}"
    return status, text[:1000]


def desktop_scrape_single_url(
    db_path: Path,
    table_id: str,
    url: str,
    cfg: Dict[str, Any],
    platform_hint: str = "",
    source_type: str = "single",
    transcribe: bool = True,
) -> Dict[str, Any]:
    normalized = normalize_url(url)
    platform = platform_hint or detect_platform(normalized) or "未知"
    if not normalized:
        return desktop_save_item(
            db_path,
            table_id,
            {
                "platform": platform,
                "source_url": url,
                "source_type": source_type,
                "status": "待人工确认",
                "error": "没有识别到有效链接。",
                "raw_metadata_json": "{}",
            },
        )
    if is_youtube_target(normalized, platform):
        preflight = youtube_preflight_check(cfg)
        if not preflight.get("ok"):
            return desktop_save_item(
                db_path,
                table_id,
                {
                    "platform": "YouTube",
                    "source_url": normalized,
                    "source_type": source_type,
                    "status": preflight.get("status") or "待人工确认",
                    "error": preflight.get("error") or "YouTube 预检失败。",
                    "raw_metadata_json": json.dumps({"preflight": preflight}, ensure_ascii=False),
                },
            )
    try:
        meta = extract_from_page(normalized, cfg)
        if platform_hint and not meta.get("platform"):
            meta["platform"] = platform_hint
        caption = (meta.get("caption") or "").strip()
        is_youtube_meta = is_youtube_target(normalized, meta.get("platform") or platform_hint)
        has_video_to_transcribe = bool(
            meta.get("content_type") == "video"
            and not caption
            and (media_url_or_empty(meta) or is_youtube_meta)
        )
        skip_asr_unavailable = bool(
            transcribe
            and has_video_to_transcribe
            and (meta.get("platform") or platform_hint) in {"YouTube", "Instagram"}
            and not desktop_asr_available(cfg)
        )
        if transcribe and has_video_to_transcribe and not skip_asr_unavailable:
            try:
                caption = transcribe_from_meta(cfg, meta).strip()
                if caption and (cfg.get("asr") or {}).get("format_transcript", True):
                    caption = format_transcript_text(caption)
            except Exception as e:
                status = classify_processing_error(e)
                if status == "待人工确认":
                    status = "ASR失败"
                if is_youtube_meta:
                    status, error = youtube_desktop_error_status(status, e)
                else:
                    error = str(e)[:1000]
                return desktop_save_item(
                    db_path,
                    table_id,
                    desktop_item_from_meta(normalized, source_type, meta, "", status, error),
                )
        status = desktop_status_from_meta(meta, caption)
        quality = metadata_quality_message(meta)
        error = quality if status != "成功" else ""
        if skip_asr_unavailable:
            if (meta.get("platform") or platform_hint) == "YouTube":
                status = "字幕缺失"
                error = "已补全基础信息和视频直链；YouTube 没有可用字幕，且未配置可用 ASR，暂未生成逐字稿。"
            else:
                status = "基础信息成功"
                error = "已补全基础信息和视频直链；未配置可用 ASR，暂未生成逐字稿。"
        if not transcribe and has_video_to_transcribe:
            status = "基础信息成功"
            error = "已补全基础信息；主页批量模式暂不阻塞等待长视频 ASR，逐字稿可后续单条补转。"
        return desktop_save_item(
            db_path,
            table_id,
            desktop_item_from_meta(normalized, source_type, meta, caption, status, error),
        )
    except Exception as e:
        status = classify_processing_error(e)
        if is_youtube_target(normalized, platform):
            status, error = youtube_desktop_error_status(status, e)
        else:
            error = str(e)[:1000]
        return desktop_save_item(
            db_path,
            table_id,
            {
                "platform": platform,
                "source_url": normalized,
                "source_type": source_type,
                "status": status,
                "error": error,
                "raw_metadata_json": "{}",
            },
        )


def desktop_queue_pending_items(db_path: Path, limit: int = 20) -> List[Dict[str, Any]]:
    desktop_db_init(db_path)
    placeholders = ",".join("?" for _ in DESKTOP_QUEUE_STATUSES)
    with desktop_connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM collected_items
            WHERE status IN ({placeholders})
            ORDER BY datetime(created_at) ASC
            LIMIT ?
            """,
            [*DESKTOP_QUEUE_STATUSES, max(1, limit)],
        ).fetchall()
    return [desktop_row_to_dict(row) for row in rows]


def desktop_queue_add_urls(
    db_path: Path,
    table_id: str,
    urls: List[str],
    platform: str = "",
    source_type: str = "single",
) -> Dict[str, Any]:
    if not table_id:
        raise ValueError("缺少 table_id")
    saved: List[Dict[str, Any]] = []
    for raw in urls:
        url = normalize_url(str(raw)) or str(raw).strip()
        if not url:
            continue
        item_platform = platform or detect_platform(url) or "未知"
        saved.append(
            desktop_save_item(
                db_path,
                table_id,
                {
                    "platform": item_platform,
                    "source_url": url,
                    "source_type": source_type,
                    "title": "待采集任务",
                    "status": "待采集",
                    "error": "已进入手机任务队列；Mac 采集引擎在线时会自动执行。",
                    "raw_metadata_json": json.dumps(
                        {"queued_at": now_text(), "platform": item_platform, "source_url": url},
                        ensure_ascii=False,
                    ),
                },
            )
        )
    return {"ok": True, "queued": saved, "count": len(saved)}


def desktop_queue_process_once(db_path: Path, cfg: Dict[str, Any], limit: int = 3) -> Dict[str, Any]:
    pending = desktop_queue_pending_items(db_path, limit=limit)
    processed: List[Dict[str, Any]] = []
    for item in pending:
        item_id = str(item.get("id") or "")
        table_id = str(item.get("table_id") or "")
        url = str(item.get("source_url") or "")
        platform = str(item.get("platform") or detect_platform(url) or "未知")
        source_type = str(item.get("source_type") or "single")
        if not item_id or not table_id or not url:
            continue
        try:
            desktop_update_item(db_path, item_id, {"status": "采集中", "error": "Mac 采集引擎正在处理这条任务。"})
            result = desktop_scrape_single_url(db_path, table_id, url, cfg, platform, source_type, True)
            processed.append({"id": result.get("id"), "status": result.get("status"), "title": result.get("title")})
        except Exception as e:
            try:
                desktop_update_item(db_path, item_id, {"status": classify_processing_error(e), "error": str(e)[:1000]})
            except Exception:
                pass
            processed.append({"id": item_id, "status": "失败", "error": str(e)[:500]})
    return {"ok": True, "processed": processed, "count": len(processed), "remaining": len(desktop_queue_pending_items(db_path, limit=1000))}


def desktop_queue_worker(db_path: Path, cfg: Dict[str, Any], stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            desktop_queue_process_once(db_path, cfg, limit=2)
        except Exception as e:
            print(f"手机任务队列补跑失败：{e}", flush=True)
        stop_event.wait(20)


def desktop_start_queue_worker(db_path: Path, cfg: Dict[str, Any]) -> None:
    key = str(db_path.resolve())
    with DESKTOP_QUEUE_WORKER_LOCK:
        if key in DESKTOP_QUEUE_WORKER_STARTED:
            return
        DESKTOP_QUEUE_WORKER_STARTED.add(key)
    stop_event = threading.Event()
    thread = threading.Thread(target=desktop_queue_worker, args=(db_path, cfg, stop_event), daemon=True)
    thread.start()


def mobile_inbox_status_rank(status: str) -> int:
    value = str(status or "").strip()
    if not value or value in {"待开始", "待扒取"}:
        return 0
    if value in {"扒取中", "采集中", "待采集"}:
        return 1
    if value == "成功":
        return 3
    if value in HOLD_STATUSES:
        return 2
    return 1


def mobile_inbox_should_apply_status(
    current_status: str,
    incoming_status: str,
    current_remote_mtime: int = 0,
    incoming_remote_mtime: int = 0,
) -> bool:
    if current_remote_mtime and incoming_remote_mtime and incoming_remote_mtime < current_remote_mtime:
        return False
    current = str(current_status or "").strip()
    incoming = str(incoming_status or "").strip()
    if incoming == current:
        return True
    return mobile_inbox_status_rank(incoming) > mobile_inbox_status_rank(current)


def mobile_inbox_metadata(record: Dict[str, Any], fields: Dict[str, Any], existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    previous: Dict[str, Any] = {}
    if existing:
        try:
            previous_raw = json.loads(str(existing.get("raw_metadata_json") or "{}"))
            if isinstance(previous_raw, dict):
                previous = previous_raw
        except Exception:
            previous = {}
    metadata = {
        "mobile_inbox_record_id": str(record.get("record_id") or previous.get("mobile_inbox_record_id") or ""),
        "mobile_note": as_text(fields.get("手机备注")) or str(previous.get("mobile_note") or ""),
        "mobile_submitted_at": as_text(fields.get("提交时间")) or str(previous.get("mobile_submitted_at") or ""),
        "mobile_remote_modified_at": str(
            record.get("last_modified_time") or previous.get("mobile_remote_modified_at") or ""
        ),
        "source": as_text(fields.get("来源")) or str(previous.get("source") or MOBILE_INBOX_DEFAULT_SOURCE),
    }
    return metadata


def mobile_inbox_existing_item(
    conn: sqlite3.Connection,
    table_id: str,
    source_url: str,
    record_id: str,
) -> Optional[Dict[str, Any]]:
    record_match, url_match = mobile_inbox_existing_items(conn, table_id, source_url, record_id)
    return record_match or url_match


def mobile_inbox_existing_items(
    conn: sqlite3.Connection,
    table_id: str,
    source_url: str,
    record_id: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    record_match: Optional[Dict[str, Any]] = None
    if record_id:
        rows = conn.execute("SELECT * FROM collected_items WHERE table_id = ?", (table_id,)).fetchall()
        for row in rows:
            item = desktop_row_to_dict(row)
            try:
                meta = json.loads(str(item.get("raw_metadata_json") or "{}"))
            except Exception:
                meta = {}
            if isinstance(meta, dict) and str(meta.get("mobile_inbox_record_id") or "") == record_id:
                record_match = item
                break
    url_row = conn.execute(
        "SELECT * FROM collected_items WHERE table_id = ? AND source_url = ?",
        (table_id, source_url),
    ).fetchone()
    url_match = desktop_row_to_dict(url_row) if url_row else None
    return record_match, url_match


def mobile_inbox_metadata_from_item(item: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not item:
        return {}
    try:
        metadata = json.loads(str(item.get("raw_metadata_json") or "{}"))
    except Exception:
        return {}
    return metadata if isinstance(metadata, dict) else {}


def mobile_inbox_put_assignment(assignments: List[str], values: List[Any], field: str, value: Any) -> None:
    prefix = f"{field} = "
    for index, assignment in enumerate(assignments):
        if assignment.startswith(prefix):
            values[index] = value
            return
    assignments.append(f"{field} = ?")
    values.append(value)


def mobile_inbox_merge_local_fields(target: Dict[str, Any], source: Dict[str, Any]) -> Tuple[List[str], List[Any]]:
    assignments: List[str] = []
    values: List[Any] = []
    for field in (
        "title",
        "caption",
        "cover_url",
        "duration",
        "published_at",
        "video_path",
        "max_daily_card",
        "daily_date",
        "max_feedback",
    ):
        if str(source.get(field) or ""):
            mobile_inbox_put_assignment(assignments, values, field, str(source.get(field) or ""))
    for field in ("likes", "comments", "shares"):
        if source.get(field) is not None:
            mobile_inbox_put_assignment(assignments, values, field, source.get(field))
    if int(source.get("daily_selected") or 0):
        mobile_inbox_put_assignment(assignments, values, "daily_selected", int(source.get("daily_selected") or 0))
    if int(source.get("daily_sort") or 0):
        mobile_inbox_put_assignment(assignments, values, "daily_sort", int(source.get("daily_sort") or 0))
    source_status = str(source.get("status") or "").strip()
    target_status = str(target.get("status") or "").strip()
    if source_status and mobile_inbox_status_rank(source_status) > mobile_inbox_status_rank(target_status):
        mobile_inbox_put_assignment(assignments, values, "status", source_status)
    if str(source.get("error") or "") and (
        mobile_inbox_status_rank(source_status) >= mobile_inbox_status_rank(target_status)
        or not str(target.get("error") or "")
    ):
        mobile_inbox_put_assignment(assignments, values, "error", str(source.get("error") or ""))
    return assignments, values


def mobile_inbox_migrate_download_tasks(
    conn: sqlite3.Connection,
    survivor_id: str,
    duplicate_id: str,
    ts: str,
) -> None:
    if not survivor_id or not duplicate_id or survivor_id == duplicate_id:
        return
    active_list = DESKTOP_ACTIVE_DOWNLOAD_STATUSES
    placeholders = ",".join("?" for _ in active_list)
    survivor_active = conn.execute(
        f"SELECT id FROM video_download_tasks WHERE item_id = ? AND status IN ({placeholders}) LIMIT 1",
        (survivor_id, *active_list),
    ).fetchone()
    duplicate_active_rows = conn.execute(
        f"SELECT id FROM video_download_tasks WHERE item_id = ? AND status IN ({placeholders})",
        (duplicate_id, *active_list),
    ).fetchall()
    if survivor_active and duplicate_active_rows:
        for row in duplicate_active_rows:
            conn.execute(
                """
                UPDATE video_download_tasks
                SET status = 'cancelled', stage = '已合并到同一素材', completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (ts, ts, row["id"]),
            )
    conn.execute(
        "UPDATE video_download_tasks SET item_id = ?, updated_at = ? WHERE item_id = ?",
        (survivor_id, ts, duplicate_id),
    )


def mobile_inbox_items_have_active_download_tasks(
    conn: sqlite3.Connection,
    *item_ids: str,
) -> bool:
    unique_ids = list(dict.fromkeys(item_id for item_id in item_ids if item_id))
    if not unique_ids:
        return False
    item_placeholders = ",".join("?" for _ in unique_ids)
    status_placeholders = ",".join("?" for _ in DESKTOP_ACTIVE_DOWNLOAD_STATUSES)
    row = conn.execute(
        f"""
        SELECT 1
        FROM video_download_tasks
        WHERE item_id IN ({item_placeholders})
          AND status IN ({status_placeholders})
        LIMIT 1
        """,
        (*unique_ids, *DESKTOP_ACTIVE_DOWNLOAD_STATUSES),
    ).fetchone()
    return row is not None


def mobile_inbox_delete_duplicate_record_item(
    conn: sqlite3.Connection,
    table_id: str,
    duplicate_id: str,
) -> None:
    if duplicate_id:
        conn.execute("DELETE FROM collected_items WHERE table_id = ? AND id = ?", (table_id, duplicate_id))


def mobile_inbox_remote_mtime(metadata: Dict[str, Any]) -> int:
    try:
        return int(str(metadata.get("mobile_remote_modified_at") or "0"))
    except (TypeError, ValueError):
        return 0


def desktop_sync_mobile_inbox_once(db_path: Path, cfg: Dict[str, Any]) -> Dict[str, Any]:
    table = desktop_ensure_mobile_inbox_table(db_path)
    try:
        table_cfg = mobile_inbox_table_config(cfg)
    except ValueError as error:
        return {
            "ok": False,
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "seen": 0,
            "table_id": table["id"],
            "error": str(error),
        }
    names = {**DEFAULT_FIELDS, **(table_cfg.get("fields") or {})}
    records = list_records(table_cfg)
    created = updated = skipped = 0
    ts = now_text()
    with desktop_connect(db_path) as conn:
        for record in records:
            fields = record.get("fields") or {}
            source_url = normalize_url(as_text(fields.get(names["url"])))
            if not source_url:
                skipped += 1
                continue
            record_id = str(record.get("record_id") or "")
            incoming_status = as_text(fields.get(names["status"])) or "待扒取"
            platform = as_text(fields.get(names["platform"])) or detect_platform(source_url) or "未知"
            note = as_text(fields.get("手机备注"))
            record_item, url_item = mobile_inbox_existing_items(conn, str(table["id"]), source_url, record_id)
            existing = record_item or url_item
            merge_target = (
                url_item
                if record_item and url_item and str(record_item.get("id") or "") != str(url_item.get("id") or "")
                else None
            )
            metadata_source = record_item or existing
            metadata = mobile_inbox_metadata(record, fields, metadata_source)
            if existing:
                update_item = merge_target or existing
                old_meta = mobile_inbox_metadata_from_item(metadata_source)
                old_mtime = mobile_inbox_remote_mtime(old_meta)
                incoming_mtime = mobile_inbox_remote_mtime(metadata)
                remote_is_stale = bool(old_mtime and incoming_mtime and incoming_mtime < old_mtime)
                if remote_is_stale and record_item and source_url != str(record_item.get("source_url") or ""):
                    skipped += 1
                    continue
                if remote_is_stale:
                    metadata = {
                        "mobile_inbox_record_id": str(
                            old_meta.get("mobile_inbox_record_id") or metadata.get("mobile_inbox_record_id") or ""
                        ),
                        "mobile_note": str(old_meta.get("mobile_note") or metadata.get("mobile_note") or ""),
                        "mobile_submitted_at": str(
                            old_meta.get("mobile_submitted_at") or metadata.get("mobile_submitted_at") or ""
                        ),
                        "mobile_remote_modified_at": str(
                            old_meta.get("mobile_remote_modified_at") or metadata.get("mobile_remote_modified_at") or ""
                        ),
                        "source": str(old_meta.get("source") or metadata.get("source") or MOBILE_INBOX_DEFAULT_SOURCE),
                    }
                if (
                    merge_target
                    and record_item
                    and mobile_inbox_items_have_active_download_tasks(
                        conn,
                        str(merge_target.get("id") or ""),
                        str(record_item.get("id") or ""),
                    )
                ):
                    skipped += 1
                    continue
                metadata_json = json.dumps(metadata, ensure_ascii=False)
                assignments: List[str] = []
                values: List[Any] = []
                if (
                    record_item
                    and not merge_target
                    and source_url != str(update_item.get("source_url") or "")
                    and not remote_is_stale
                ):
                    assignments.append("source_url = ?")
                    values.append(source_url)
                if metadata_json != str(update_item.get("raw_metadata_json") or "{}"):
                    assignments.append("raw_metadata_json = ?")
                    values.append(metadata_json)
                if platform != "未知" and str(update_item.get("platform") or "") in {"", "未知"} and not remote_is_stale:
                    assignments.append("platform = ?")
                    values.append(platform)
                apply_status = mobile_inbox_should_apply_status(
                    str(update_item.get("status") or ""),
                    incoming_status,
                    old_mtime,
                    incoming_mtime,
                )
                if apply_status:
                    if incoming_status != str(update_item.get("status") or ""):
                        assignments.append("status = ?")
                        values.append(incoming_status)
                    if note and str(update_item.get("error") or "") != note and str(update_item.get("status") or "") == "待扒取":
                        assignments.append("error = ?")
                        values.append(note)
                if merge_target and record_item:
                    merge_assignments, merge_values = mobile_inbox_merge_local_fields(merge_target, record_item)
                    assignments.extend(merge_assignments)
                    values.extend(merge_values)
                if assignments:
                    assignments.append("updated_at = ?")
                    values.extend([ts, update_item["id"]])
                    conn.execute(
                        f"UPDATE collected_items SET {', '.join(assignments)} WHERE id = ?",
                        values,
                    )
                    updated += 1
                if merge_target and record_item:
                    mobile_inbox_migrate_download_tasks(
                        conn,
                        str(update_item.get("id") or ""),
                        str(record_item.get("id") or ""),
                        ts,
                    )
                    mobile_inbox_delete_duplicate_record_item(conn, str(table["id"]), str(record_item.get("id") or ""))
                    updated += 1
                continue
            conn.execute(
                """
                INSERT INTO collected_items(
                    id, table_id, platform, source_url, source_type, title, caption, cover_url,
                    duration, likes, comments, shares, published_at, status, error,
                    raw_metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    table["id"],
                    platform or "未知",
                    source_url,
                    "mobile_inbox",
                    "",
                    "",
                    "",
                    "",
                    None,
                    None,
                    None,
                    "",
                    incoming_status,
                    note,
                    json.dumps(metadata, ensure_ascii=False),
                    ts,
                    ts,
                ),
            )
            created += 1
        if created or updated:
            conn.execute("UPDATE collection_tables SET updated_at = ? WHERE id = ?", (ts, table["id"]))
    return {
        "ok": True,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "seen": len(records),
        "table_id": table["id"],
    }


def desktop_mobile_inbox_worker(db_path: Path, cfg: Dict[str, Any], stop_event: threading.Event) -> None:
    try:
        raw_interval = (cfg.get("mobile_inbox") or {}).get("poll_interval") or 10
        interval = max(5, min(60, int(raw_interval)))
    except (TypeError, ValueError):
        interval = 10
    while not stop_event.is_set():
        try:
            desktop_sync_mobile_inbox_once(db_path, cfg)
        except SystemExit as error:
            print(f"手机收集箱同步失败：{error}", flush=True)
        except Exception as error:
            print(f"手机收集箱同步失败：{error}", flush=True)
        stop_event.wait(interval)


def desktop_start_mobile_inbox_worker(db_path: Path, cfg: Dict[str, Any]) -> None:
    mobile_cfg = cfg.get("mobile_inbox") or {}
    if not mobile_cfg.get("enabled", False):
        return
    try:
        table_cfg = mobile_inbox_table_config(cfg)
    except ValueError:
        return
    key = f"{db_path.resolve()}:{(table_cfg.get('feishu') or {}).get('table_id') or ''}"
    with DESKTOP_MOBILE_INBOX_WORKER_LOCK:
        if key in DESKTOP_MOBILE_INBOX_WORKER_STARTED:
            return
        DESKTOP_MOBILE_INBOX_WORKER_STARTED.add(key)
    stop_event = threading.Event()
    thread = threading.Thread(target=desktop_mobile_inbox_worker, args=(db_path, cfg, stop_event), daemon=True)
    thread.start()


def pmset_power_summary() -> Dict[str, Any]:
    result: Dict[str, Any] = {"source": "", "raw": ""}
    try:
        got = subprocess.run(["pmset", "-g", "ps"], capture_output=True, text=True, timeout=5)
        text = (got.stdout or got.stderr or "").strip()
        result["raw"] = text
        if "AC Power" in text:
            result["source"] = "AC Power"
        elif "Battery Power" in text:
            result["source"] = "Battery Power"
    except Exception as e:
        result["error"] = str(e)
    return result


def launchctl_label_running(label: str) -> bool:
    try:
        result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return result.returncode == 0
    except Exception:
        return False


def desktop_engine_status(db_path: Path, cfg: Dict[str, Any]) -> Dict[str, Any]:
    pending = desktop_queue_pending_items(db_path, limit=1000)
    items = desktop_list_items(db_path)
    processing_count = sum(1 for item in items if item.get("status") == "采集中")
    waiting_login_count = sum(1 for item in items if item.get("status") in (LOGIN_STATUSES | RETRY_LOGIN_STATUSES))
    browser_retry_count = sum(1 for item in items if item.get("status") in BROWSER_RETRY_STATUSES)
    fallback_cfg = browser_fallback_config(cfg)
    cdp_ok = cdp_browser_available(fallback_cfg) if fallback_cfg.get("enabled", True) else False
    power = pmset_power_summary()
    app_label = "com.chen.content-link-collector.desktop-app"
    queue_label = "com.chen.content-link-collector.keep-awake-ac"
    if pending:
        status = "有待采集任务"
    elif waiting_login_count:
        status = "需要登录处理"
    elif browser_retry_count:
        status = "浏览器待恢复"
    else:
        status = "在线可采集"
    return {
        "ok": True,
        "status": status,
        "checked_at": now_text(),
        "pending_count": len(pending),
        "processing_count": processing_count,
        "waiting_login_count": waiting_login_count,
        "browser_retry_count": browser_retry_count,
        "cdp_browser": "运行中" if cdp_ok else "未连接",
        "power_source": power.get("source") or "未知",
        "keep_awake_ac": "运行中" if launchctl_label_running(queue_label) else "未运行",
        "desktop_service": "运行中" if launchctl_label_running(app_label) else "当前进程",
    }


def desktop_save_profile_candidate(
    db_path: Path,
    table_id: str,
    item: Dict[str, Any],
    profile_url: str,
    platform: str = "抖音",
) -> Dict[str, Any]:
    return desktop_save_item(
        db_path,
        table_id,
        {
            "platform": platform or detect_platform(item.get("url") or profile_url) or "未知",
            "source_url": item.get("url") or "",
            "source_type": "profile",
            "title": item.get("title") or "主页发现视频",
            "cover_url": item.get("cover_url") or "",
            "status": "候选",
            "error": "主页扫描候选：先预览筛选，勾选后再采集基础信息或逐字稿。",
            "raw_metadata_json": json.dumps({"profile_url": profile_url}, ensure_ascii=False),
        },
    )


def desktop_mobile_inbox_record_id(item: Dict[str, Any]) -> str:
    metadata = mobile_inbox_metadata_from_item(item)
    return str(metadata.get("mobile_inbox_record_id") or "").strip()


def desktop_write_mobile_inbox_status(
    cfg: Dict[str, Any],
    item: Dict[str, Any],
    status: str,
    error: str = "",
) -> None:
    record_id = desktop_mobile_inbox_record_id(item)
    if not record_id:
        return
    table_cfg = mobile_inbox_table_config(cfg)
    names = table_cfg["fields"]
    update_record(
        table_cfg,
        record_id,
        {
            names["status"]: status,
            names["error"]: error[:1000],
        },
    )


def desktop_collect_selected_profile_items(
    db_path: Path,
    table_id: str,
    item_ids: List[str],
    cfg: Dict[str, Any],
    platform: str = "抖音",
    transcribe: bool = False,
) -> Dict[str, Any]:
    selected = {str(item_id) for item_id in item_ids if str(item_id)}
    if not table_id:
        raise ValueError("缺少 table_id")
    if not selected:
        raise ValueError("请先勾选要采集的候选作品")
    items = [item for item in desktop_list_items(db_path, table_id) if item.get("id") in selected]
    results = []
    safety = youtube_safety_config(cfg)
    is_youtube_batch = platform == "YouTube" or any((item.get("platform") == "YouTube" or is_youtube_target(item.get("source_url") or "")) for item in items)
    throttle_seconds = float(safety.get("throttle_seconds") or 0) if is_youtube_batch and safety.get("enabled", True) else 0.0
    max_network_failures = int(safety.get("max_consecutive_network_failures") or 0) if is_youtube_batch and safety.get("enabled", True) else 0
    consecutive_network_failures = 0
    writeback_errors: List[str] = []

    def write_mobile_status(item: Dict[str, Any], status: str, error: str = "") -> None:
        if not desktop_mobile_inbox_record_id(item):
            return
        try:
            desktop_write_mobile_inbox_status(cfg, item, status, error)
        except (Exception, SystemExit) as write_error:
            message = f"飞书状态回写失败：{write_error}"
            writeback_errors.append(message)
            print(message, flush=True)

    for item in items:
        url = item.get("source_url") or ""
        if not url:
            continue
        if results and throttle_seconds > 0:
            time.sleep(throttle_seconds)
        if desktop_mobile_inbox_record_id(item):
            desktop_update_item(db_path, str(item.get("id") or ""), {"status": "扒取中"})
            write_mobile_status(item, "扒取中")
        item_platform = str(item.get("platform") or "").strip()
        detected_platform = detect_platform(url)
        platform_hint = (
            item_platform
            if item_platform and item_platform != "未知"
            else detected_platform if detected_platform != "未知"
            else platform or "抖音"
        )
        result = desktop_scrape_single_url(
            db_path,
            table_id,
            url,
            cfg,
            platform_hint,
            source_type=item.get("source_type") or "profile",
            transcribe=transcribe,
        )
        results.append(result)
        status = str((results[-1] or {}).get("status") or "")
        if desktop_mobile_inbox_record_id(item):
            write_mobile_status(item, status, str((results[-1] or {}).get("error") or ""))
        if is_youtube_batch and status in YOUTUBE_NETWORK_STATUSES:
            consecutive_network_failures += 1
        else:
            consecutive_network_failures = 0
        if max_network_failures and consecutive_network_failures >= max_network_failures:
            return {
                "processed_count": len(results),
                "items": results,
                "paused": True,
                "message": "；".join(
                    [
                        "YouTube 连续出现 VPN/网络异常，已暂停后续深度采集。请确认 VPN 稳定后再继续。",
                        *dict.fromkeys(writeback_errors),
                    ]
                ),
            }
    return {
        "processed_count": len(results),
        "items": results,
        "paused": False,
        "message": "；".join(dict.fromkeys(writeback_errors)),
    }


def normalize_douyin_video_url_from_href(href: str, base_url: str = "https://www.douyin.com/") -> str:
    href = str(href or "").strip()
    if not href:
        return ""
    absolute = urllib.parse.urljoin(base_url, href)
    aweme_id = douyin_aweme_id(absolute)
    if not aweme_id:
        for pattern in (r"[?&](?:modal_id|aweme_id)=([0-9]{8,})", r"/video/([0-9]{8,})", r"/share/video/([0-9]{8,})"):
            match = re.search(pattern, absolute)
            if match:
                aweme_id = match.group(1)
                break
    if not aweme_id:
        return ""
    return f"https://www.douyin.com/video/{aweme_id}"


def clean_profile_card_text(text: str) -> str:
    lines = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or re.fullmatch(r"[\d.,万wW赞评论分享播放\\s]+", line):
            continue
        if re.search(r"登录|关注|粉丝|获赞|私信|投稿|合集|作品\\s*\\d+", line):
            continue
        lines.append(line)
    return " ".join(lines)[:120]


def douyin_profile_entries_to_links(entries: List[Dict[str, Any]], base_url: str) -> List[Dict[str, str]]:
    seen: set[str] = set()
    results: List[Dict[str, str]] = []
    for entry in entries:
        url = normalize_douyin_video_url_from_href(str(entry.get("href") or ""), base_url)
        if not url or url in seen:
            continue
        seen.add(url)
        results.append({
            "url": url,
            "title": clean_profile_card_text(str(entry.get("text") or "")),
            "cover_url": normalize_resource_url(str(entry.get("cover_url") or ""), base_url),
        })
    return results


def normalize_xiaohongshu_note_url_from_href(href: str, base_url: str = "https://www.xiaohongshu.com/") -> str:
    href = str(href or "").strip()
    if not href:
        return ""
    absolute = urllib.parse.urljoin(base_url, href)
    parsed = urllib.parse.urlparse(absolute)
    if "xiaohongshu.com" not in parsed.netloc and "xhslink.com" not in parsed.netloc:
        return ""
    match = re.search(r"/(?:explore|item|discovery/item)/([A-Za-z0-9]+)", parsed.path)
    if not match:
        return ""
    return f"https://www.xiaohongshu.com/explore/{match.group(1)}"


def xiaohongshu_profile_entries_to_links(entries: List[Dict[str, Any]], base_url: str) -> List[Dict[str, str]]:
    seen: set[str] = set()
    results: List[Dict[str, str]] = []
    for entry in entries:
        url = normalize_xiaohongshu_note_url_from_href(str(entry.get("href") or ""), base_url)
        if not url or url in seen:
            continue
        seen.add(url)
        results.append({
            "url": url,
            "title": clean_profile_card_text(str(entry.get("text") or "")),
            "cover_url": normalize_resource_url(str(entry.get("cover_url") or ""), base_url),
        })
    return results


def normalize_bilibili_video_url_from_href(href: str, base_url: str = "https://www.bilibili.com/") -> str:
    href = str(href or "").strip()
    if not href:
        return ""
    absolute = urllib.parse.urljoin(base_url, href)
    parsed = urllib.parse.urlparse(absolute)
    if not ("bilibili.com" in parsed.netloc or parsed.netloc.endswith("b23.tv") or parsed.netloc.endswith("bili2233.cn")):
        return ""
    bvid = bilibili_bvid_from_url(absolute)
    if bvid:
        return f"https://www.bilibili.com/video/{bvid}"
    aid = bilibili_aid_from_url(absolute)
    if aid:
        return f"https://www.bilibili.com/video/av{aid}"
    return ""


def bilibili_profile_entries_to_links(entries: List[Dict[str, Any]], base_url: str) -> List[Dict[str, str]]:
    seen: set[str] = set()
    results: List[Dict[str, str]] = []
    for entry in entries:
        url = normalize_bilibili_video_url_from_href(str(entry.get("href") or ""), base_url)
        if not url or url in seen:
            continue
        seen.add(url)
        results.append({
            "url": url,
            "title": clean_profile_card_text(str(entry.get("text") or "")),
            "cover_url": normalize_resource_url(str(entry.get("cover_url") or ""), base_url),
        })
    return results


def normalize_youtube_video_url_from_href(href: str, base_url: str = "https://www.youtube.com/") -> str:
    href = str(href or "").strip()
    if not href or href.startswith(("javascript:", "mailto:", "tel:")):
        return ""
    absolute = urllib.parse.urljoin(base_url, href)
    parsed = urllib.parse.urlparse(absolute)
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    if host.endswith("youtu.be"):
        video_id = path.strip("/").split("/")[0]
        return f"https://www.youtube.com/watch?v={video_id}" if video_id else ""
    if "youtube.com" not in host:
        return ""
    query = urllib.parse.parse_qs(parsed.query)
    video_id = (query.get("v") or [""])[0]
    if video_id and path == "/watch":
        return f"https://www.youtube.com/watch?v={video_id}"
    shorts_match = re.match(r"^/shorts/([^/?#]+)$", path)
    if shorts_match:
        return f"https://www.youtube.com/shorts/{shorts_match.group(1)}"
    return ""


def youtube_profile_entries_to_links(entries: List[Dict[str, Any]], base_url: str) -> List[Dict[str, str]]:
    seen: set[str] = set()
    results: List[Dict[str, str]] = []
    for entry in entries:
        url = normalize_youtube_video_url_from_href(str(entry.get("href") or ""), base_url)
        if not url or url in seen:
            continue
        seen.add(url)
        results.append({
            "url": url,
            "title": clean_profile_card_text(str(entry.get("text") or "")),
            "cover_url": normalize_resource_url(str(entry.get("cover_url") or ""), base_url),
        })
    return results


def normalize_instagram_work_url_from_href(href: str, base_url: str = "https://www.instagram.com/") -> str:
    href = str(href or "").strip()
    if not href or href.startswith(("javascript:", "mailto:", "tel:")):
        return ""
    absolute = urllib.parse.urljoin(base_url, href)
    parsed = urllib.parse.urlparse(absolute)
    if "instagram.com" not in parsed.netloc.lower():
        return ""
    match = re.match(r"^/(p|reel|tv)/([^/?#]+)/?", parsed.path)
    if not match:
        return ""
    kind, shortcode = match.groups()
    return f"https://www.instagram.com/{kind}/{shortcode}/"


def instagram_profile_entries_to_links(entries: List[Dict[str, Any]], base_url: str) -> List[Dict[str, str]]:
    seen: set[str] = set()
    results: List[Dict[str, str]] = []
    for entry in entries:
        url = normalize_instagram_work_url_from_href(str(entry.get("href") or ""), base_url)
        if not url or url in seen:
            continue
        seen.add(url)
        results.append({
            "url": url,
            "title": clean_profile_card_text(str(entry.get("text") or "")),
            "cover_url": normalize_resource_url(str(entry.get("cover_url") or ""), base_url),
        })
    return results


def normalize_shipinhao_work_url_from_href(href: str, base_url: str = "https://channels.weixin.qq.com/") -> str:
    href = str(href or "").strip()
    if not href or href.startswith(("javascript:", "mailto:", "tel:")):
        return ""
    absolute = urllib.parse.urljoin(base_url, href)
    parsed = urllib.parse.urlparse(absolute)
    host = parsed.netloc.lower()
    if "weixin.qq.com" not in host and "channels.weixin" not in host:
        return ""
    lower_path = parsed.path.lower()
    lower_query = parsed.query.lower()
    if any(skip in lower_path for skip in ("/profile", "/login", "/help", "/notice", "/creator")):
        return ""
    is_work = (
        "/post" in lower_path
        or "/feed" in lower_path
        or "/detail" in lower_path
        or "feed_id=" in lower_query
        or "exportkey=" in lower_query
    )
    if not is_work:
        return ""
    return urllib.parse.urlunparse((parsed.scheme or "https", parsed.netloc, parsed.path, "", parsed.query, ""))


def shipinhao_profile_entries_to_links(entries: List[Dict[str, Any]], base_url: str) -> List[Dict[str, str]]:
    seen: set[str] = set()
    results: List[Dict[str, str]] = []
    for entry in entries:
        url = normalize_shipinhao_work_url_from_href(str(entry.get("href") or ""), base_url)
        if not url or url in seen:
            continue
        seen.add(url)
        results.append({
            "url": url,
            "title": clean_profile_card_text(str(entry.get("text") or "")) or "视频号主页发现作品",
            "cover_url": normalize_resource_url(str(entry.get("cover_url") or ""), base_url),
        })
    return results


def profile_entries_to_links(platform: str, entries: List[Dict[str, Any]], base_url: str) -> List[Dict[str, str]]:
    if platform == "小红书":
        return xiaohongshu_profile_entries_to_links(entries, base_url)
    if platform == "B站":
        return bilibili_profile_entries_to_links(entries, base_url)
    if platform == "YouTube":
        return youtube_profile_entries_to_links(entries, base_url)
    if platform == "Instagram":
        return instagram_profile_entries_to_links(entries, base_url)
    if platform == "视频号":
        return shipinhao_profile_entries_to_links(entries, base_url)
    return douyin_profile_entries_to_links(entries, base_url)


def desktop_profile_status(session_id: str = "") -> Dict[str, Any]:
    with DESKTOP_PROFILE_SESSIONS_LOCK:
        if session_id:
            session = DESKTOP_PROFILE_SESSIONS.get(session_id)
        else:
            session = next(reversed(DESKTOP_PROFILE_SESSIONS.values()), None) if DESKTOP_PROFILE_SESSIONS else None
        if not session:
            return {"ok": False, "status": "未启动", "message": "还没有主页采集会话。"}
        public_keys = {
            "session_id",
            "table_id",
            "platform",
            "profile_url",
            "status",
            "message",
            "found_count",
            "saved_count",
            "completed_count",
            "error_count",
            "last_error",
            "started_at",
            "updated_at",
        }
        return {"ok": True, **{key: session.get(key) for key in public_keys}}


def desktop_profile_update(session_id: str, **updates: Any) -> None:
    with DESKTOP_PROFILE_SESSIONS_LOCK:
        session = DESKTOP_PROFILE_SESSIONS.get(session_id)
        if not session:
            return
        session.update(updates)
        session["updated_at"] = now_text()


def desktop_profile_complete_video(
    session_id: str,
    db_path: Path,
    table_id: str,
    url: str,
    cfg: Dict[str, Any],
    platform: str = "抖音",
) -> None:
    with DESKTOP_PROFILE_ENRICH_SEMAPHORE:
        try:
            result: Dict[str, Any] = {}
            for attempt in range(1, 4):
                desktop_profile_update(session_id, status="补全中", message=f"正在补全：{url}")
                result = desktop_scrape_single_url(
                    db_path,
                    table_id,
                    url,
                    cfg,
                    platform,
                    source_type="profile",
                    transcribe=False,
                )
                status = result.get("status") or ""
                has_core_data = bool(result.get("title") and result.get("cover_url") and result.get("duration"))
                if status in {"基础信息成功", "成功", "图文作品"} and has_core_data:
                    break
                if attempt < 3 and status in ({"等待登录", "待人工确认", "网络异常"} | BROWSER_RETRY_STATUSES):
                    time.sleep(2 * attempt)
                    continue
                break
            with DESKTOP_PROFILE_SESSIONS_LOCK:
                session = DESKTOP_PROFILE_SESSIONS.get(session_id)
                if session:
                    status = result.get("status") or ""
                    has_core_data = bool(result.get("title") and result.get("cover_url") and result.get("duration"))
                    if status in {"基础信息成功", "成功", "图文作品"} and has_core_data:
                        session["completed_count"] = int(session.get("completed_count") or 0) + 1
                    else:
                        session["error_count"] = int(session.get("error_count") or 0) + 1
                        session["last_error"] = f"{url}：{status or '未补全'}"
                    session["status"] = "正在监听"
                    session["message"] = (
                        f"已发现 {session.get('found_count', 0)} 条，"
                        f"已补全 {session.get('completed_count', 0)} 条，"
                        f"待处理 {session.get('error_count', 0)} 条。继续滚动主页可发现更多。"
                    )
                    session["updated_at"] = now_text()
        except Exception as e:
            with DESKTOP_PROFILE_SESSIONS_LOCK:
                session = DESKTOP_PROFILE_SESSIONS.get(session_id)
                if session:
                    session["error_count"] = int(session.get("error_count") or 0) + 1
                    session["last_error"] = str(e)[:500]
                    session["status"] = "正在监听"
                    session["message"] = f"有作品补全失败：{str(e)[:120]}"
                    session["updated_at"] = now_text()


def desktop_profile_worker(
    session_id: str,
    db_path: Path,
    table_id: str,
    profile_url: str,
    cfg: Dict[str, Any],
    platform: str = "抖音",
) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        desktop_profile_update(session_id, status="失败", last_error="真实浏览器模式缺少 Playwright；请先安装 playwright。")
        return

    fallback_cfg = browser_fallback_config(cfg)
    page = None
    browser = None
    try:
        desktop_profile_update(session_id, status="启动中", message=f"正在打开{platform}主页专用浏览器。")
        launch_cdp_browser(fallback_cfg, profile_url)
        with sync_playwright() as p:
            browser = connect_cdp_browser_with_recovery(p, fallback_cfg, platform)
            context = browser.contexts[0] if browser.contexts else browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()
            page.goto(profile_url, wait_until="domcontentloaded", timeout=max(15000, fallback_cfg["timeout"] * 1000))
            desktop_profile_update(
                session_id,
                status="正在监听",
                message=f"浏览器已打开。请在弹出的{platform}主页里登录并手动下滑，软件会采集已加载的作品。",
            )
            while True:
                with DESKTOP_PROFILE_SESSIONS_LOCK:
                    session = DESKTOP_PROFILE_SESSIONS.get(session_id)
                    stop_event = session.get("stop_event") if session else None
                    seen = session.setdefault("seen", set()) if session else set()
                if stop_event and stop_event.is_set():
                    break
                entries = page.evaluate(
                    """() => Array.from(document.querySelectorAll('a[href]')).map(a => {
                        const img = a.querySelector('img');
                        return {
                          href: a.href || a.getAttribute('href') || '',
                          text: (a.innerText || a.getAttribute('aria-label') || a.title || '').slice(0, 500),
                          cover_url: img ? (img.currentSrc || img.src || '') : ''
                        };
                    })"""
                )
                links = profile_entries_to_links(platform, entries if isinstance(entries, list) else [], page.url or profile_url)
                new_links = [item for item in links if item["url"] not in seen]
                for item in new_links:
                    with DESKTOP_PROFILE_SESSIONS_LOCK:
                        session = DESKTOP_PROFILE_SESSIONS.get(session_id)
                        if not session:
                            continue
                        session.setdefault("seen", set()).add(item["url"])
                        session["found_count"] = int(session.get("found_count") or 0) + 1
                        session["saved_count"] = int(session.get("saved_count") or 0) + 1
                        session["status"] = "正在监听"
                        session["message"] = f"已发现 {session['found_count']} 条候选。继续滚动主页可发现更多，勾选后再采集。"
                        session["updated_at"] = now_text()
                    desktop_save_profile_candidate(db_path, table_id, item, profile_url, platform)
                if not new_links:
                    desktop_profile_update(
                        session_id,
                        status="正在监听",
                        message=f"正在监听。已发现 {desktop_profile_status(session_id).get('found_count') or 0} 条候选；继续下滑主页可加载更多作品。",
                    )
                page.wait_for_timeout(3000)
    except Exception as e:
        desktop_profile_update(session_id, status="失败", last_error=str(e)[:800], message=f"主页采集失败：{str(e)[:160]}")
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
        current = desktop_profile_status(session_id)
        if current.get("status") != "失败":
            desktop_profile_update(session_id, status="已停止", message="主页扫描已停止。候选作品会继续保留在表格里，可勾选后采集。")


def desktop_start_profile_session(
    db_path: Path,
    table_id: str,
    profile_url: str,
    cfg: Dict[str, Any],
    platform: str = "抖音",
) -> Dict[str, Any]:
    normalized = normalize_url(profile_url)
    platform = platform or detect_platform(normalized or profile_url) or "抖音"
    if not table_id:
        raise ValueError("缺少 table_id")
    if platform not in {"抖音", "小红书", "B站", "视频号", "YouTube", "Instagram"}:
        raise ValueError("主页批量采集当前支持抖音、小红书、B站、视频号、YouTube 和 Instagram")
    detected = detect_platform(normalized or profile_url)
    if detected and detected != platform:
        raise ValueError(f"当前选择的是{platform}，请粘贴对应平台的主页链接")
    if not normalized:
        raise ValueError(f"请粘贴有效的{platform}主页链接")
    session_id = str(uuid.uuid4())
    stop_event = threading.Event()
    session = {
        "session_id": session_id,
        "table_id": table_id,
        "platform": platform,
        "profile_url": normalized,
        "status": "启动中",
        "message": "正在准备主页监听。",
        "found_count": 0,
        "saved_count": 0,
        "completed_count": 0,
        "error_count": 0,
        "last_error": "",
        "started_at": now_text(),
        "updated_at": now_text(),
        "stop_event": stop_event,
        "seen": set(),
    }
    with DESKTOP_PROFILE_SESSIONS_LOCK:
        DESKTOP_PROFILE_SESSIONS[session_id] = session
    thread = threading.Thread(
        target=desktop_profile_worker,
        args=(session_id, db_path, table_id, normalized, cfg, platform),
        daemon=True,
    )
    session["thread"] = thread
    thread.start()
    return desktop_profile_status(session_id)


def desktop_stop_profile_session(session_id: str) -> Dict[str, Any]:
    with DESKTOP_PROFILE_SESSIONS_LOCK:
        session = DESKTOP_PROFILE_SESSIONS.get(session_id)
        if not session:
            return {"ok": False, "status": "未启动", "message": "没有找到这个主页采集会话。"}
        stop_event = session.get("stop_event")
        if stop_event:
            stop_event.set()
        session["status"] = "正在停止"
        session["message"] = "已请求停止主页监听。"
        session["updated_at"] = now_text()
    return desktop_profile_status(session_id)


DESKTOP_APP_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-title" content="CHEN采集">
  <meta name="theme-color" content="#081120">
  <title>CHEN 内容采集助手</title>
  <style>
    :root{color-scheme:dark;--bg:#081120;--panel:#101d31;--panel2:#17263c;--line:rgba(255,255,255,.11);--text:#eef5ff;--muted:#9aa9bf;--orange:#ff982f;--orange2:#f06118;--green:#4fe083;--blue:#45a9ff;--pink:#ff4e9a}
    *{box-sizing:border-box}body{margin:0;min-height:100vh;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",Inter,sans-serif;background:radial-gradient(circle at 76% 15%,rgba(255,152,47,.22),transparent 22rem),linear-gradient(145deg,#081120,#101a2d 58%,#091323);color:var(--text);padding:24px}.window{max-width:1380px;min-height:850px;margin:auto;border:1px solid rgba(255,255,255,.08);border-radius:18px;overflow:hidden;background:rgba(8,15,28,.9);box-shadow:0 26px 90px rgba(0,0,0,.45)}.home-page.hidden{display:none!important}.hero{display:grid;grid-template-columns:1fr 380px;gap:30px;padding:34px 48px 26px;align-items:center}.hero h1{margin:0 0 14px;font-size:54px;line-height:1.06;letter-spacing:0}.hero h1 span{color:var(--orange)}.hero p{margin:0 0 24px;max-width:720px;color:var(--muted);font-size:18px;line-height:1.75}.btn{border:0;border-radius:10px;min-height:42px;padding:0 19px;background:#26344b;color:#e9f2ff;font-weight:900;cursor:pointer}.btn.primary{background:linear-gradient(135deg,#ffd36b,var(--orange));color:#172033}.btn.danger{background:#493044;color:#ffdce7}.actions{display:flex;gap:14px;align-items:center;flex-wrap:wrap}.daily-hero-btn{min-height:50px;padding:0 22px;border-radius:14px;background:linear-gradient(135deg,#ffec9c,#ff982f 58%,#ff6f61)!important;color:#151b28!important;box-shadow:0 16px 34px rgba(255,128,47,.32),inset 0 1px 0 rgba(255,255,255,.45);transform:translateZ(0);transition:transform .22s cubic-bezier(.18,.89,.32,1.28),box-shadow .22s ease,filter .22s ease}.daily-hero-btn:hover{transform:translateY(-2px) scale(1.018);filter:brightness(1.04);box-shadow:0 22px 42px rgba(255,128,47,.42),inset 0 1px 0 rgba(255,255,255,.5)}.daily-hero-btn:active{transform:translateY(1px) scale(.982);box-shadow:0 8px 18px rgba(255,128,47,.28),inset 0 2px 8px rgba(116,51,0,.18)}.orange-stage{height:300px;display:grid;place-items:center;position:relative}.orange-buddy{width:210px;height:210px;border-radius:50%;background:radial-gradient(circle at 35% 28%,#ffe7bd 0 9%,transparent 10%),radial-gradient(circle at 65% 69%,rgba(255,255,255,.11) 0 2.2%,transparent 3%),linear-gradient(145deg,#ffbd49,var(--orange) 54%,#ed5b1a);box-shadow:inset -16px -20px 38px rgba(138,55,10,.22),inset 9px 10px 24px rgba(255,255,255,.17),0 0 62px rgba(255,152,47,.42);position:relative;animation:float 3.8s ease-in-out infinite;transform-origin:50% 78%}.orange-buddy:before{content:"";position:absolute;width:74px;height:40px;top:-23px;left:100px;border-radius:100% 0 100% 0;background:linear-gradient(135deg,#8af0a3,#31b660);transform:rotate(-18deg)}.face{position:absolute;inset:72px 43px auto;height:78px;transition:.18s}.eye{position:absolute;top:6px;width:40px;height:40px;border-radius:50%;background:#fffdf4;animation:blink 5.2s infinite}.eye.left{left:0}.eye.right{right:0}.eye:before{content:"";position:absolute;inset:7px;border-radius:50%;background:#111725}.eye:after{content:"";position:absolute;width:11px;height:11px;border-radius:50%;background:#fff;top:10px;left:10px}.cheek{position:absolute;top:48px;width:30px;height:13px;border-radius:50%;background:rgba(255,122,154,.42)}.cheek.left{left:2px}.cheek.right{right:2px}.mouth{position:absolute;left:50%;top:43px;width:34px;height:23px;transform:translateX(-50%);border-bottom:5px solid #101725;border-radius:0 0 42px 42px}.juice{position:absolute;left:50%;top:210px;width:42px;height:0;transform:translateX(-50%);border-radius:999px;background:linear-gradient(#ffd56e,#ff8c2f);opacity:0}.glass{position:absolute;left:50%;top:252px;width:88px;height:58px;transform:translateX(-50%);border:2px solid rgba(255,255,255,.25);border-top:0;border-radius:0 0 18px 18px;overflow:hidden;opacity:.62}.glass:before{content:"";position:absolute;left:0;right:0;bottom:0;height:18px;background:linear-gradient(#ffd56e,#ff8c2f);opacity:.72}.orange-stage:hover .orange-buddy{animation:squeeze 1.45s ease-in-out infinite;filter:brightness(1.08)}.orange-stage:hover .face{transform:scaleY(.84) translateY(8px)}.orange-stage:hover .juice{animation:pour 1.45s ease-in-out infinite}@keyframes float{50%{transform:translateY(-10px)}}@keyframes squeeze{0%,100%{transform:scale(1)}38%{transform:translateY(18px) scale(1.16,.76)}58%{transform:translateY(-5px) scale(.92,1.12)}}@keyframes pour{0%,24%,100%{height:0;opacity:0}34%{height:58px;opacity:1}72%{height:44px;opacity:.85}}@keyframes blink{0%,92%,100%{transform:scaleY(1)}95%{transform:scaleY(.12)}97%{transform:scaleY(1)}}
    .platforms{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;padding:0 48px 26px}.platform{background:rgba(17,29,49,.9);border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:16px;min-height:220px;transition:.18s;cursor:pointer;display:flex;flex-direction:column}.platform:hover{transform:translateY(-2px);border-color:rgba(255,255,255,.18);background:rgba(20,34,55,.95)}.platform.selected{transform:translateY(-2px);border-color:rgba(255,152,47,.65);box-shadow:0 0 0 1px rgba(255,152,47,.22),0 0 38px rgba(255,152,47,.26);background:linear-gradient(180deg,rgba(66,44,19,.88),rgba(17,29,49,.9))}.platform-head{display:flex;align-items:center;gap:10px;font-size:18px;font-weight:900;padding-bottom:12px;border-bottom:1px dashed rgba(255,255,255,.18)}.appicon{width:36px;height:36px;border-radius:10px;display:grid;place-items:center;font-weight:950;position:relative;overflow:hidden}.dy{background:linear-gradient(145deg,#27101f,#07070d)}.dy:before{content:"♪";font-size:31px;color:#fff;text-shadow:-3px 0 #20e2ee,3px 2px #ff2c7d}.xhs{background:#ff2442}.xhs:before{content:"小红书";font-size:9px;color:#fff}.bz{background:#e8669a}.bz:before{content:"bilibili";font-size:9px;color:#fff}.shipin{background:#ff9d32}.shipin:before{content:"∞";font-size:30px;color:#fff}.yt{background:#ff0033}.yt:before{content:"▶";font-size:22px;color:#fff}.ig{background:linear-gradient(135deg,#833ab4,#fd1d1d 50%,#fcb045)}.ig:before{content:"◎";font-size:27px;color:#fff}.platform ul{margin:14px 0 14px;padding:0;list-style:none;color:#d8e6f8;font-weight:800;line-height:1.9}.platform li:before{content:"✓";color:var(--green);margin-right:8px}.platform li.pending:before{content:"•";color:#ffd36b}.platform-actions{display:flex;gap:12px;margin-top:auto}.platform-actions .btn{flex:1}.page.hidden{display:none!important}
    .home-tables{display:grid;grid-template-columns:390px 1fr;gap:16px;padding:0 48px 28px}.app-page{padding:30px 42px;border-top:0;min-height:850px}.workspace-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}.workspace-title{display:flex;align-items:center;gap:12px;font-size:22px;font-weight:950}.workspace{display:grid;grid-template-columns:420px 1fr;gap:16px}.panel{background:rgba(17,29,49,.92);border:1px solid rgba(255,255,255,.09);border-radius:12px;padding:16px;min-width:0}.panel h2{margin:0 0 14px;font-size:18px}.table-list{display:grid;gap:8px}.table-row{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:center;border:1px solid rgba(255,255,255,.08);border-radius:10px;background:#1b2a41;padding:10px 10px 10px 12px;font-weight:900;cursor:pointer}.table-row:hover{border-color:rgba(255,255,255,.2);background:#23344f}.table-row.selected{border-color:var(--orange);background:linear-gradient(90deg,rgba(255,152,47,.22),#22334d)}.table-main{min-width:0}.table-name{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.table-meta{display:block;margin-top:4px;color:#9eb0c8;font-size:12px;font-weight:800}.table-actions{display:flex;gap:6px}.table-edit{min-height:30px;padding:0 10px;border-radius:8px;background:rgba(255,255,255,.08);font-size:12px}.table-row.editing{grid-template-columns:1fr;cursor:default}.rename-form{display:flex;gap:8px}.rename-form input{min-width:0;flex:1}.confirm-strip{border:1px solid rgba(255,211,107,.4);background:rgba(255,152,47,.12);border-radius:10px;padding:10px;margin-top:8px;color:#ffe5bc;line-height:1.5}.confirm-actions{display:flex;gap:8px;margin-top:8px}.new-table{display:flex;gap:8px;margin:12px 0}.new-table input,.rename-form input,.control textarea{width:100%;border:1px solid rgba(255,255,255,.12);border-radius:10px;background:#0b1424;color:#edf6ff;padding:12px;font:inherit}.control textarea{min-height:230px;resize:vertical;-webkit-user-select:text;user-select:text}.youtube-tools{display:none;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px;padding:10px;border:1px solid rgba(255,255,255,.08);border-radius:10px;background:rgba(11,20,36,.72)}.youtube-tools.open{display:flex}.youtube-tools .btn{min-height:34px;padding:0 12px;border-radius:8px;font-size:12px}.tabs,.views{display:flex;gap:8px;margin-bottom:12px}.table-tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.table-tools .btn{width:96px;min-height:40px;padding:0}.candidate-tools{display:flex;gap:6px;align-items:center;flex-wrap:wrap}.candidate-tools .btn{width:auto;min-width:74px;min-height:34px;padding:0 10px;font-size:12px}.tab,.view{background:#26344b}.tab.active,.view.active{background:linear-gradient(135deg,#ffd36b,var(--orange));color:#172033}.mode-note{border:1px solid rgba(69,169,255,.22);background:rgba(69,169,255,.08);border-radius:10px;padding:10px;color:#b9d8fb;line-height:1.55;margin-bottom:12px}.status{min-height:22px;color:#aac0da;margin-top:12px;line-height:1.5}.status.error{color:#ffb7c8}.status.ok{color:#aef3c7}.status.compact{font-size:12px;min-height:18px}.engine-card{margin:0 0 18px;border:1px solid rgba(255,255,255,.1);border-radius:12px;background:rgba(11,20,36,.78);padding:12px;max-width:560px}.engine-main{display:flex;align-items:center;gap:8px}.engine-dot{width:10px;height:10px;border-radius:50%;background:#9aa9bf;box-shadow:0 0 0 4px rgba(255,255,255,.05)}.engine-card.ok .engine-dot,.engine-mini.ok:before{background:var(--green)}.engine-card.warn .engine-dot,.engine-mini.warn:before{background:#ffd36b}.engine-card.error .engine-dot,.engine-mini.error:before{background:#ff6b8f}.engine-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:10px;color:#aabbd2;font-size:12px;font-weight:850}.engine-mini{display:flex;align-items:center;gap:8px;border:1px solid rgba(255,255,255,.08);border-radius:10px;background:#0b1424;color:#b9c8dd;padding:9px 10px;margin-bottom:12px;font-size:12px;font-weight:900}.engine-mini:before{content:"";width:8px;height:8px;border-radius:50%;background:#9aa9bf}.help{margin-top:18px;color:#9eb0c8;line-height:1.7}.guide{display:grid;gap:12px;color:#dce8f7}.guide-kicker{color:#9eb0c8;margin:0}.guide-section{border:1px solid rgba(255,255,255,.08);border-radius:10px;background:#0b1424;padding:12px}.guide-section h3{margin:0 0 8px;font-size:15px;color:#fff}.guide-section ol,.guide-section ul{margin:0;padding-left:20px}.guide-section li{margin:4px 0}.guide-badge{display:inline-flex;border-radius:999px;background:rgba(255,152,47,.16);color:#ffd9a8;padding:2px 8px;font-size:12px;font-weight:950}.results-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;gap:12px}.table-scroll{overflow:auto;border:1px solid rgba(255,255,255,.09);border-radius:10px;max-height:560px;min-height:220px;resize:vertical;background:#0b1424}.result-table{border-collapse:collapse;font-size:13px;min-width:1600px;width:max-content;table-layout:fixed;-webkit-user-select:text;user-select:text}.result-table th,.result-table td{border:1px solid rgba(255,255,255,.08);padding:8px 9px;text-align:left;vertical-align:top}.result-table th{color:#aac0da;background:#203048;position:sticky;top:0;z-index:1;height:38px;white-space:nowrap;overflow:hidden}.result-table tbody tr{height:96px}.result-table tbody tr:hover{background:rgba(255,255,255,.035)}.result-table tbody tr.row-selected{background:rgba(255,152,47,.1);box-shadow:inset 3px 0 0 var(--orange)}.th-inner{display:flex;align-items:center;justify-content:space-between;gap:8px}.col-resizer{display:block;width:8px;align-self:stretch;cursor:col-resize;border-radius:99px;opacity:.55}.col-resizer:hover{background:rgba(255,152,47,.42);opacity:1}.candidate-check{width:18px;height:18px;accent-color:var(--orange);cursor:pointer}.editable-cell{min-width:0;max-height:74px;outline:0;border-radius:6px;white-space:pre-wrap;overflow:auto;line-height:1.42;cursor:text}.editable-cell.cell-short{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.editable-cell.cell-long{max-height:74px}.editable-cell:hover{background:rgba(255,255,255,.055)}.editable-cell:focus{background:#0b1424;box-shadow:0 0 0 2px rgba(255,152,47,.55);padding:4px;max-height:240px;resize:vertical;overflow:auto}.editable-cell.save-error{box-shadow:0 0 0 2px rgba(255,80,120,.65)}.copy-btn{min-height:26px;padding:0 8px;border-radius:7px;background:rgba(255,255,255,.08);font-size:12px;margin-top:6px}.cover-btn{display:block;border:0;padding:0;background:transparent;cursor:zoom-in}.cover{width:48px;height:64px;object-fit:cover;border-radius:6px;background:#0b1424}.cover:hover{box-shadow:0 0 0 2px rgba(255,152,47,.75)}.source-link{max-height:38px;overflow:hidden;color:#9ed1ff;line-height:1.35;word-break:break-all}.status-pill{display:inline-flex;border-radius:999px;padding:3px 8px;background:rgba(255,255,255,.08);font-size:12px;font-weight:900}.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}.card{border:1px solid rgba(255,255,255,.09);border-radius:10px;padding:12px;background:#142238}.card h3{margin:8px 0;font-size:15px}.muted{color:var(--muted)}.detail{white-space:pre-wrap;line-height:1.65;background:#0b1424;border-radius:10px;padding:14px;color:#dce8f7;max-height:420px;overflow:auto;-webkit-user-select:text;user-select:text}.modal{position:fixed;inset:0;display:none;place-items:center;background:rgba(2,7,14,.76);z-index:99;padding:18px}.modal.open{display:grid}.modal-card{width:min(760px,92vw);max-height:86vh;border:1px solid rgba(255,255,255,.14);border-radius:12px;background:#101d31;box-shadow:0 20px 58px rgba(0,0,0,.5);overflow:hidden}.modal-card.compact{width:min(520px,92vw)}.modal-card.help-modal{width:min(620px,92vw)}.help-modal .modal-body{max-height:70vh;overflow:auto}.modal-head{display:flex;justify-content:space-between;gap:10px;align-items:center;padding:10px 12px;border-bottom:1px solid rgba(255,255,255,.1)}.modal-head strong{font-size:16px}.modal-actions{display:flex;gap:6px;align-items:center}.modal-actions .btn{min-height:34px;padding:0 12px;border-radius:9px;font-size:13px}.modal-body{padding:14px;display:grid;gap:12px}.export-row{display:grid;grid-template-columns:92px 1fr;gap:10px;align-items:center}.export-row select{border:1px solid rgba(255,255,255,.12);border-radius:9px;background:#0b1424;color:#edf6ff;padding:10px;font:inherit}.export-note{border-radius:10px;background:#0b1424;color:#9ed1ff;padding:10px;min-height:42px;line-height:1.45;word-break:break-all}.modal-img-wrap{display:grid;place-items:center;max-height:62vh;padding:14px;background:#081120}.modal-img-wrap img{max-width:100%;max-height:58vh;border-radius:9px;object-fit:contain}.modal-url{display:flex;justify-content:space-between;gap:10px;align-items:center;padding:9px 12px;color:#9ed1ff;background:#142238;font-size:12px;-webkit-user-select:text;user-select:text}.modal-url span{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.mini-btn{min-height:28px;padding:0 8px;border-radius:7px;background:rgba(255,255,255,.08);font-size:12px;white-space:nowrap}.view-tools{display:flex;gap:6px;align-items:center;flex-wrap:wrap}.table-tool{min-width:72px!important;width:auto!important}.selected-count{display:inline-flex;align-items:center;min-height:30px;border-radius:999px;padding:0 10px;background:#0b1424;color:#ffdcae;font-size:12px;font-weight:950}.table-toolbar-panel{display:none;flex-basis:100%;width:100%;border:1px solid rgba(255,255,255,.09);border-radius:10px;background:#0b1424;padding:10px;margin-top:2px}.table-toolbar-panel.open{display:block}.toolbar-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:8px}.toolbar-field{display:flex;align-items:center;gap:8px;border:1px solid rgba(255,255,255,.08);border-radius:8px;background:#142238;padding:8px 10px;font-size:12px;font-weight:900}.toolbar-field input{accent-color:var(--orange)}.toolbar-form{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;align-items:center}.toolbar-form input,.toolbar-form select{width:100%;border:1px solid rgba(255,255,255,.12);border-radius:8px;background:#101d31;color:#edf6ff;padding:9px;font:inherit;font-size:13px}.density-actions{display:flex;gap:8px;flex-wrap:wrap}.result-table.density-compact tbody tr{height:58px}.result-table.density-compact .editable-cell{max-height:42px}.result-table.density-relaxed tbody tr{height:132px}.result-table.density-relaxed .editable-cell{max-height:108px}.sort-mark{color:#ffd36b;font-size:11px;margin-left:4px}@media(max-width:1050px){.hero,.workspace,.home-tables{grid-template-columns:1fr}.platforms{grid-template-columns:repeat(2,1fr)}}@media(max-width:680px){body{padding:0;background:#081120}.window{border-radius:0;border:0;min-height:100vh}.hero{padding-top:22px;gap:12px}.hero h1{font-size:34px}.hero p{font-size:15px;margin-bottom:14px}.orange-stage{display:none}.platforms{grid-template-columns:1fr}.platform{min-height:150px}.hero,.platforms,.app-page,.home-tables{padding-left:14px;padding-right:14px}.home-tables{gap:12px}.workspace{gap:12px}.workspace-head{position:sticky;top:0;z-index:3;background:#081120;padding:10px 0;margin:-10px 0 12px}.control textarea{min-height:150px}.actions,.tabs{display:grid;grid-template-columns:1fr 1fr}.actions .btn,.tabs .btn{width:100%;padding:0 10px}.engine-grid{grid-template-columns:1fr}.table-shell-head{padding:12px}.table-primary-actions{display:grid;grid-template-columns:1fr 1fr;width:100%}.bitable-toolbar{overflow:auto;flex-wrap:nowrap}.bitable-toolbar .btn{white-space:nowrap}.cards{grid-template-columns:1fr}.modal{padding:10px}.modal-card{width:100%;max-height:92vh}}

    .results-panel{padding:0;overflow:hidden;background:#111d30}.table-shell-head{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:14px 16px 11px;border-bottom:1px solid rgba(255,255,255,.08);background:#121f33}.table-title-block{min-width:190px}.table-title-block h2{margin:0;font-size:19px;line-height:1.2}.table-subline{display:flex;align-items:center;gap:8px;margin-top:6px}.table-subtle{color:#8fa1ba;font-size:12px;font-weight:800}.table-primary-actions{display:flex;gap:7px;align-items:center;justify-content:flex-end;flex-wrap:wrap}.table-primary-actions .btn{min-height:32px;padding:0 11px;border-radius:8px;font-size:12px}.bitable-toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:9px 12px;border-bottom:1px solid rgba(255,255,255,.08);background:#0d1829}.toolbar-group{display:flex;align-items:center;gap:4px;flex-wrap:wrap}.toolbar-divider{width:1px;height:22px;background:rgba(255,255,255,.1);margin:0 2px}.toolbar-spacer{flex:1;min-width:12px}.bitable-toolbar .btn{width:auto;min-width:0;min-height:30px;padding:0 9px;border-radius:7px;background:transparent;color:#b9c8dd;font-size:12px;font-weight:900}.bitable-toolbar .btn:hover{background:rgba(255,255,255,.07);color:#edf6ff}.bitable-toolbar .btn.primary,.bitable-toolbar .view.active,.bitable-toolbar .table-tool.primary{background:rgba(255,152,47,.16);color:#ffd6a8;box-shadow:inset 0 0 0 1px rgba(255,152,47,.22)}.export-tool{background:rgba(255,255,255,.06)!important;color:#e7f0ff!important}.selected-count{min-height:24px;padding:0 8px;background:rgba(255,152,47,.14);color:#ffd6a8}.table-toolbar-panel{margin:8px 0 0;padding:9px;background:#101d31;border-color:rgba(255,255,255,.08)}.table-scroll{border-radius:0;border-left:0;border-right:0;border-bottom:0}.result-table th{background:#1c2d45}.result-table tbody tr.row-selected{background:rgba(255,152,47,.08)}@media(max-width:900px){.table-shell-head{align-items:flex-start;flex-direction:column}.table-primary-actions{justify-content:flex-start}.toolbar-spacer{display:none}}
  </style>
  <style>
    .download-queue-page{padding:28px 34px 40px;min-height:850px;background:linear-gradient(180deg,rgba(15,28,48,.98),rgba(8,17,32,.98))}.download-queue-head{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;margin-bottom:22px}.download-queue-title h1{margin:0;font-size:30px;letter-spacing:-.02em}.download-queue-title p{margin:7px 0 0;color:#91a6c2}.download-summary{display:grid;grid-template-columns:repeat(4,minmax(130px,1fr));gap:10px;margin-bottom:16px}.download-stat{padding:13px 15px;border:1px solid rgba(255,255,255,.08);border-radius:11px;background:rgba(16,29,49,.78)}.download-stat span{display:block;color:#8fa4bf;font-size:12px;font-weight:850}.download-stat strong{display:block;margin-top:5px;font-size:22px}.download-stat.downloading strong{color:#ffb45f}.download-stat.completed strong{color:#75e89e}.download-stat.failed strong{color:#ff7d9e}.download-toolbar{display:flex;align-items:center;gap:7px;flex-wrap:wrap;padding:10px 12px;border:1px solid rgba(255,255,255,.08);border-radius:11px 11px 0 0;background:#101d31}.download-toolbar .btn{min-height:32px;padding:0 11px;border-radius:8px;font-size:12px}.download-table-wrap{overflow:auto;border:1px solid rgba(255,255,255,.08);border-top:0;border-radius:0 0 11px 11px;background:#0b1627}.download-table{width:100%;min-width:900px;border-collapse:collapse;table-layout:fixed}.download-table th{position:sticky;top:0;z-index:1;padding:11px 12px;text-align:left;background:#172841;color:#9fb2cc;font-size:12px;letter-spacing:.02em}.download-table td{padding:12px;border-top:1px solid rgba(255,255,255,.065);vertical-align:middle;color:#dce8f7;font-size:13px}.download-table tbody tr{transition:background .16s}.download-table tbody tr:hover{background:rgba(255,255,255,.035)}.download-title-cell{display:flex;align-items:center;gap:9px;min-width:0}.download-platform-dot{width:8px;height:8px;border-radius:50%;background:var(--orange);box-shadow:0 0 0 4px rgba(255,152,47,.1);flex:0 0 auto}.download-title-text{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:850}.download-status{display:inline-flex;align-items:center;min-height:25px;padding:0 9px;border-radius:999px;font-size:12px;font-weight:950;background:rgba(69,169,255,.12);color:#9ed1ff}.download-status.downloading{background:rgba(255,152,47,.14);color:#ffc27e}.download-status.completed{background:rgba(79,224,131,.13);color:#aef3c7}.download-status.failed{background:rgba(255,78,122,.14);color:#ffb7c8}.download-progress{display:grid;grid-template-columns:minmax(72px,1fr) 40px;gap:8px;align-items:center}.download-progress-track{height:6px;border-radius:999px;background:#263650;overflow:hidden}.download-progress-bar{height:100%;border-radius:inherit;background:linear-gradient(90deg,#ff8426,#ffbd5e);transition:width .35s ease}.download-progress.completed .download-progress-bar{background:linear-gradient(90deg,#2fc66b,#69eba0)}.download-progress.failed .download-progress-bar{background:#ff5a7f}.download-progress-label{text-align:right;color:#9fb2cc;font-variant-numeric:tabular-nums;font-size:12px}.download-row-actions{display:flex;gap:5px;flex-wrap:wrap}.download-row-actions .btn{min-height:28px;padding:0 8px;border-radius:7px;font-size:11px}.download-empty{padding:60px 20px;text-align:center;color:#8fa4bf}.inline-download{display:grid;gap:6px;min-width:112px}.inline-download .btn{margin:0;width:100%;min-height:30px;padding:0 9px;font-size:12px}.inline-download .btn.is-downloading{background:rgba(255,152,47,.16);color:#ffc27e}.inline-download .btn.is-completed{background:rgba(79,224,131,.16);color:#bff8d0}.inline-download .btn.is-failed{background:rgba(255,78,122,.16);color:#ffd0dc}.inline-download .download-progress-track{height:5px}.queue-count-badge{display:inline-flex;align-items:center;justify-content:center;min-width:20px;height:20px;padding:0 6px;margin-left:5px;border-radius:999px;background:rgba(255,152,47,.18);color:#ffd1a0;font-size:11px}@media(max-width:760px){.download-queue-page{padding:18px 12px}.download-queue-head{flex-direction:column}.download-summary{grid-template-columns:1fr 1fr}}
    .setup-page[hidden]{display:none}.setup-page{min-height:850px;padding:36px 48px;background:#0b1627}.setup-head{max-width:820px;margin-bottom:24px}.setup-head h1{margin:0 0 8px;font-size:30px}.setup-head p{margin:0;color:var(--muted);line-height:1.65}.setup-form{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:0;border:1px solid rgba(255,255,255,.1);background:#101d31}.setup-group{padding:22px}.setup-group+ .setup-group{border-left:1px solid rgba(255,255,255,.1)}.setup-group h2{margin:0 0 16px;font-size:18px}.setup-fields{display:grid;gap:12px}.setup-field{display:grid;gap:6px;color:#b7c8dd;font-size:13px;font-weight:850}.setup-field input{width:100%;min-width:0;border:1px solid rgba(255,255,255,.14);border-radius:8px;background:#081322;color:#f1f6ff;padding:11px 12px;font:inherit}.setup-field input:focus{outline:2px solid rgba(255,152,47,.55);border-color:transparent}.setup-progress{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px;margin:18px 0}.setup-check{min-height:52px;padding:10px;border:1px solid rgba(255,255,255,.09);background:#101d31;color:#a9bad0;font-size:12px;font-weight:850}.setup-check.ok{border-color:rgba(79,224,131,.34);color:#aef3c7}.setup-check.warn{border-color:rgba(255,211,107,.34);color:#ffe0a1}.setup-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.setup-actions .btn[disabled]{opacity:.55;cursor:wait}.setup-message{min-height:22px;margin-top:12px;color:#aac0da}.setup-message.error{color:#ffb7c8}.setup-message.ok{color:#aef3c7}@media(max-width:760px){.setup-page{padding:24px 14px}.setup-form{grid-template-columns:1fr}.setup-group+ .setup-group{border-left:0;border-top:1px solid rgba(255,255,255,.1)}.setup-progress{grid-template-columns:1fr 1fr}}
  </style>
</head>
<body>
  <div class="window" id="appWindow">
    <section class="setup-page" id="setupPage" hidden>
      <div class="setup-head"><h1>首次设置</h1><p>配置你自己的飞书工作表；云端日报是可选项。密钥只写入当前 Mac 的钥匙串。</p></div>
      <form id="setupForm" onsubmit="event.preventDefault();saveFirstRunSetup(false)">
        <div class="setup-form">
          <section class="setup-group"><h2>飞书连接</h2><div class="setup-fields">
            <label class="setup-field">App ID<input id="setupFeishuAppId" autocomplete="off"></label>
            <label class="setup-field">App Secret<input id="setupFeishuAppSecret" type="password" autocomplete="new-password"></label>
            <label class="setup-field">Base Token<input id="setupFeishuAppToken" autocomplete="off"></label>
            <label class="setup-field">数据表 ID<input id="setupFeishuTableId" autocomplete="off"></label>
            <label class="setup-field">手机收集箱表 ID（可选）<input id="setupFeishuMobileTableId" autocomplete="off"></label>
          </div></section>
          <section class="setup-group"><h2>云端日报（可选）</h2><div class="setup-fields">
            <label class="setup-field">Worker 地址<input id="setupPublisherWorkerUrl" type="url" autocomplete="off" placeholder="https://your-worker.workers.dev"></label>
            <label class="setup-field">发布设备 Token<input id="setupPublisherToken" type="password" autocomplete="new-password"></label>
            <button class="btn" type="button" onclick="openSettings()">选择视频保存目录</button>
          </div></section>
        </div>
        <div class="setup-progress" id="setupProgress" aria-live="polite"></div>
        <div class="setup-actions"><button class="btn primary" id="setupSubmit" type="submit">验证并继续</button><button class="btn" id="setupLocalOnly" type="button" onclick="saveFirstRunSetup(true)">暂时跳过飞书与云端配置</button></div>
        <div class="setup-message" id="setupMessage" aria-live="polite"></div>
      </form>
    </section>
    <main class="home-page" id="homePage" hidden>
    <section class="hero">
      <div><h1><span>CHEN</span><br>一个会榨取信息的<br>橙子助手</h1><p>选择平台，粘贴作品或主页链接。需要登录时会提示打开平台页面；采集完成后直接在软件里保存、分类和导出。</p><div class="engine-card" id="engineStatusCard"><div class="engine-main"><span class="engine-dot"></span><strong>采集引擎检测中</strong></div><div class="engine-grid"><span>队列 -</span><span>电源 -</span><span>浏览器 -</span></div></div><div class="actions"><button class="btn daily-hero-btn" onclick="openDailyPage()">打开外部情报口喷日报</button><button class="btn primary" onclick="document.querySelector('#platforms').scrollIntoView({behavior:'smooth'})">选择平台</button><button class="btn" onclick="document.querySelector('.home-tables').scrollIntoView({behavior:'smooth'})">历史记忆</button><button class="btn" onclick="openSettings()">软件设置</button><button class="btn" onclick="openDownloadQueue()">下载队列</button><strong class="muted">v0.5</strong></div></div>
      <div class="orange-stage"><div class="orange-buddy"><div class="face"><span class="eye left"></span><span class="eye right"></span><span class="cheek left"></span><span class="cheek right"></span><span class="mouth"></span></div></div><div class="juice"></div><div class="glass"></div></div>
    </section>
    <section class="platforms" id="platforms"></section>
    <section class="home-tables">
      <aside class="panel"><h2>采集表格</h2><div class="table-list" id="tables"></div><div class="new-table"><input id="tableName" placeholder="新建分类表格"><button class="btn primary" onclick="createTable()">新建</button></div><div class="status compact" id="tableStatus"></div><div class="help"><strong>表格说明</strong><br>表格负责分类保存采集结果。先选中一张表，再进入平台采集页；单作品会写成作品行，主页链接会先写成候选行，勾选后再补全。</div></aside>
      <section class="panel results-panel"><div class="table-shell-head"><div class="table-title-block"><h2 id="homeResultTitle">表格预览</h2><div class="table-subline"><span class="selected-count">已选 0</span><span class="table-subtle">当前表格视图</span></div></div><div class="table-primary-actions"><button class="btn" onclick="selectCandidateItems()">全选当前</button><button class="btn" onclick="clearSelectedItems()">清空选择</button><button class="btn primary" onclick="collectSelected(false)">采集选中</button><button class="btn primary" onclick="collectSelected(true)">选中+逐字稿</button></div></div><div class="bitable-toolbar"><div class="toolbar-group view-switch"><button class="btn view active" data-view="table" onclick="setView('table')">表格</button><button class="btn view" data-view="card" onclick="setView('card')">卡片</button><button class="btn view" data-view="detail" onclick="setView('detail')">详情</button></div><span class="toolbar-divider"></span><div class="toolbar-group"><button class="btn table-tool" onclick="toggleTableTool('fields')">字段配置</button><button class="btn table-tool" onclick="toggleTableTool('filter')">筛选</button><button class="btn table-tool" onclick="toggleTableTool('sort')">排序</button><button class="btn table-tool" onclick="toggleTableTool('density')">行高</button><button class="btn table-tool" onclick="resetTableView()">重置视图</button></div><span class="toolbar-spacer"></span><button class="btn export-tool" onclick="openDailyPage()">网页日报</button><button class="btn export-tool" onclick="openExportModal()">导出表格</button><div class="table-toolbar-panel"></div></div><div id="homeResults"></div></section>
    </section>
    </main>
    <section class="app-page page hidden" id="appPage">
      <div class="workspace-head"><div class="workspace-title"><span id="pageIcon" class="appicon dy"></span><span id="pageTitle">抖音扒取</span></div><button class="btn" onclick="showHome()">返回主页</button></div>
      <div class="workspace">
        <main class="panel control"><h2 id="controlTitle">抖音扒取</h2><p class="muted" id="currentTableHint">当前表格：默认采集表</p><div class="engine-mini" id="engineMini">采集引擎检测中</div><div class="tabs"><button class="btn tab active" data-mode="single" onclick="setMode('single')">单作品链接</button><button class="btn tab" data-mode="profile" onclick="setMode('profile')">主页链接</button></div><div class="mode-note" id="modeNote"></div><textarea id="urls" placeholder="把作品链接粘贴到这里，一行一个"></textarea><div class="actions" style="margin-top:12px"><button class="btn primary" id="scrapeButton" onclick="scrape()">开始扒取</button><button class="btn primary" onclick="queueScrape()">加入队列</button><button class="btn" id="profileStop" onclick="stopProfileSession()" style="display:none">停止监听</button><button class="btn" onclick="openLogin()">打开平台浏览器</button><button class="btn" onclick="toggleYouTubeTools()">YouTube修复</button></div><div class="youtube-tools" id="youtubeTools"><button class="btn" onclick="diagnoseYouTube(false)">检查失败原因</button><button class="btn" onclick="diagnoseYouTube(true)">测试能否转写</button><button class="btn" onclick="enableYouTubePoProvider()">增强下载</button></div><div class="status" id="status">准备好了。</div></main>
        <section class="panel results-panel"><div class="table-shell-head"><div class="table-title-block"><h2 id="resultTitle">采集结果</h2><div class="table-subline"><span class="selected-count">已选 0</span><span class="table-subtle">采集结果表</span></div></div><div class="table-primary-actions"><button class="btn" onclick="selectCandidateItems()">全选当前</button><button class="btn" onclick="clearSelectedItems()">清空选择</button><button class="btn primary" onclick="collectSelected(false)">采集选中</button><button class="btn primary" onclick="collectSelected(true)">选中+逐字稿</button></div></div><div class="bitable-toolbar"><div class="toolbar-group view-switch"><button class="btn view active" data-view="table" onclick="setView('table')">表格</button><button class="btn view" data-view="card" onclick="setView('card')">卡片</button><button class="btn view" data-view="detail" onclick="setView('detail')">详情</button></div><span class="toolbar-divider"></span><div class="toolbar-group"><button class="btn table-tool" onclick="toggleTableTool('fields')">字段配置</button><button class="btn table-tool" onclick="toggleTableTool('filter')">筛选</button><button class="btn table-tool" onclick="toggleTableTool('sort')">排序</button><button class="btn table-tool" onclick="toggleTableTool('density')">行高</button><button class="btn table-tool" onclick="resetTableView()">重置视图</button></div><span class="toolbar-spacer"></span><button class="btn export-tool" onclick="openDailyPage()">网页日报</button><button class="btn export-tool" onclick="openExportModal()">导出表格</button><div class="table-toolbar-panel"></div></div><div id="results"></div></section>
      </div>
    </section>
    <section class="download-queue-page page hidden" id="downloadQueuePage">
      <div class="download-queue-head"><div class="download-queue-title"><h1>下载队列</h1><p>集中查看所有平台的视频下载进度与文件状态</p></div><button class="btn" onclick="closeDownloadQueuePage()">返回主页</button></div>
      <div class="download-summary" id="downloadQueueSummary"></div>
      <div class="download-toolbar"><button class="btn primary" onclick="downloadSelectedVideos()">批量下载所选</button><button class="btn" onclick="retryFailedDownloads()">重试失败项</button><button class="btn" onclick="cancelQueuedDownloads()">取消等待项</button><button class="btn" onclick="clearCompletedDownloads()">清除已完成记录</button><button class="btn" onclick="openVideoDirectory('batch')">打开下载文件夹</button><button class="btn" onclick="pollDownloadQueue(true)">刷新</button></div>
      <div class="download-table-wrap" id="downloadQueueTable"></div>
    </section>
  </div>
  <div class="modal" id="coverModal" onclick="closeCover(event)">
    <div class="modal-card" onclick="event.stopPropagation()">
      <div class="modal-head"><strong>封面预览</strong><div class="modal-actions"><button class="btn" onclick="openCoverOriginal()">打开原图</button><button class="btn primary" onclick="saveCoverFile()">保存封面</button><button class="btn" onclick="closeCover()">关闭</button></div></div>
      <div class="modal-img-wrap"><img id="coverLarge" alt="封面预览"></div>
      <div class="modal-url"><span id="coverUrlText"></span><button class="mini-btn" onclick="copyCurrentCoverUrl()">复制链接</button></div>
    </div>
  </div>
  <div class="modal" id="exportModal" onclick="closeExportModal(event)">
    <div class="modal-card compact" onclick="event.stopPropagation()">
      <div class="modal-head"><strong>导出采集表格</strong><div class="modal-actions"><button class="btn" onclick="closeExportModal()">关闭</button></div></div>
      <div class="modal-body">
        <div class="export-row"><span class="muted">当前表格</span><strong id="exportTableName">默认采集表</strong></div>
        <div class="export-row"><span class="muted">导出格式</span><select id="exportFormat"><option value="max-daily">MAX 日报</option><option value="csv">CSV 表格</option><option value="markdown">Markdown 文档</option><option value="json">JSON 数据</option></select></div>
        <div class="export-note" id="exportPath">点击“选择位置并保存”，会弹出系统保存窗口。</div>
        <div class="actions"><button class="btn primary" onclick="saveExportFile()">选择位置并保存</button><button class="btn" onclick="downloadExportFile()">浏览器下载</button></div>
      </div>
    </div>
  </div>
  <div class="modal" id="helpModal" onclick="closeHelpModal(event)">
    <div class="modal-card help-modal" onclick="event.stopPropagation()">
      <div class="modal-head"><strong id="helpModalTitle">操作帮助</strong><div class="modal-actions"><button class="btn" onclick="closeHelpModal()">关闭</button></div></div>
      <div class="modal-body" id="helpModalBody"></div>
    </div>
  </div>
  <div class="modal" id="settingsModal"><div class="modal-card compact"><div class="modal-head"><strong>软件设置</strong><button class="btn" onclick="closeModal('settingsModal')">关闭</button></div><div class="modal-body"><div><strong>单条视频保存位置</strong><div class="export-note" id="singleDirectory"></div><div class="actions"><button class="btn primary" onclick="chooseVideoDirectory('single')">选择文件夹</button><button class="btn" onclick="openVideoDirectory('single')">打开文件夹</button><button class="btn" onclick="resetVideoDirectory('single')">恢复默认</button></div></div><div><strong>批量队列保存位置</strong><div class="export-note" id="batchDirectory"></div><div class="actions"><button class="btn primary" onclick="chooseVideoDirectory('batch')">选择文件夹</button><button class="btn" onclick="openVideoDirectory('batch')">打开文件夹</button><button class="btn" onclick="resetVideoDirectory('batch')">恢复默认</button></div></div></div></div></div>
<script>
const platforms=[['抖音','dy',['单作品链接','主页候选预览','勾选后深度采集'],'支持单条抖音作品链接；主页链接会打开专用浏览器，你手动登录并下滑主页，软件先生成候选列表，勾选后再补全数据。'],['小红书','xhs',['图文 / 视频笔记','小红书主页候选预览','勾选后深度采集'],'支持小红书图文和视频笔记；主页链接会打开专用浏览器，你手动登录并下滑主页，软件先生成候选列表，勾选后再补全数据。'],['B站','bz',['BV / av / 分享链接','B站主页候选预览','勾选后深度采集','长视频 ASR'],'支持 BV、av 和分享链接；UP主页会打开专用浏览器，你手动下滑主页，软件先生成候选列表，勾选后再补全数据或转写。'],['视频号','shipin',['单作品链接','视频号主页候选预览','勾选后深度采集','失败原因提示'],'视频号更依赖微信登录态；主页会打开专用浏览器，你扫码登录并手动下滑，软件先保存候选，勾选后再采集详情或转写。'],['YouTube','yt',['单视频 / Shorts','YouTube频道候选预览','字幕优先 / ASR兜底','勾选后深度采集'],'支持 YouTube 单视频、Shorts 和频道页；频道页先生成候选列表，勾选后再补全信息、字幕或转写。'],['Instagram','ig',['Post / Reel链接','Instagram主页候选预览','登录态采集','勾选后深度采集'],'支持 Instagram Post、Reel 和主页候选预览；需要登录时会打开平台页面，候选勾选后再深度采集。']];
const tablePrefsVersion=3;
const defaultColWidths=[58,74,230,180,86,82,230,92,92,92,142,110,300,220,300,118];
let state={platform:'抖音',mode:'single',view:'table',tableId:'',tables:[],items:[],page:'home',editingTableId:'',deletingTableId:'',tableMessage:'',profileSessionId:'',profilePollTimer:null,selectedItems:new Set(),downloadTasks:new Map(),downloadPollTimer:null,hiddenCols:new Set(),filters:{query:'',platform:'',status:''},sort:{field:'updated_at',dir:'desc'},rowDensity:'normal',toolPanel:'',colWidths:[...defaultColWidths]};
const tableColumns=['选择','封面','作品标题','来源','类型','平台','时长','点赞','评论','分享','发布时间','状态','文案/逐字稿','备注','口喷日报','操作'];
const tableFields=['select','cover','title','source_url','source_type','platform','duration','likes','comments','shares','published_at','status','caption','error','max_daily_card','actions'];
loadTablePrefs();
function loadTablePrefs(){try{const raw=localStorage.getItem('chen.tablePrefs');if(!raw)return;const prefs=JSON.parse(raw);if(prefs.uiVersion!==tablePrefsVersion)return;if(Array.isArray(prefs.colWidths)&&prefs.colWidths.length===tableColumns.length)state.colWidths=prefs.colWidths.map((x,i)=>Number(x)||defaultColWidths[i]);if(Array.isArray(prefs.hiddenCols))state.hiddenCols=new Set(prefs.hiddenCols.filter(i=>i>0&&i<tableColumns.length-1));if(prefs.rowDensity)state.rowDensity=prefs.rowDensity;if(prefs.sort)state.sort={field:prefs.sort.field||'updated_at',dir:prefs.sort.dir==='asc'?'asc':'desc'};if(prefs.filters)state.filters={query:prefs.filters.query||'',platform:prefs.filters.platform||'',status:prefs.filters.status||''}}catch(e){}}
function saveTablePrefs(){try{localStorage.setItem('chen.tablePrefs',JSON.stringify({uiVersion:tablePrefsVersion,colWidths:state.colWidths,hiddenCols:[...state.hiddenCols],rowDensity:state.rowDensity,sort:state.sort,filters:state.filters}))}catch(e){}}
function qs(s){return document.querySelector(s)}function qsa(s){return [...document.querySelectorAll(s)]}
async function api(path,opts={}){const url=new URL(path,window.location.href);if(url.origin!==window.location.origin)throw new Error('请求地址不受信任');const headers={...(opts.headers||{}),'Content-Type':'application/json'};const res=await fetch(url.pathname+url.search,{...opts,headers,mode:'same-origin',credentials:'same-origin'});if(!res.ok)throw new Error(await res.text());return res.json()}
const setupCheckLabels={python:'Python',playwright:'Playwright',yt_dlp:'yt-dlp',ffmpeg:'ffmpeg',browser:'Chrome / Edge'};
let workbenchInitialized=false;
function renderSetupProgress(status={}){const diagnostics=status.diagnostics||{};qs('#setupProgress').innerHTML=Object.entries(setupCheckLabels).map(([key,label])=>{const check=diagnostics[key];const tone=!check?'warn':(check.ok?'ok':'warn');const detail=!check?'检测中':(check.ok?(check.version||check.name||'可用'):'未检测到');return `<div class="setup-check ${tone}"><strong>${escapeHtml(label)}</strong><br>${escapeHtml(detail)}</div>`}).join('')}
function initializeWorkbench(){if(workbenchInitialized)return;workbenchInitialized=true;renderPlatforms();installDownloadBatchButtons();loadTables().catch(e=>qs('#status').textContent='加载失败：'+e.message);pollDownloadQueue(false);loadEngineStatus();setInterval(loadEngineStatus,10000);setInterval(()=>{if(state.tableId)loadItems().catch(()=>{})},15000)}
function enterWorkbench(){qs('#setupPage').hidden=true;qs('#homePage').hidden=false;qs('#homePage').classList.remove('hidden');qs('#homePage').removeAttribute('aria-hidden');initializeWorkbench()}
async function initializeSetup(){const message=qs('#setupMessage');try{const status=await api('/api/setup/status');renderSetupProgress(status);if(status.complete){enterWorkbench();return}qs('#setupPage').hidden=false;qs('#homePage').hidden=true;qs('#homePage').classList.add('hidden');qs('#homePage').setAttribute('aria-hidden','true');message.textContent='请完成飞书配置，或先进入本地模式。';message.className='setup-message'}catch(error){qs('#setupPage').hidden=false;qs('#homePage').hidden=true;qs('#homePage').classList.add('hidden');renderSetupProgress({});message.textContent='读取首次设置状态失败，请重新打开应用。';message.className='setup-message error'}}
async function saveFirstRunSetup(localOnly){const submit=qs('#setupSubmit'),localOnlyButton=qs('#setupLocalOnly'),message=qs('#setupMessage');let saved=false;submit.disabled=true;localOnlyButton.disabled=true;message.textContent=localOnly?'正在启用本地模式...':'正在验证并保存配置...';message.className='setup-message';renderSetupProgress({});const payload=localOnly?{local_only:true}:{feishu_app_id:qs('#setupFeishuAppId').value,feishu_app_secret:qs('#setupFeishuAppSecret').value,feishu_app_token:qs('#setupFeishuAppToken').value,feishu_table_id:qs('#setupFeishuTableId').value,feishu_mobile_inbox_table_id:qs('#setupFeishuMobileTableId').value,publisher_worker_url:qs('#setupPublisherWorkerUrl').value,publisher_device_token:qs('#setupPublisherToken').value};try{const status=await api('/api/setup/save',{method:'POST',body:JSON.stringify(payload)});renderSetupProgress(status);if(!status.complete)throw new Error('配置尚未完成');saved=true;message.textContent=localOnly?'已进入本地模式。':'配置已安全保存。';message.className='setup-message ok';setTimeout(enterWorkbench,250)}catch(error){let detail=error.message;try{detail=JSON.parse(detail).error||detail}catch(ignore){}message.textContent='设置失败：'+detail;message.className='setup-message error'}finally{if(!saved){submit.disabled=false;localOnlyButton.disabled=false}}}
function engineTone(s){if(!s||!s.ok)return 'error';if((s.pending_count||0)>0||s.status==='需要登录处理')return 'warn';return 'ok'}
function renderEngineStatus(s){const tone=engineTone(s);const title=s&&s.status?s.status:'采集引擎离线';const bits=s?[`队列 ${s.pending_count||0}`,`电源 ${s.power_source||'未知'}`,`浏览器 ${s.cdp_browser||'未知'}`]:['队列 -','电源 -','浏览器 -'];const card=qs('#engineStatusCard');if(card){card.className='engine-card '+tone;card.innerHTML=`<div class="engine-main"><span class="engine-dot"></span><strong>${escapeHtml(title)}</strong></div><div class="engine-grid">${bits.map(x=>`<span>${escapeHtml(x)}</span>`).join('')}</div>`}const mini=qs('#engineMini');if(mini){mini.className='engine-mini '+tone;mini.textContent=s?`${title} · 队列 ${s.pending_count||0} · ${s.power_source||'未知'}`:'采集引擎离线'}}
async function loadEngineStatus(){try{const s=await api('/api/engine/status');renderEngineStatus(s)}catch(e){renderEngineStatus(null)}}
function platformData(name){return platforms.find(p=>p[0]===name)||platforms[0]}
function douyinHelpHtml(){return `<div class="guide"><p class="guide-kicker"><span class="guide-badge">抖音教学</span>先选中一个采集表，再进入抖音扒取。结果会保存在当前表格里，可以编辑、复制、看封面和导出。</p><div class="guide-section"><h3>单个作品链接怎么抓</h3><ol><li>打开抖音作品页，复制浏览器里的作品链接。</li><li>回到软件，点“单作品链接”。</li><li>把链接粘贴到输入框里；多条链接可以一行一个。</li><li>点“开始扒取”，软件会自动补全标题、封面、时长、点赞、评论、分享、发布时间。</li><li>如果视频能拿到音频，会继续写入“文案/逐字稿”；长视频会比短视频慢一些。</li></ol></div><div class="guide-section"><h3>主页链接怎么批量抓</h3><ol><li>复制抖音博主主页链接，切到“主页链接”。</li><li>粘贴主页链接后点“开始扒取”。</li><li>软件会打开抖音专用浏览器；如果没登录，你先在弹出的页面里登录。</li><li>登录后手动向下滑主页，页面加载出哪些视频，软件就会实时发现并写进当前表格。</li><li>想结束时点“停止监听”。已发现的视频不会丢，会留在表格里。</li></ol></div><div class="guide-section"><h3>结果怎么看</h3><ul><li>“表格”适合批量检查和编辑；“卡片”适合看封面；“详情”适合看长文案。</li><li>封面可以点开放大，也可以保存到本地。</li><li>单元格里的标题、文案、状态、备注都可以直接点进去编辑。</li><li>右上角“导出表格”可以把当前采集表保存成 CSV、Markdown 或 JSON。</li></ul></div><div class="guide-section"><h3>常见情况</h3><ul><li>提示需要登录：点“打开平台浏览器”，如果页面确实退出登录，再完成登录。</li><li>提示浏览器未就绪：重新点“打开平台浏览器”或关闭专用浏览器后再试，不代表账号掉线。</li><li>主页批量没有新增：确认你粘贴的是主页链接，并且在弹出的抖音主页里继续下滑加载作品。</li><li>逐字稿没立刻出现：通常是音频转写排队或平台没有直接字幕，等状态更新即可。</li><li>某条失败：看“备注”列，里面会写具体失败原因。</li></ul></div></div>`}
function platformHelpHtml(p){if(p==='抖音')return douyinHelpHtml();const info=platformData(p);return `<div class="guide"><p class="guide-kicker"><span class="guide-badge">${escapeHtml(p)}帮助</span>${escapeHtml(info[3])}</p><div class="guide-section"><h3>单个作品怎么抓</h3><ol><li>复制${escapeHtml(p)}单个作品链接。</li><li>回到软件，点“单作品链接”。</li><li>把链接粘贴到输入框里；多条链接可以一行一个。</li><li>点“开始扒取”，完成后在右侧表格查看结果。</li></ol></div><div class="guide-section"><h3>主页怎么批量抓</h3><ol><li>复制${escapeHtml(p)}主页链接，切到“主页链接”。</li><li>点“扫描主页”，软件会打开${escapeHtml(p)}专用浏览器。</li><li>如果需要登录，先在弹出的页面里登录。</li><li>手动向下滑主页，页面加载出哪些作品，软件就会先写入候选。</li><li>回到软件勾选候选，再点“采集选中”或“选中+逐字稿”。</li></ol></div><div class="guide-section"><h3>结果怎么看</h3><ul><li>表格、卡片、详情三种视图都可用。</li><li>封面可以点开放大，也可以保存。</li><li>表格文字可以编辑和复制，右上角可导出 CSV、Markdown 或 JSON。</li></ul></div></div>`}
function renderPlatforms(){qs('#platforms').innerHTML=platforms.map(p=>`<div class="platform ${p[0]===state.platform?'selected':''}" onclick="selectPlatform('${p[0]}')"><div class="platform-head"><span class="appicon ${p[1]}"></span>${p[0]}扒取</div><ul>${p[2].map(x=>`<li class="${x.includes('规划中')?'pending':''}">${x}</li>`).join('')}</ul><div class="platform-actions"><button class="btn" onclick="event.stopPropagation();showPlatformHelp('${p[0]}')">帮助</button><button class="btn primary" onclick="event.stopPropagation();enterPlatform('${p[0]}')">采集</button></div></div>`).join('')}
function selectPlatform(p){state.platform=p;renderPlatforms()}
function showPlatformHelp(p){selectPlatform(p);qs('#helpModalTitle').textContent=p+'操作帮助';qs('#helpModalBody').innerHTML=platformHelpHtml(p);qs('#helpModal').classList.add('open')}
function closeHelpModal(event){if(event&&event.target&&event.target.id!=='helpModal')return;const modal=qs('#helpModal');if(modal)modal.classList.remove('open')}
function enterPlatform(p){selectPlatform(p);state.page='app';state.tableMessage='';const info=platformData(p);qs('#downloadQueuePage').classList.add('hidden');qs('#homePage').classList.add('hidden');qs('#homePage').setAttribute('aria-hidden','true');qs('#appPage').classList.remove('hidden');qs('#appPage').removeAttribute('aria-hidden');qs('#urls').value='';setStatus('准备好了。');qs('#controlTitle').textContent=p+'扒取';qs('#pageTitle').textContent=p+'扒取';qs('#pageIcon').className='appicon '+info[1];setMode(state.mode);renderTables();loadItems()}
function showHome(){state.page='home';state.view='table';state.tableMessage='';qs('#urls').value='';setStatus('准备好了。');qs('#downloadQueuePage').classList.add('hidden');qs('#appPage').classList.add('hidden');qs('#appPage').setAttribute('aria-hidden','true');qs('#homePage').classList.remove('hidden');qs('#homePage').removeAttribute('aria-hidden');setView('table');renderTables();loadItems()}
function setMode(m){state.mode=m;qsa('.tab').forEach(b=>b.classList.toggle('active',b.dataset.mode===m));qs('#urls').placeholder=m==='single'?'把作品链接粘贴到这里，一行一个':`把${state.platform}主页链接粘贴到这里；每次扫描一个主页`;if(qs('#modeNote'))qs('#modeNote').textContent=m==='single'?'单作品模式：每条链接会抓标题、封面、互动数据，并在可用时转写逐字稿。':`主页链接模式：点开始后会打开${state.platform}主页专用浏览器。你登录并手动下滑主页，软件只生成候选预览；勾选作品后再采集基础信息或逐字稿。`;if(qs('#scrapeButton'))qs('#scrapeButton').textContent=m==='profile'?'扫描主页':'开始扒取';if(qs('#profileStop'))qs('#profileStop').style.display=(m==='profile'&&state.profileSessionId)?'inline-flex':'none'}
function setView(v){state.view=v;qsa('.view').forEach(b=>b.classList.toggle('active',b.dataset.view===v));renderItems()}
async function loadTables(){state.tables=await api('/api/tables');if(!state.tableId&&state.tables[0])state.tableId=state.tables[0].id;renderTables();await loadItems()}
function currentTable(){return state.tables.find(t=>t.id===state.tableId)||null}
function renderTables(){const tableBox=qs('#tables');if(tableBox)tableBox.innerHTML=state.tables.map(tableRowHtml).join('');const t=currentTable();const title=t?`${t.name} · 采集结果`:'采集结果';if(qs('#resultTitle'))qs('#resultTitle').textContent=title;if(qs('#homeResultTitle'))qs('#homeResultTitle').textContent=t?`${t.name} · 表格预览`:'表格预览';if(qs('#currentTableHint'))qs('#currentTableHint').textContent=t?`当前表格：${t.name}`:'当前表格：未选择';if(qs('#tableStatus'))qs('#tableStatus').textContent=state.tableMessage||''}
function desktop_table_is_system(t){return !!(t&&t.system_key)}
function tableRowHtml(t){const isSystem=desktop_table_is_system(t);if(!isSystem&&t.id===state.editingTableId)return `<div class="table-row editing selected"><div class="rename-form"><input id="renameInput" value="${escapeAttr(t.name)}" onkeydown="if(event.key==='Enter')saveRename('${t.id}');if(event.key==='Escape')cancelTableAction()"><button class="btn table-edit primary" onclick="saveRename('${t.id}')">保存</button><button class="btn table-edit" onclick="cancelTableAction()">取消</button></div></div>`;const confirm=!isSystem&&t.id===state.deletingTableId?`<div class="confirm-strip">确认删除「${escapeHtml(t.name)}」？里面的 ${t.item_count||0} 条记录也会删除。<div class="confirm-actions"><button class="btn table-edit danger" onclick="event.stopPropagation();confirmDelete('${t.id}')">确认删除</button><button class="btn table-edit" onclick="event.stopPropagation();cancelTableAction()">取消</button></div></div>`:'';const when=(t.updated_at||t.created_at||'').slice(5,16);const actions=isSystem?'':`<div class="table-actions"><button class="btn table-edit" onclick="event.stopPropagation();startRename('${t.id}')">改名</button><button class="btn table-edit danger" onclick="event.stopPropagation();askDelete('${t.id}')">删除</button></div>`;return `<div class="table-row ${t.id===state.tableId?'selected':''}" onclick="selectTable('${t.id}')"><div class="table-main"><span class="table-name" title="${escapeAttr(t.name)}">${escapeHtml(t.name)}</span><span class="table-meta">${escapeHtml(t.default_platform||'通用')} · ${t.item_count||0} 条记录${when?' · '+escapeHtml(when):''}</span>${confirm}</div>${actions}</div>`}
function selectTable(id){state.tableId=id;state.editingTableId='';state.deletingTableId='';state.tableMessage='';setView('table');loadItems()}
async function createTable(){const input=qs('#tableName');const name=input.value.trim();try{const t=await api('/api/tables',{method:'POST',body:JSON.stringify({name,default_platform:state.platform})});state.tableId=t.id;state.tableMessage=`已新建「${t.name}」。`;input.value='';await loadTables()}catch(e){state.tableMessage='新建失败：'+e.message;renderTables()}}
function startRename(id){state.tableId=id;state.editingTableId=id;state.deletingTableId='';state.tableMessage='';renderTables();setTimeout(()=>{const input=qs('#renameInput');if(input){input.focus();input.select()}},0)}
async function saveRename(id){const input=qs('#renameInput');const name=(input&&input.value||'').trim();if(!name){state.tableMessage='表格名字不能为空。';renderTables();return}try{const t=await api('/api/tables/rename',{method:'POST',body:JSON.stringify({table_id:id,name})});state.tableId=id;state.editingTableId='';state.tableMessage=`已改名为「${t.name}」。`;await loadTables()}catch(e){state.tableMessage='改名失败：'+e.message;renderTables()}}
function askDelete(id){state.tableId=id;state.editingTableId='';state.deletingTableId=id;state.tableMessage='';renderTables()}
async function confirmDelete(id){try{const r=await api('/api/tables/delete',{method:'POST',body:JSON.stringify({table_id:id})});state.tableId=(r.next_table&&r.next_table.id)||'';state.deletingTableId='';state.view='table';state.tableMessage='已删除采集表。';await loadTables()}catch(e){state.tableMessage='删除失败：'+e.message;renderTables()}}
function cancelTableAction(){state.editingTableId='';state.deletingTableId='';renderTables()}
async function loadItems(){if(!state.tableId)return;state.items=await api('/api/items?table_id='+encodeURIComponent(state.tableId));state.selectedItems=new Set([...state.selectedItems].filter(id=>state.items.some(i=>i.id===id)));renderTables();renderItems()}
function visibleColumns(){return tableColumns.map((_,i)=>i).filter(i=>!state.hiddenCols.has(i))}
function filteredItems(){const q=(state.filters.query||'').trim().toLowerCase();return state.items.filter(i=>{if(state.filters.platform&&i.platform!==state.filters.platform)return false;if(state.filters.status&&i.status!==state.filters.status)return false;if(!q)return true;return ['title','source_url','caption','error','status','platform'].some(k=>String(i[k]??'').toLowerCase().includes(q))})}
function sortedItems(){const rows=[...filteredItems()];const field=state.sort.field||'updated_at';const dir=state.sort.dir==='asc'?1:-1;const numeric=new Set(['likes','comments','shares']);rows.sort((a,b)=>{let av=a[field],bv=b[field];if(numeric.has(field)){av=Number(av||0);bv=Number(bv||0);return (av-bv)*dir}av=String(av??'');bv=String(bv??'');return av.localeCompare(bv,'zh-Hans-CN',{numeric:true})*dir});return rows}
function resultHtml(){const rows=sortedItems();if(state.view==='card')return rows.length?'<div class="cards">'+rows.map(cardHtml).join('')+'</div>':'<p class="muted">当前筛选下没有卡片。</p>';if(state.view==='detail'){const it=rows[0];return it?`<div class="detail">${escapeHtml(it.caption||it.error||'暂无详情')}</div>`:'<p class="muted">当前筛选下没有详情。</p>'}const cols=visibleColumns();return `<div class="table-scroll"><table class="result-table density-${state.rowDensity}">${tableColgroup()}<thead><tr>${tableHeader()}</tr></thead><tbody>${rows.length?rows.map(rowHtml).join(''):`<tr><td colspan="${cols.length}" class="muted">当前筛选下没有采集结果。</td></tr>`}</tbody></table></div>`}
function renderItems(){const html=resultHtml();if(qs('#results'))qs('#results').innerHTML=html;if(qs('#homeResults'))qs('#homeResults').innerHTML=html;renderTablePanels();updateSelectedCount()}
function tableColgroup(){return '<colgroup>'+visibleColumns().map(i=>`<col style="width:${state.colWidths[i]||120}px">`).join('')+'</colgroup>'}
function tableHeader(){return visibleColumns().map(i=>{const name=tableColumns[i];const mark=state.sort.field===tableFields[i]?`<span class="sort-mark">${state.sort.dir==='asc'?'↑':'↓'}</span>`:'';return `<th><div class="th-inner"><span onclick="sortByColumn(${i})">${name}${mark}</span><span class="col-resizer" onmousedown="startColumnResize(event,${i})"></span></div></th>`}).join('')}
function startColumnResize(event,index){event.preventDefault();event.stopPropagation();const startX=event.clientX;const startWidth=state.colWidths[index]||120;document.body.style.cursor='col-resize';document.body.style.userSelect='none';function move(e){state.colWidths[index]=Math.max(58,Math.min(520,startWidth+e.clientX-startX));const visibleIndex=visibleColumns().indexOf(index)+1;qsa(`.result-table col:nth-child(${visibleIndex})`).forEach(col=>col.style.width=state.colWidths[index]+'px')}function up(){document.removeEventListener('mousemove',move);document.removeEventListener('mouseup',up);document.body.style.cursor='';document.body.style.userSelect='';saveTablePrefs()}document.addEventListener('mousemove',move);document.addEventListener('mouseup',up)}
function rowHtml(i){const selected=state.selectedItems.has(i.id)?' row-selected':'';const id=escapeAttr(i.id||'');const cells=[selectHtml(i),coverHtml(i.cover_url),editableCell(i,'title'),`<div class="source-link" title="${escapeAttr(i.source_url||'')}">${escapeHtml(i.source_url||'')}</div><button class="btn copy-btn" onclick="copyText('${escapeJs(i.source_url||'')}')">复制</button>`,i.source_type==='profile'?'主页批量':'单作品',editableCell(i,'platform'),editableCell(i,'duration'),editableCell(i,'likes'),editableCell(i,'comments'),editableCell(i,'shares'),editableCell(i,'published_at'),editableCell(i,'status','status-pill'),editableCell(i,'caption'),editableCell(i,'error'),editableCell(i,'max_daily_card'),`<button class="btn copy-btn" onclick="copyRow('${id}')">复制整行</button><button class="btn copy-btn" onclick="addRowToDaily('${id}')">录入日报</button><button class="btn copy-btn" onclick="removeRowFromDaily('${id}')">删除日报</button>${downloadActionHtml(i)}`];return `<tr class="${selected}" data-item="${id}">${visibleColumns().map(index=>`<td>${cells[index]}</td>`).join('')}</tr>`}
function selectHtml(i){const id=escapeAttr(i.id||'');const checked=state.selectedItems.has(i.id)?'checked':'';return `<input class="candidate-check" type="checkbox" ${checked} onchange="toggleSelectedItem('${id}',this.checked)" title="勾选后可采集选中">`}
function cardHtml(i){const id=escapeAttr(i.id||'');return `<div class="card">${coverHtml(i.cover_url)}<h3>${escapeHtml(i.title||'未命名')}</h3><p class="muted">${escapeHtml(i.platform||'')} · ${i.source_type==='profile'?'主页批量':'单作品'} · ${escapeHtml(i.status||'')}</p><p>${escapeHtml((i.caption||i.error||'').slice(0,90))}</p><button class="btn copy-btn" onclick="copyRow('${id}')">复制这条</button>${downloadActionHtml(i)}</div>`}
function editableCell(item,field,extra=''){const raw=item[field];const value=raw===null||raw===undefined?'':String(raw);const kind=['caption','error','title','max_daily_card'].includes(field)?'cell-long':'cell-short';return `<div class="editable-cell ${kind} ${extra}" contenteditable="true" spellcheck="false" data-item="${escapeAttr(item.id||'')}" data-field="${field}" onfocus="this.dataset.before=this.innerText" onblur="saveCell(this)">${escapeHtml(value)}</div>`}
function coverHtml(url){if(!url)return '';const safe=escapeAttr(url);return `<button class="cover-btn" onclick="openCover('${escapeJs(url)}')" title="点击放大封面"><img class="cover" src="${coverProxy(url)}" alt="封面"></button>`}
function setStatus(text,type=''){const el=qs('#status');if(!el)return;el.textContent=text;el.className='status '+type}
function setTableFeedback(text,type=''){state.tableMessage=text;if(qs('#tableStatus'))qs('#tableStatus').textContent=text;setStatus(text,type)}
function coverProxy(url,download=false){return '/api/cover?url='+encodeURIComponent(url)+(download?'&download=1':'')}
async function saveCell(el){const itemId=el.dataset.item||'';const field=el.dataset.field||'';const value=el.innerText.trim();if(value===(el.dataset.before||''))return;try{await api('/api/items/update',{method:'POST',body:JSON.stringify({item_id:itemId,updates:{[field]:value}})});el.dataset.before=value;el.classList.remove('save-error');state.tableMessage='已保存单元格修改。';if(qs('#tableStatus'))qs('#tableStatus').textContent=state.tableMessage;const item=state.items.find(x=>x.id===itemId);if(item)item[field]=value}catch(e){el.classList.add('save-error');setStatus('保存失败：'+e.message,'error')}}
async function copyText(text){try{if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(text)}else{const t=document.createElement('textarea');t.value=text;document.body.appendChild(t);t.select();document.execCommand('copy');t.remove()}setStatus('已复制。','ok')}catch(e){setStatus('复制失败，请手动选中文字复制。','error')}}
function copyRow(id){const i=state.items.find(x=>x.id===id);if(!i)return;const text=['平台: '+(i.platform||''),'作品链接: '+(i.source_url||''),'作品标题: '+(i.title||''),'文案/逐字稿: '+(i.caption||''),'口喷日报: '+(i.max_daily_card||''),'封面图链接: '+(i.cover_url||''),'时长: '+(i.duration||''),'点赞: '+(i.likes??''),'评论: '+(i.comments??''),'分享: '+(i.shares??''),'发布时间: '+(i.published_at||''),'状态: '+(i.status||''),'备注: '+(i.error||'')].join('\\n');copyText(text)}
function openCover(url){const modal=qs('#coverModal');modal.dataset.url=url;qs('#coverLarge').src=coverProxy(url);qs('#coverUrlText').textContent=url;modal.classList.add('open')}
function closeCover(event){if(event&&event.target&&event.target.id!=='coverModal')return;const modal=qs('#coverModal');if(!modal)return;modal.classList.remove('open');qs('#coverLarge').removeAttribute('src')}
function currentCoverUrl(){const modal=qs('#coverModal');return (modal&&modal.dataset.url)||''}
function openExportModal(){if(!state.tableId){state.tableMessage='请先选择或新建采集表。';renderTables();return}const t=currentTable();qs('#exportTableName').textContent=t?t.name:'当前采集表';qs('#exportPath').textContent='默认会生成可直接给 Max 阅读的 Markdown 日报。';qs('#exportModal').classList.add('open')}
function closeExportModal(event){if(event&&event.target&&event.target.id!=='exportModal')return;const modal=qs('#exportModal');if(modal)modal.classList.remove('open')}
async function saveExportFile(){if(!state.tableId)return;const fmt=(qs('#exportFormat')&&qs('#exportFormat').value)||'max-daily';const note=qs('#exportPath');try{note.textContent='正在打开保存窗口...';const r=await api('/api/export/save',{method:'POST',body:JSON.stringify({table_id:state.tableId,format:fmt})});note.textContent='已保存到：'+r.path;setStatus(fmt==='max-daily'?'MAX 日报已生成。':'表格已导出。','ok')}catch(e){note.textContent='导出失败：'+e.message;setStatus('导出失败：'+e.message,'error')}}
function downloadExportFile(){if(!state.tableId)return;const fmt=(qs('#exportFormat')&&qs('#exportFormat').value)||'max-daily';location.href='/api/export?table_id='+encodeURIComponent(state.tableId)+'&format='+encodeURIComponent(fmt)}
function openDailyPage(){const path=state.tableId?'/daily?table_id='+encodeURIComponent(state.tableId):'/daily';window.location.href=path}
async function openCoverOriginal(){const url=currentCoverUrl();if(!url)return;try{await api('/api/open-url',{method:'POST',body:JSON.stringify({url})});setStatus('已用系统浏览器打开原图。','ok')}catch(e){setStatus('打开原图失败：'+e.message,'error')}}
async function saveCoverFile(){const url=currentCoverUrl();if(!url)return;const text=qs('#coverUrlText');try{text.textContent='正在保存封面...';const r=await api('/api/cover/save',{method:'POST',body:JSON.stringify({url,platform:state.platform})});text.textContent='已保存到：'+r.path;setStatus('封面已保存到下载目录。','ok')}catch(e){text.textContent=url;setStatus('保存封面失败：'+e.message,'error')}}
function closeModal(id){const el=qs('#'+id);if(el)el.classList.remove('open')}
async function loadVideoSettings(){const s=await api('/api/settings/video-download');qs('#singleDirectory').textContent=s.single_directory;qs('#batchDirectory').textContent=s.batch_directory}
async function openSettings(){qs('#settingsModal').classList.add('open');try{await loadVideoSettings()}catch(e){setStatus('读取下载设置失败：'+e.message,'error')}}
async function chooseVideoDirectory(kind){try{const s=await api('/api/settings/video-download/choose',{method:'POST',body:JSON.stringify({kind})});qs('#singleDirectory').textContent=s.single_directory;qs('#batchDirectory').textContent=s.batch_directory}catch(e){setStatus('设置下载目录失败：'+e.message,'error')}}
async function resetVideoDirectory(kind){try{const s=await api('/api/settings/video-download/reset',{method:'POST',body:JSON.stringify({kind})});qs('#singleDirectory').textContent=s.single_directory;qs('#batchDirectory').textContent=s.batch_directory}catch(e){setStatus('恢复默认目录失败：'+e.message,'error')}}
async function openVideoDirectory(kind){try{await api('/api/settings/video-download/open',{method:'POST',body:JSON.stringify({kind})})}catch(e){setStatus('打开文件夹失败：'+e.message,'error')}}
async function saveVideoFile(id){if(!id)return;state.downloadTasks.set(id,{item_id:id,status:'preparing',stage:'获取视频地址',progress:0});renderItems();try{setStatus('视频已加入下载队列。','ok');await api('/api/video/tasks',{method:'POST',body:JSON.stringify({item_id:id})});await pollDownloadQueue(false)}catch(e){state.downloadTasks.set(id,{item_id:id,status:'failed',stage:'下载失败',error_message:e.message,progress:0});renderItems();setStatus('下载视频失败：'+e.message,'error')}}
function downloadSelectedVideos(){const ids=[...state.selectedItems];if(!ids.length){setTableFeedback('请先勾选要批量下载的视频。','error');return}return api('/api/video/batches',{method:'POST',body:JSON.stringify({item_ids:ids})}).then(()=>{setTableFeedback(`已加入批量下载 ${ids.length} 条。`,'ok');state.selectedItems.clear();return pollDownloadQueue(state.page==='downloads')}).catch(e=>setTableFeedback('批量下载失败：'+e.message,'error'))}
function latestDownloadTask(itemId){return state.downloadTasks.get(itemId)||null}
function downloadActionHtml(item){const id=escapeAttr(item.id||''),task=latestDownloadTask(item.id);if(!task)return `<div class="inline-download"><button class="btn copy-btn" onclick="saveVideoFile('${id}')">下载视频</button></div>`;const status=task.status||'',progress=Math.max(0,Math.min(100,Number(task.progress||0)));if(['queued','preparing','downloading','merging'].includes(status)){const label=status==='queued'?'等待下载':status==='merging'?'正在合并':(progress>0?`正在下载 ${progress.toFixed(0)}%`:'正在获取视频');return `<div class="inline-download"><button class="btn copy-btn is-downloading" disabled>${label}</button><div class="download-progress-track"><div class="download-progress-bar" style="width:${Math.max(progress,status==='preparing'?8:0)}%"></div></div></div>`}if(status==='completed'&&task.output_path)return `<div class="inline-download"><button class="btn copy-btn is-completed" onclick="openDownloadedVideo('${escapeAttr(task.id)}')">下载完成</button></div>`;if(status==='failed')return `<div class="inline-download"><button class="btn copy-btn is-failed" onclick="saveVideoFile('${id}')">下载失败 · 重试</button></div>`;return `<div class="inline-download"><button class="btn copy-btn" onclick="saveVideoFile('${id}')">下载视频</button></div>`}
function openDownloadQueue(){state.page='downloads';qs('#homePage').classList.add('hidden');qs('#appPage').classList.add('hidden');qs('#downloadQueuePage').classList.remove('hidden');window.scrollTo(0,0);pollDownloadQueue(true)}
function closeDownloadQueuePage(){showHome()}
async function retryFailedDownloads(){await api('/api/video/retry-failed',{method:'POST',body:'{}'});pollDownloadQueue(true)}
async function cancelQueuedDownloads(){await api('/api/video/cancel-queued',{method:'POST',body:'{}'});pollDownloadQueue(true)}
async function clearCompletedDownloads(){await api('/api/video/clear-completed',{method:'POST',body:'{}'});pollDownloadQueue(true)}
async function openDownloadedVideo(taskId){try{await api('/api/video/open-output',{method:'POST',body:JSON.stringify({task_id:taskId})})}catch(e){setStatus('打开视频失败：'+e.message,'error')}}
async function openDownloadedFolder(taskId){try{await api('/api/video/open-folder',{method:'POST',body:JSON.stringify({task_id:taskId})})}catch(e){setStatus('打开文件夹失败：'+e.message,'error')}}
function formatBytes(value){const n=Number(value||0);if(!n)return '—';if(n>=1073741824)return (n/1073741824).toFixed(1)+' GB';return (n/1048576).toFixed(n>=10485760?0:1)+' MB'}
function downloadStatusLabel(task){return {queued:'等待下载',preparing:'获取地址',downloading:'正在下载',merging:'正在合并',completed:'下载完成',failed:'下载失败',cancelled:'已取消'}[task.status]||task.stage||task.status}
function downloadProgressHtml(task){const status=task.status||'',progress=status==='completed'?100:Math.max(0,Math.min(100,Number(task.progress||0))),tone=status==='completed'?'completed':status==='failed'?'failed':'';return `<div class="download-progress ${tone}"><div class="download-progress-track"><div class="download-progress-bar" style="width:${progress}%"></div></div><span class="download-progress-label">${progress?progress.toFixed(0)+'%':'—'}</span></div>`}
function downloadQueueRow(task){const status=task.status||'',statusClass=['downloading','preparing','merging'].includes(status)?'downloading':status;const actions=status==='completed'?`<button class="btn" onclick="openDownloadedVideo('${escapeAttr(task.id)}')">打开视频</button><button class="btn" onclick="openDownloadedFolder('${escapeAttr(task.id)}')">打开文件夹</button>`:status==='failed'?`<button class="btn" onclick="saveVideoFile('${escapeAttr(task.item_id)}')">重试</button><button class="btn" onclick="openDownloadedFolder('${escapeAttr(task.id)}')">打开文件夹</button>`:`<button class="btn" onclick="openDownloadedFolder('${escapeAttr(task.id)}')">打开文件夹</button>`;return `<tr><td><div class="download-title-cell"><span class="download-platform-dot"></span><span class="download-title-text" title="${escapeAttr(task.title||'未命名视频')}">${escapeHtml(task.title||'未命名视频')}</span></div></td><td>${escapeHtml(task.platform||'—')}</td><td><span class="download-status ${statusClass}">${escapeHtml(downloadStatusLabel(task))}</span></td><td>${downloadProgressHtml(task)}</td><td>${formatBytes(task.total_bytes||task.downloaded_bytes)}</td><td>${escapeHtml(task.completed_at||'—')}</td><td><div class="download-row-actions">${actions}</div></td></tr>`}
function renderDownloadQueue(queue){const c=queue.counts||{},active=(c.preparing||0)+(c.downloading||0)+(c.merging||0);qs('#downloadQueueSummary').innerHTML=`<div class="download-stat"><span>等待</span><strong>${c.queued||0}</strong></div><div class="download-stat downloading"><span>下载中</span><strong>${active}</strong></div><div class="download-stat completed"><span>已完成</span><strong>${c.completed||0}</strong></div><div class="download-stat failed"><span>失败</span><strong>${c.failed||0}</strong></div>`;const rows=(queue.tasks||[]).map(downloadQueueRow).join('');qs('#downloadQueueTable').innerHTML=rows?`<table class="download-table"><colgroup><col style="width:27%"><col style="width:7%"><col style="width:10%"><col style="width:19%"><col style="width:9%"><col style="width:12%"><col style="width:16%"></colgroup><thead><tr><th>视频标题</th><th>平台</th><th>状态</th><th>下载进度</th><th>文件大小</th><th>完成时间</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table>`:'<div class="download-empty">暂无下载任务，勾选视频后点击“批量下载所选”。</div>'}
async function pollDownloadQueue(renderPage=state.page==='downloads'){try{const q=await api('/api/video/queue');const newest=new Map();(q.tasks||[]).forEach(task=>{if(!newest.has(task.item_id))newest.set(task.item_id,task)});state.downloadTasks=newest;if(state.page!=='downloads')renderItems();if(renderPage&&qs('#downloadQueuePage')&&!qs('#downloadQueuePage').classList.contains('hidden'))renderDownloadQueue(q);const c=q.counts||{},active=(c.queued||0)+(c.preparing||0)+(c.downloading||0)+(c.merging||0);if(state.downloadPollTimer)clearTimeout(state.downloadPollTimer);state.downloadPollTimer=active?setTimeout(()=>pollDownloadQueue(state.page==='downloads'),1000):null}catch(e){setStatus('读取下载队列失败：'+e.message,'error')}}
function copyCurrentCoverUrl(){copyText(currentCoverUrl())}
function tableUnique(field){return [...new Set(state.items.map(i=>String(i[field]??'').trim()).filter(Boolean))].sort((a,b)=>a.localeCompare(b,'zh-Hans-CN',{numeric:true}))}
function tablePanelHtml(){if(!state.toolPanel)return '';if(state.toolPanel==='fields')return `<div class="toolbar-grid">${tableColumns.map((name,i)=>`<label class="toolbar-field"><input type="checkbox" ${state.hiddenCols.has(i)?'':'checked'} ${i===0||i===tableColumns.length-1?'disabled':''} onchange="setColumnVisible(${i},this.checked)"> ${name}</label>`).join('')}</div>`;if(state.toolPanel==='filter'){const platforms=tableUnique('platform').map(v=>`<option value="${escapeAttr(v)}" ${state.filters.platform===v?'selected':''}>${escapeHtml(v)}</option>`).join('');const statuses=tableUnique('status').map(v=>`<option value="${escapeAttr(v)}" ${state.filters.status===v?'selected':''}>${escapeHtml(v)}</option>`).join('');return `<div class="toolbar-form"><input placeholder="搜索标题、链接、文案、备注" value="${escapeAttr(state.filters.query)}" oninput="setFilter('query',this.value)"><select onchange="setFilter('platform',this.value)"><option value="">全部平台</option>${platforms}</select><select onchange="setFilter('status',this.value)"><option value="">全部状态</option>${statuses}</select><button class="btn" onclick="clearFilters()">清空筛选</button></div>`}if(state.toolPanel==='sort')return `<div class="toolbar-form"><select onchange="setSort(this.value,state.sort.dir)">${tableFields.map((field,i)=>field==='select'||field==='cover'||field==='actions'?'':`<option value="${field}" ${state.sort.field===field?'selected':''}>${tableColumns[i]}</option>`).join('')}</select><select onchange="setSort(state.sort.field,this.value)"><option value="desc" ${state.sort.dir==='desc'?'selected':''}>降序</option><option value="asc" ${state.sort.dir==='asc'?'selected':''}>升序</option></select></div>`;if(state.toolPanel==='density')return `<div class="density-actions"><button class="btn ${state.rowDensity==='compact'?'primary':''}" onclick="setRowDensity('compact')">紧凑</button><button class="btn ${state.rowDensity==='normal'?'primary':''}" onclick="setRowDensity('normal')">标准</button><button class="btn ${state.rowDensity==='relaxed'?'primary':''}" onclick="setRowDensity('relaxed')">宽松</button></div>`;return ''}
function renderTablePanels(){const label=toolPanelLabel();qsa('.table-toolbar-panel').forEach(el=>{el.innerHTML=tablePanelHtml();el.classList.toggle('open',!!state.toolPanel)});qsa('.table-tool').forEach(btn=>btn.classList.toggle('primary',!!label&&btn.textContent.includes(label)))}
function toolPanelLabel(){return {fields:'字段配置',filter:'筛选',sort:'排序',density:'行高'}[state.toolPanel]||''}
function toggleTableTool(panel){state.toolPanel=state.toolPanel===panel?'':panel;renderTablePanels()}
function setColumnVisible(index,visible){if(visible)state.hiddenCols.delete(index);else state.hiddenCols.add(index);saveTablePrefs();renderItems()}
function setFilter(field,value){state.filters={...state.filters,[field]:value};saveTablePrefs();renderItems()}
function clearFilters(){state.filters={query:'',platform:'',status:''};saveTablePrefs();renderItems();setTableFeedback('已清空筛选。','ok')}
function setSort(field,dir){state.sort={field:field||'updated_at',dir:dir==='asc'?'asc':'desc'};saveTablePrefs();renderItems()}
function sortByColumn(index){const field=tableFields[index];if(['select','cover','actions'].includes(field))return;const dir=state.sort.field===field&&state.sort.dir==='desc'?'asc':'desc';setSort(field,dir)}
function setRowDensity(value){state.rowDensity=value;saveTablePrefs();renderItems()}
function resetTableView(){state.hiddenCols=new Set();state.filters={query:'',platform:'',status:''};state.sort={field:'updated_at',dir:'desc'};state.rowDensity='normal';state.colWidths=[...defaultColWidths];state.toolPanel='';saveTablePrefs();renderItems();setTableFeedback('已重置当前表格视图。','ok')}
function updateSelectedCount(){qsa('.selected-count').forEach(el=>el.textContent=`已选 ${state.selectedItems.size}`)}
function toggleSelectedItem(id,checked){if(!id)return;if(checked)state.selectedItems.add(id);else state.selectedItems.delete(id);updateSelectedCount();setTableFeedback(state.selectedItems.size?`已选中 ${state.selectedItems.size} 条。`:'未选中记录。',state.selectedItems.size?'ok':'')}
function selectableItems(){return state.items.filter(i=>i.source_url&&i.id)}
function candidateItems(){return selectableItems()}
function selectCandidateItems(){const items=selectableItems();items.forEach(i=>state.selectedItems.add(i.id));renderItems();setTableFeedback(items.length?`已选中当前表格 ${items.length} 条。`:'当前表格没有可选择的记录。',items.length?'ok':'error')}
function clearSelectedItems(){state.selectedItems.clear();renderItems();setTableFeedback('已清空选择。','ok')}
function installDownloadBatchButtons(){qsa('.table-primary-actions').forEach(group=>{if(group.querySelector('.batch-download-btn'))return;const button=document.createElement('button');button.className='btn batch-download-btn';button.textContent='批量下载';button.onclick=downloadSelectedVideos;group.appendChild(button)})}
// 批量下载入口由表格多选状态驱动，也可从下载队列查看每条进度。
async function collectSelected(transcribe=false){if(!state.tableId){setTableFeedback('请先选择采集表。','error');return}const ids=[...state.selectedItems];if(!ids.length){setTableFeedback('请先勾选要采集的作品。','error');return}try{setTableFeedback(transcribe?'正在采集选中作品并转写逐字稿...':'正在采集选中作品基础信息...');const r=await api('/api/profile/collect-selected',{method:'POST',body:JSON.stringify({table_id:state.tableId,platform:state.platform,item_ids:ids,transcribe})});setTableFeedback(r.message||`已采集 ${r.processed_count||0} 条选中作品。`,r.paused?'error':'ok');state.selectedItems.clear();await loadTables()}catch(e){setTableFeedback('采集选中失败：'+e.message,'error')}}
async function addSelectedToDaily(){if(!state.tableId){setTableFeedback('请先选择采集表。','error');return}const ids=[...state.selectedItems];if(!ids.length){setTableFeedback('请先勾选要录入日报的素材。','error');return}try{const r=await api('/api/daily/add',{method:'POST',body:JSON.stringify({table_id:state.tableId,item_ids:ids,date:new Date().toISOString().slice(0,10)})});setTableFeedback(`已录入日报 ${r.count||0} 条。`,'ok');state.selectedItems.clear();await loadItems()}catch(e){setTableFeedback('录入日报失败：'+e.message,'error')}}
async function removeSelectedFromDaily(){const ids=[...state.selectedItems];if(!ids.length){setTableFeedback('请先勾选要从日报删除的素材。','error');return}try{const r=await api('/api/daily/remove',{method:'POST',body:JSON.stringify({item_ids:ids})});setTableFeedback(`已从日报删除 ${r.count||0} 条。`,'ok');state.selectedItems.clear();await loadItems()}catch(e){setTableFeedback('删除日报失败：'+e.message,'error')}}
async function addRowToDaily(id){if(!state.tableId){setTableFeedback('请先选择采集表。','error');return}if(!id)return;try{const r=await api('/api/daily/add',{method:'POST',body:JSON.stringify({table_id:state.tableId,item_ids:[id],date:new Date().toISOString().slice(0,10)})});setTableFeedback(`已录入日报 ${r.count||0} 条。`,'ok');await loadItems()}catch(e){setTableFeedback('录入日报失败：'+e.message,'error')}}
async function removeRowFromDaily(id){if(!id)return;try{const r=await api('/api/daily/remove',{method:'POST',body:JSON.stringify({item_ids:[id]})});setTableFeedback(`已从日报删除 ${r.count||0} 条。`,'ok');await loadItems()}catch(e){setTableFeedback('删除日报失败：'+e.message,'error')}}
async function scrape(){if(!state.tableId){setStatus('请先回主页创建或选择一个采集表格。','error');return}const urls=qs('#urls').value.split(/\\n+/).map(s=>s.trim()).filter(Boolean);if(!urls.length){setStatus('请先粘贴链接。','error');return}if(state.mode==='profile')return startProfileSession(urls[0]);setStatus('采集中，长视频转写会多等一会儿。');try{const r=await api('/api/scrape',{method:'POST',body:JSON.stringify({table_id:state.tableId,platform:state.platform,mode:state.mode,urls})});setStatus(`完成 ${r.items.length} 条。`,'ok');qs('#urls').value='';await loadTables()}catch(e){setStatus('失败：'+e.message,'error')}}
async function queueScrape(){if(!state.tableId){setStatus('请先回主页创建或选择一个采集表格。','error');return}const urls=qs('#urls').value.split(/\\n+/).map(s=>s.trim()).filter(Boolean);if(!urls.length){setStatus('请先粘贴链接。','error');return}try{setStatus('正在加入手机任务队列...');const r=await api('/api/queue/add',{method:'POST',body:JSON.stringify({table_id:state.tableId,platform:state.platform,mode:state.mode,urls})});setStatus(`已加入队列 ${r.count||0} 条。Mac 在线时会自动补跑。`,'ok');qs('#urls').value='';await loadTables();await loadEngineStatus()}catch(e){setStatus('加入队列失败：'+e.message,'error')}}
function toggleYouTubeTools(){const el=qs('#youtubeTools');if(!el)return;el.classList.toggle('open')}
async function diagnoseYouTube(probe=false){const urls=qs('#urls').value.split(/\\n+/).map(s=>s.trim()).filter(Boolean);const url=urls[0]||'';if(!url){setStatus('请先粘贴一条 YouTube 视频链接。','error');return}try{setStatus(probe?'正在做 YouTube 下载探测，可能需要稍等...':'正在诊断 YouTube 链路...');const r=await api('/api/youtube/diagnose',{method:'POST',body:JSON.stringify({url,probe_download:probe})});const checks=r.checks||{};const bits=[];if(checks.preflight)bits.push('预检:'+checks.preflight.status);if(checks.metadata)bits.push('元数据:'+checks.metadata.status);if(checks.captions)bits.push('字幕:'+checks.captions.status);if(checks.download_probe)bits.push('下载:'+checks.download_probe.status);setStatus(`YouTube${probe?'下载探测':'诊断'}：${r.status||''}。${bits.join('，')}。${r.recommended_action||''}`,r.ok?'ok':(r.status==='需下载音频'?'':'error'))}catch(e){setStatus('YouTube诊断失败：'+e.message,'error')}}
async function enableYouTubePoProvider(){try{setStatus('正在启用 YouTube PO Provider 优先模式...');const r=await api('/api/youtube/po-provider-enable',{method:'POST',body:JSON.stringify({enabled:true})});setStatus(r.message||'已启用 PO Provider 优先模式。请确认 yt-dlp PO Token Provider 插件已安装，然后重新下载探测。','ok')}catch(e){setStatus('启用 PO 模式失败：'+e.message,'error')}}
async function startProfileSession(url){if(!['抖音','小红书','B站','视频号','YouTube','Instagram'].includes(state.platform)){setStatus('这个平台暂未接入主页候选预览。','error');return}try{setStatus(`正在打开${state.platform}主页专用浏览器，打开后请手动下滑主页；本阶段只生成候选预览。`);const r=await api('/api/profile/start',{method:'POST',body:JSON.stringify({table_id:state.tableId,platform:state.platform,url})});state.profileSessionId=r.session_id||'';if(qs('#profileStop'))qs('#profileStop').style.display='inline-flex';setStatus(r.message||'主页扫描已启动。发现的作品会先作为候选写入表格，勾选后再采集详情或逐字稿。','ok');pollProfileSession();if(state.profilePollTimer)clearInterval(state.profilePollTimer);state.profilePollTimer=setInterval(pollProfileSession,3000)}catch(e){setStatus('主页扫描启动失败：'+e.message,'error')}}
async function pollProfileSession(){if(!state.profileSessionId)return;try{const r=await api('/api/profile/status?session_id='+encodeURIComponent(state.profileSessionId));const text=`${r.status||''}：${r.message||''} 候选 ${r.found_count||0} 条，已选 ${state.selectedItems.size} 条`;setStatus(text,(r.status==='失败'||(r.error_count||0)>0)?'error':'ok');await loadTables();if(['已停止','失败'].includes(r.status||'')){if(state.profilePollTimer)clearInterval(state.profilePollTimer);state.profilePollTimer=null;state.profileSessionId='';if(qs('#profileStop'))qs('#profileStop').style.display='none'}}catch(e){setStatus('读取主页扫描状态失败：'+e.message,'error')}}
async function stopProfileSession(){if(!state.profileSessionId)return;try{const r=await api('/api/profile/stop',{method:'POST',body:JSON.stringify({session_id:state.profileSessionId})});setStatus(r.message||'正在停止主页监听。','ok')}catch(e){setStatus('停止失败：'+e.message,'error')}}
async function openLogin(){const urls={'抖音':'https://www.douyin.com/','小红书':'https://www.xiaohongshu.com/','B站':'https://www.bilibili.com/','视频号':'https://channels.weixin.qq.com/','YouTube':'https://www.youtube.com/','Instagram':'https://www.instagram.com/'};const url=urls[state.platform]||'https://www.douyin.com/';try{setStatus('正在打开 '+state.platform+' 专用浏览器...');await api('/api/open-url',{method:'POST',body:JSON.stringify({url})});setStatus('已打开 '+state.platform+' 专用浏览器。如果页面已登录，可以直接回到软件重试采集。','ok')}catch(e){setStatus('打开平台浏览器失败：'+e.message,'error')}}
function exportCsv(){downloadExportFile()}
function escapeHtml(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
function escapeAttr(s){return escapeHtml(s).replace(/`/g,'&#96;')}
function escapeJs(s){return String(s??'').replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/\n/g,'\\n').replace(/\r/g,'')}
initializeSetup();
</script>
</body>
</html>"""


DESKTOP_DAILY_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>外部情报口喷日报</title>
  <style>
    :root{color-scheme:dark;--bg:#070a0f;--panel:#111923;--panel2:#172231;--line:rgba(255,255,255,.12);--text:#f3f7ff;--muted:#9ba8ba;--orange:#ff9d35;--blue:#7cc7ff;--mint:#72f0bd;--side:300px;--detail:560px;--row:88px}
    *{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at 18% 12%,rgba(255,157,53,.24),transparent 28rem),radial-gradient(circle at 78% 10%,rgba(124,199,255,.16),transparent 24rem),linear-gradient(135deg,#070a0f,#111821 58%,#0a1018);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",Inter,sans-serif;font-weight:850;overflow-x:hidden}
    #particleCanvas{position:fixed;inset:0;width:100%;height:100%;z-index:0;pointer-events:none;opacity:.76}.wrap,.timeline-drawer,.timeline-scrim{position:relative;z-index:1}
    body:before,body:after{content:"";position:fixed;z-index:0;width:34rem;height:34rem;border-radius:50%;filter:blur(38px);opacity:.2;animation:dailyFloat 12s ease-in-out infinite alternate;pointer-events:none}body:before{left:-12rem;top:8rem;background:#ff9d35}body:after{right:-10rem;bottom:3rem;background:#4fa8ff;animation-delay:-4s}@keyframes dailyFloat{0%{transform:translate3d(0,0,0) scale(.94)}100%{transform:translate3d(4rem,-2rem,0) scale(1.12)}}@keyframes shimmer{0%{background-position:0% 50%}100%{background-position:100% 50%}}
    .wrap{max-width:1880px;margin:0 auto;padding:28px}.top{display:flex;justify-content:space-between;align-items:end;gap:16px;border-bottom:1px solid var(--line);padding-bottom:20px;margin-bottom:12px}.kicker{color:var(--orange);font-weight:950;letter-spacing:.12em;text-shadow:0 0 22px rgba(255,157,53,.35)}.top h1{margin:4px 0 0;font-size:38px;letter-spacing:0}.meta{color:var(--muted);font-weight:900}.top-actions{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.count{color:#b8c4d3;font-size:20px;font-weight:950}.btn{border:1px solid rgba(255,255,255,.14);border-radius:10px;background:linear-gradient(180deg,rgba(255,255,255,.08),rgba(255,255,255,.035));color:#edf4ff;min-height:40px;padding:0 14px;font:inherit;font-size:14px;font-weight:950;cursor:pointer;box-shadow:inset 0 1px 0 rgba(255,255,255,.08),0 10px 28px rgba(0,0,0,.18);transition:transform .32s cubic-bezier(.18,.89,.32,1.28),border-color .2s,background .2s,box-shadow .2s}.btn:hover,.btn.active{background:#223246;border-color:rgba(255,255,255,.24);transform:translateY(-2px);box-shadow:inset 0 1px 0 rgba(255,255,255,.1),0 16px 36px rgba(0,0,0,.26)}.btn:active{transform:translateY(1px) scale(.98)}.btn:disabled{cursor:default;opacity:.52;transform:none;box-shadow:inset 0 1px 0 rgba(255,255,255,.05)}.btn.primary{background:linear-gradient(120deg,#ffd36b,var(--orange),#ffbd6f);background-size:180% 100%;color:#131923;border:0;animation:shimmer 5s ease-in-out infinite alternate}.btn.ghost{background:rgba(255,255,255,.05)}#publishCloudButton,#openCloudButton,#copyCloudButton{min-width:108px}.cloud-publish-status{max-width:min(100%,760px);min-height:20px;margin:0 0 18px;color:var(--muted);font-size:13px;font-weight:900;line-height:1.45;overflow-wrap:anywhere}.cloud-publish-status.running{color:#ffd36b}.cloud-publish-status.succeeded{color:#9df2bd}.cloud-publish-status.failed{color:#ffb4ca}
    .toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:18px 0 22px}.tool-group{display:flex;gap:8px;align-items:center;flex-wrap:wrap;border:1px solid var(--line);border-radius:12px;background:rgba(12,19,28,.72);padding:6px;backdrop-filter:blur(18px)}.tool-panel{display:none;margin:-8px 0 18px;border:1px solid var(--line);border-radius:14px;background:rgba(17,25,35,.88);padding:14px;backdrop-filter:blur(20px);box-shadow:0 18px 60px rgba(0,0,0,.28)}.tool-panel.open{display:block}.panel-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}.panel-head strong{font-size:17px}.hint{color:var(--muted);font-size:13px}.checks{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}.checks label,.control-line{border:1px solid rgba(255,255,255,.1);border-radius:11px;background:linear-gradient(180deg,rgba(255,255,255,.065),rgba(255,255,255,.028));padding:10px 11px;color:#dce7f5}.checks input{accent-color:var(--orange)}.control-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}.control-line{display:grid;gap:8px}.control-line input,.control-line select{width:100%;accent-color:var(--orange);border:1px solid rgba(255,255,255,.14);border-radius:8px;background:#0b121c;color:#edf4ff;padding:8px}.control-line input[type=range]{height:22px;padding:0;background:transparent}.control-line input[type=range]::-webkit-slider-runnable-track{height:7px;border-radius:999px;background:linear-gradient(90deg,rgba(255,157,53,.9),rgba(124,199,255,.75));box-shadow:0 0 20px rgba(255,157,53,.22)}.control-line input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:22px;height:22px;border-radius:50%;margin-top:-7px;background:#fff3dd;border:3px solid var(--orange);box-shadow:0 8px 24px rgba(255,157,53,.35);transition:transform .26s cubic-bezier(.18,.89,.32,1.28)}.control-line input[type=range]:active::-webkit-slider-thumb{transform:scale(1.22)}
    .layout{display:grid;grid-template-columns:var(--side) minmax(460px,1fr) minmax(380px,var(--detail));gap:18px;align-items:start;transition:grid-template-columns .45s cubic-bezier(.18,.89,.32,1.08)}.panel{border:1px solid var(--line);border-radius:14px;background:rgba(17,25,35,.84);box-shadow:0 20px 70px rgba(0,0,0,.25),inset 0 1px 0 rgba(255,255,255,.04);backdrop-filter:blur(16px);min-width:0}.side{padding:14px;position:sticky;top:14px}.side h2{margin:0 0 12px;font-size:15px;color:#c5d0dd}.date-list,.item-list{display:grid;gap:10px}.date-row,.item{border:1px solid rgba(255,255,255,.1);border-radius:12px;padding:12px;background:linear-gradient(180deg,rgba(255,255,255,.055),rgba(255,255,255,.025));cursor:pointer;transition:transform .28s cubic-bezier(.18,.89,.32,1.28),border-color .2s,background .2s,box-shadow .2s}.date-row:hover,.item:hover{transform:translateY(-2px);border-color:rgba(255,157,53,.42)}.date-row.active,.item.active{border-color:rgba(255,157,53,.75);box-shadow:0 0 0 1px rgba(255,157,53,.18),0 18px 36px rgba(255,157,53,.09);background:#203044}.idx{display:inline-grid;place-items:center;width:25px;height:25px;border-radius:50%;background:#263244;color:#dbe6f3;font-weight:950;margin-bottom:7px}.item-title{font-weight:950;line-height:1.35}.item-sub{color:var(--muted);font-size:12px;margin-top:8px}
    .main{padding:20px;min-height:560px}.title{font-size:30px;line-height:1.28;font-weight:950;margin:0 0 12px}.chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}.chip{border:1px solid rgba(255,255,255,.12);border-radius:999px;padding:6px 10px;color:#dbe6f3;font-weight:900}.chip.hot{border-color:rgba(255,157,53,.55);color:#ffd6a7}.cover{display:block;width:100%;max-height:68vh;object-fit:contain;background:#000;border:1px solid rgba(255,255,255,.14);border-radius:10px}.daily-video-shell{position:relative;display:grid;place-items:center;min-height:360px;background:#030406;border:1px solid rgba(255,255,255,.14);border-radius:10px;overflow:hidden}.daily-video{display:block;width:100%;max-height:68vh;background:#000}.daily-video-error{width:100%;min-height:360px;place-items:center;padding:28px;color:#ffb4ca;text-align:center}.daily-video-error:not([hidden]){display:grid}.empty-video{min-height:360px;display:grid;place-items:center;background:#030406;border-radius:10px;color:#7f8da0}.reader{white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere;line-height:1.78;color:#edf4ff;font-size:17px}
    .detail{padding:20px;max-height:calc(100vh - 150px);overflow:auto}.section{border-top:1px solid rgba(255,255,255,.1);padding-top:14px;margin-top:14px}.section:first-child{border-top:0;margin-top:0;padding-top:0}.section h2{margin:0 0 10px;font-size:24px;color:#f3f7ff}.section h3{margin:0 0 10px;font-size:16px;color:#dce7f5}.card-date{float:right;border:1px solid var(--line);border-radius:999px;padding:5px 11px;color:#cbd5e1;font-size:13px}.editable{white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere;line-height:1.72;font-size:16px;outline:0;min-height:180px}.editable:focus{box-shadow:0 0 0 2px rgba(255,157,53,.4);border-radius:8px}.link{color:var(--blue);word-break:break-all}.daily-table-wrap{overflow:auto}.daily-table{width:max-content;min-width:100%;border-collapse:collapse}.daily-table th,.daily-table td{border:1px solid rgba(255,255,255,.09);padding:10px;vertical-align:top;text-align:left;max-width:360px}.daily-table th{position:sticky;top:0;background:#1c2a3a;color:#c8d3e1}.daily-table tr{height:var(--row)}.timeline-scrim{position:fixed;inset:0;background:rgba(0,0,0,.42);opacity:0;pointer-events:none;transition:.25s;z-index:4}.timeline-scrim.open{opacity:1;pointer-events:auto}.timeline-drawer{position:fixed;right:18px;top:18px;bottom:18px;width:min(430px,calc(100vw - 36px));z-index:5;border:1px solid rgba(255,255,255,.14);border-radius:18px;background:rgba(13,20,30,.92);backdrop-filter:blur(24px);box-shadow:0 34px 90px rgba(0,0,0,.48);padding:18px;transform:translateX(calc(100% + 32px));transition:transform .48s cubic-bezier(.18,.89,.32,1.08);overflow:auto}.timeline-drawer.open{transform:translateX(0)}.timeline-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}.timeline-title{font-size:22px;font-weight:950}.timeline-list{display:grid;gap:10px}.timeline-day{border:1px solid rgba(255,255,255,.1);border-radius:13px;padding:13px;background:linear-gradient(180deg,rgba(255,255,255,.06),rgba(255,255,255,.025));cursor:pointer;transition:transform .28s cubic-bezier(.18,.89,.32,1.28),border-color .2s}.timeline-day:hover{transform:translateX(-4px);border-color:rgba(255,157,53,.55)}.timeline-day.active{border-color:rgba(255,157,53,.75);box-shadow:0 0 0 1px rgba(255,157,53,.18)}.timeline-day strong{display:block;font-size:18px}.timeline-day span{display:block;color:var(--muted);margin-top:4px}.hidden{display:none!important}.status{color:var(--muted);font-size:13px}.status.ok{color:#9df2bd}.status.error{color:#ffb4ca}@media(max-width:1180px){.layout{grid-template-columns:1fr}.side{position:static}.detail{max-height:none}.top{align-items:start;flex-direction:column}.toolbar{align-items:flex-start;flex-direction:column}}
  </style>
</head>
<body>
<canvas id="particleCanvas" aria-hidden="true"></canvas>
<main class="wrap">
  <header class="top">
    <div><div class="kicker">MAX DAILY INTEL</div><h1>外部情报口喷日报</h1><div class="meta" id="dateText">----</div></div>
    <div class="top-actions"><span class="count" id="count">0/0 条素材</span><button class="btn primary" id="publishCloudButton" onclick="publishCloudDaily()" disabled>发布到云端</button><button class="btn" id="openCloudButton" onclick="openCloudDaily()" disabled>打开云端日报</button><button class="btn ghost" id="copyCloudButton" onclick="copyCloudDailyLink()" disabled>复制链接</button><button class="btn" onclick="location.href='/'">返回采集助手</button><button class="btn" onclick="showTimeline()">日报时间轴</button></div>
  </header>
  <div class="cloud-publish-status" id="cloudPublishStatus" role="status" aria-live="polite">尚未发布到云端</div>
  <nav class="toolbar" aria-label="日报视图工具栏">
    <div class="tool-group"><button class="btn active" data-mode="work" onclick="setMode('work')">工作台</button><button class="btn" data-mode="video" onclick="setMode('video')">视频专注</button><button class="btn" data-mode="text" onclick="setMode('text')">文稿阅读</button><button class="btn" data-mode="table" onclick="setMode('table')">表格总览</button></div>
    <div class="tool-group"><button class="btn ghost" onclick="toggleTool('fields')">字段配置</button><button class="btn ghost" onclick="toggleTool('filter')">筛选</button><button class="btn ghost" onclick="toggleTool('sort')">排序</button><button class="btn ghost" onclick="toggleTool('height')">行高</button><button class="btn ghost" onclick="toggleTool('space')">调整空间</button><button class="btn primary" onclick="saveCurrentCard()">保存卡片</button><span class="status" id="status">正在加载...</span></div>
  </nav>
  <section class="tool-panel" id="toolPanel"></section>
  <section class="layout">
    <aside class="panel side"><h2>日期</h2><div class="date-list" id="dates"></div><h2 style="margin-top:16px">当天素材</h2><div class="item-list" id="items"></div></aside>
    <section class="panel main" id="main"></section>
    <aside class="panel detail" id="detail"></aside>
  </section>
</main>
<div class="timeline-scrim" id="timelineScrim" onclick="closeTimeline()"></div>
<aside class="timeline-drawer" id="timelineDrawer" aria-label="日报时间轴"></aside>
<script>
const params=new URLSearchParams(location.search);let tableId=params.get('table_id')||'';let dailyDate=params.get('date')||new Date().toISOString().slice(0,10);let daily={items:[],dates:[]};let current=null;let mode='work';let tool='';let fields=['source_url','platform','title','caption','cover_url','duration','likes','comments','shares','published_at','status','daily_date','max_daily_card','max_feedback'];let visibleFields=new Set(fields);let latestCloudReport=null;let cloudPublishTimer=null;let cloudPublishPolling=false;let cloudPublishPollQueued=false;let cloudPublishGeneration=0;let cloudPublishState='initializing';
const labels={source_url:'作品链接',platform:'平台',title:'作品标题',caption:'文案',cover_url:'封面图链接',duration:'时长',likes:'点赞',comments:'评论',shares:'分享',published_at:'发布时间',status:'抓取状态',daily_date:'日报日期',max_daily_card:'MAX口喷卡片',max_feedback:'Max反馈'};
function qs(s){return document.querySelector(s)}function esc(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}function num(v){return (v===null||v===undefined||v==='')?'':Number(v).toLocaleString('zh-CN')}
async function api(path,opts={}){const res=await fetch(path,{headers:{'Content-Type':'application/json'},...opts});if(!res.ok){let message=await res.text();try{const error=JSON.parse(message);message=error.error||error.message||message}catch(e){}throw new Error(message)}return res.json()}function setStatus(t,c=''){qs('#status').textContent=t;qs('#status').className='status '+c}
function isCurrentCloudPublishGeneration(generation){return generation===cloudPublishGeneration}
function clearCloudPublishTimer(){if(cloudPublishTimer){clearTimeout(cloudPublishTimer);cloudPublishTimer=null}}
function scheduleCloudPublishPoll(generation){clearCloudPublishTimer();cloudPublishTimer=setTimeout(()=>pollCloudPublish(generation),1000)}
function rememberCloudReport(report){if(report&&report.reportUrl)latestCloudReport={dailyDate:report.dailyDate,reportId:report.reportId,reportUrl:report.reportUrl,shareUrl:report.shareUrl||''}}
function renderCloudPublish(state={}){if(state.state)cloudPublishState=state.state;const running=cloudPublishState==='running';const initializing=cloudPublishState==='initializing';const status=qs('#cloudPublishStatus');const missingShare=latestCloudReport&&latestCloudReport.reportUrl&&!latestCloudReport.shareUrl;status.textContent=state.message||(latestCloudReport?(missingShare?'云端日报已可用，固定分享链接未配置':'云端日报已可用'):'尚未发布到云端');status.className='cloud-publish-status '+cloudPublishState;qs('#publishCloudButton').disabled=initializing||running;qs('#publishCloudButton').textContent=initializing?'正在恢复...':(running?'发布中...':'发布到云端');const available=Boolean(latestCloudReport&&latestCloudReport.reportUrl);const shareAvailable=Boolean(latestCloudReport&&latestCloudReport.shareUrl);qs('#openCloudButton').disabled=!available;qs('#copyCloudButton').disabled=!shareAvailable}
async function recoverCloudPublish(path){try{return {value:await api(path)}}catch(error){return {error:error}}}
async function initializeCloudPublish(){const generation=++cloudPublishGeneration;clearCloudPublishTimer();cloudPublishPollQueued=false;renderCloudPublish({state:'initializing',message:'正在恢复云端发布状态'});const [latestResult,statusResult]=await Promise.all([recoverCloudPublish('/api/cloud/latest'),recoverCloudPublish('/api/cloud/publish/status')]);if(!isCurrentCloudPublishGeneration(generation))return;if(latestResult.value)rememberCloudReport(latestResult.value);if(statusResult.value){const state=statusResult.value;rememberCloudReport(state);renderCloudPublish(state);if(state.state==='running')scheduleCloudPublishPoll(generation);else clearCloudPublishTimer();return}clearCloudPublishTimer();renderCloudPublish({state:'failed',message:'读取发布状态失败：'+statusResult.error.message})}
async function publishCloudDaily(){if(cloudPublishState==='initializing'||cloudPublishState==='running')return;const generation=++cloudPublishGeneration;clearCloudPublishTimer();cloudPublishPollQueued=false;renderCloudPublish({state:'running',message:'正在准备日报'});try{await api('/api/cloud/publish',{method:'POST',body:JSON.stringify({date:dailyDate})});if(!isCurrentCloudPublishGeneration(generation))return;pollCloudPublish(generation)}catch(error){if(!isCurrentCloudPublishGeneration(generation))return;clearCloudPublishTimer();renderCloudPublish({state:'failed',message:'发布失败：'+error.message})}}
async function pollCloudPublish(generation=cloudPublishGeneration){if(!isCurrentCloudPublishGeneration(generation))return;if(cloudPublishPolling){cloudPublishPollQueued=true;return}clearCloudPublishTimer();cloudPublishPolling=true;try{const state=await api('/api/cloud/publish/status');if(!isCurrentCloudPublishGeneration(generation))return;rememberCloudReport(state);renderCloudPublish(state);if(state.state==='running')scheduleCloudPublishPoll(generation);else clearCloudPublishTimer()}catch(error){if(!isCurrentCloudPublishGeneration(generation))return;clearCloudPublishTimer();renderCloudPublish({state:'failed',message:'读取发布状态失败：'+error.message})}finally{cloudPublishPolling=false;if(cloudPublishPollQueued){cloudPublishPollQueued=false;if(cloudPublishState==='running')pollCloudPublish(cloudPublishGeneration)}}}
function openCloudDaily(){if(!latestCloudReport||!latestCloudReport.reportUrl)return;window.open(latestCloudReport.reportUrl,'_blank','noopener')}
function copyCloudDailyLinkFallback(reportUrl){const textarea=document.createElement('textarea');textarea.value=reportUrl;textarea.setAttribute('readonly','');textarea.style.position='fixed';textarea.style.opacity='0';document.body.appendChild(textarea);let copied=false;try{textarea.select();textarea.setSelectionRange(0,textarea.value.length);copied=document.execCommand('copy')}catch(error){}finally{document.body.removeChild(textarea)}return copied}
async function copyCloudDailyLink(){if(!latestCloudReport||!latestCloudReport.shareUrl)return;const generation=cloudPublishGeneration;const shareUrl=latestCloudReport.shareUrl;let copied=false;try{if(navigator.clipboard&&typeof navigator.clipboard.writeText==='function'){await navigator.clipboard.writeText(shareUrl);copied=true}}catch(error){}if(!isCurrentCloudPublishGeneration(generation)||cloudPublishState==='running')return;if(!copied)copied=copyCloudDailyLinkFallback(shareUrl);if(!isCurrentCloudPublishGeneration(generation)||cloudPublishState==='running')return;renderCloudPublish(copied?{state:'succeeded',message:'固定免登录链接已复制'}:{state:'failed',message:'复制失败，请重新点击“复制链接”'})}
async function loadDaily(){const q=new URLSearchParams({date:dailyDate});if(tableId)q.set('table_id',tableId);daily=await api('/api/daily?'+q.toString());dailyDate=daily.date;qs('#dateText').textContent=dailyDate;qs('#count').textContent=`${daily.count}/${daily.count} 条素材`;renderDates();selectItem((daily.items||[])[0]||null);renderToolPanel();setStatus('已加载','ok')}
function renderDates(){const dates=daily.dates&&daily.dates.length?daily.dates:[{date:dailyDate,count:daily.count||0}];qs('#dates').innerHTML=dates.map(d=>`<div class="date-row ${d.date===dailyDate?'active':''}" onclick="dailyDate='${d.date}';loadDaily()"><strong>${esc(d.date)}</strong><br><span class="status">${d.count} 条素材</span></div>`).join('')}
function renderItems(){qs('#items').innerHTML=(daily.items||[]).map((it,i)=>`<div class="item ${current&&current.id===it.id?'active':''}" onclick="selectItemById('${it.id}')"><span class="idx">${i+1}</span><div class="item-title">${esc(it.title||'未命名素材')}</div><div class="item-sub">${esc(it.platform||'')} · ${esc(it.duration||'')} · 点赞 ${num(it.likes)}</div></div>`).join('')||'<p class="status">当天还没有加入日报的素材。</p>'}
function selectItemById(id){selectItem((daily.items||[]).find(x=>x.id===id)||null)}function selectItem(it){current=it;renderItems();renderMain();renderDetail()}
function setMode(next){mode=next;document.querySelectorAll('[data-mode]').forEach(b=>b.classList.toggle('active',b.dataset.mode===mode));renderMain();renderDetail()}
function dailyVideoHtml(item){if(!item||!item.video_path)return '<div class="empty-video">这条素材尚未下载视频</div>';const src='/api/daily/video?item_id='+encodeURIComponent(item.id);const poster=item.cover_url?'/api/cover?url='+encodeURIComponent(item.cover_url):'';return `<div class="daily-video-shell"><video class="daily-video" controls playsinline preload="metadata" src="${src}" ${poster?`poster="${poster}"`:''} onerror="handleDailyVideoError(this)"></video><div class="daily-video-error" hidden>本地视频文件不存在或无法播放，请重新下载</div></div>`}
function handleDailyVideoError(video){if(!video)return;video.hidden=true;const message=video.nextElementSibling;if(message)message.hidden=false}
function renderMain(){if(!current){qs('#main').innerHTML='<div class="empty-video">今天还没有加入 Max 日报的素材</div>';return}if(mode==='table'){renderTable();return}const text=`<div class="reader">${esc(current.caption||current.error||'暂无文稿')}</div>`;const media=mode==='text'?text:dailyVideoHtml(current);qs('#main').innerHTML=`<h2 class="title">${esc(current.title||'未命名素材')}</h2><div class="chips"><span class="chip hot">${esc(current.platform||'素材')}</span><span class="chip">点赞 ${num(current.likes)}</span><span class="chip">评论 ${num(current.comments)}</span><span class="chip">分享 ${num(current.shares)}</span><span class="chip">${esc(current.published_at||'')}</span></div>${media}`}
function renderDetail(){if(mode==='table'){qs('#detail').innerHTML=`<section class="section"><h2>表格总览</h2><div class="status">字段显示可在“字段配置”里调整。</div></section>`;return}if(!current){qs('#detail').innerHTML='';return}qs('#detail').innerHTML=`<section class="section"><h2>MAX口喷卡片 <span class="card-date">${esc(dailyDate)}</span></h2><div class="status">外部情报口喷卡片</div><div class="editable" id="cardBody" contenteditable="true" spellcheck="false">${esc(current.max_daily_card||'还没有生成口喷卡片')}</div></section><section class="section"><h3>来源链接</h3><a class="link" href="${esc(current.source_url||'#')}" target="_blank">${esc(current.source_url||'')}</a></section><section class="section"><h3>原始文案 / 逐字稿</h3><div class="reader">${esc(current.caption||'')}</div></section>`}
function renderTable(){const rows=daily.items||[];const cols=fields.filter(f=>visibleFields.has(f));qs('#main').innerHTML=`<div class="daily-table-wrap"><table class="daily-table"><thead><tr>${cols.map(f=>`<th>${labels[f]}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${cols.map(f=>`<td>${esc(r[f]??'')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`}
function toggleTool(next){tool=tool===next?'':next;renderToolPanel()}function renderToolPanel(){const p=qs('#toolPanel');if(!tool){p.className='tool-panel';p.innerHTML='';return}p.className='tool-panel open';if(tool==='fields')p.innerHTML=`<div class="panel-head"><div><strong>字段配置</strong><div class="hint">和飞书多维表字段对齐，控制“表格总览”列显示。</div></div><div><button class="btn" onclick="fields.forEach(f=>visibleFields.add(f));renderToolPanel();renderMain()">显示全部</button><button class="btn" onclick="visibleFields=new Set(['source_url','platform','title','caption','status','daily_date','max_daily_card']);renderToolPanel();renderMain()">日报字段</button></div></div><div class="checks">${fields.map(f=>`<label><input type="checkbox" ${visibleFields.has(f)?'checked':''} onchange="this.checked?visibleFields.add('${f}'):visibleFields.delete('${f}');renderMain()"> ${labels[f]}</label>`).join('')}</div>`;else if(tool==='filter')p.innerHTML=`<div class="panel-head"><div><strong>筛选</strong><div class="hint">按标题、文案、平台或状态过滤当天素材。</div></div></div><div class="control-grid"><label class="control-line">关键词<input id="filterInput" oninput="filterRows(this.value)" placeholder="搜索日报素材"></label></div>`;else if(tool==='sort')p.innerHTML=`<div class="panel-head"><div><strong>排序</strong><div class="hint">调整当天素材阅读顺序。</div></div></div><div class="control-grid"><label class="control-line">排序字段<select onchange="sortRows(this.value)"><option value="daily_sort">日报顺序</option><option value="likes">点赞</option><option value="comments">评论</option><option value="shares">分享</option><option value="published_at">发布时间</option></select></label></div>`;else if(tool==='height')p.innerHTML=`<div class="panel-head"><div><strong>行高</strong><div class="hint">控制“表格总览”的单行高度。</div></div></div><div class="control-grid"><label class="control-line">行高滑块<input id="rowRange" type="range" min="56" max="180" value="88" data-css-var="--row" data-unit="px"></label></div>`;else p.innerHTML=`<div class="panel-head"><div><strong>调整空间</strong><div class="hint">拖动滑块调整左侧素材栏和右侧口喷卡片宽度。</div></div></div><div class="control-grid"><label class="control-line">左侧宽度<input id="spaceRange" type="range" min="240" max="420" value="300" data-css-var="--side" data-unit="px"></label><label class="control-line">卡片宽度<input id="detailRange" type="range" min="420" max="760" value="560" data-css-var="--detail" data-unit="px"></label></div>`;bindSpringSlider()}
function filterRows(q){q=String(q||'').toLowerCase();daily.items=(daily.items||[]).filter(it=>!q||['title','caption','platform','status','max_daily_card'].some(k=>String(it[k]||'').toLowerCase().includes(q)));renderItems();renderMain()}
function sortRows(field){daily.items=[...(daily.items||[])].sort((a,b)=>String(b[field]??'').localeCompare(String(a[field]??''),'zh-Hans-CN',{numeric:true}));renderItems();renderMain()}
async function saveCurrentCard(){if(!current||!qs('#cardBody'))return;try{current=await api('/api/daily/update',{method:'POST',body:JSON.stringify({item_id:current.id,updates:{max_daily_card:qs('#cardBody').innerText}})});const idx=(daily.items||[]).findIndex(x=>x.id===current.id);if(idx>=0)daily.items[idx]=current;setStatus('已保存','ok')}catch(e){setStatus('保存失败：'+e.message,'error')}}
function bindSpringSlider(){document.querySelectorAll('input[type="range"][data-css-var]').forEach(input=>{if(input.dataset.bound)return;input.dataset.bound='1';let currentValue=Number(input.value),target=currentValue,velocity=0;const key=input.dataset.cssVar,unit=input.dataset.unit||'';function tick(){const stiffness=.16,damping=.68;velocity+=(target-currentValue)*stiffness;velocity*=damping;currentValue+=velocity;document.documentElement.style.setProperty(key,currentValue.toFixed(2)+unit);if(Math.abs(target-currentValue)>.05||Math.abs(velocity)>.05)requestAnimationFrame(tick);else{currentValue=target;document.documentElement.style.setProperty(key,target+unit)}}input.addEventListener('input',()=>{target=Number(input.value);requestAnimationFrame(tick)})})}
function renderTimeline(){const dates=daily.dates&&daily.dates.length?daily.dates:[{date:dailyDate,count:daily.count||0}];qs('#timelineDrawer').innerHTML=`<div class="timeline-head"><div><div class="kicker">DAILY TIMELINE</div><div class="timeline-title">日报时间轴</div></div><button class="btn" onclick="closeTimeline()">关闭</button></div><div class="timeline-list">${dates.map(d=>`<div class="timeline-day ${d.date===dailyDate?'active':''}" onclick="dailyDate='${d.date}';closeTimeline();loadDaily()"><strong>${esc(d.date)}</strong><span>${d.count} 条素材</span></div>`).join('')}</div>`}
function showTimeline(){renderTimeline();qs('#timelineDrawer').classList.add('open');qs('#timelineScrim').classList.add('open')}
function closeTimeline(){qs('#timelineDrawer').classList.remove('open');qs('#timelineScrim').classList.remove('open')}
function startParticles(){const canvas=qs('#particleCanvas');if(!canvas)return;const ctx=canvas.getContext('2d');let w=0,h=0,parts=[];function resize(){w=canvas.width=innerWidth*devicePixelRatio;h=canvas.height=innerHeight*devicePixelRatio;canvas.style.width=innerWidth+'px';canvas.style.height=innerHeight+'px';const count=Math.min(120,Math.max(46,Math.floor(innerWidth/14)));parts=Array.from({length:count},()=>({x:Math.random()*w,y:Math.random()*h,vx:(Math.random()-.5)*.18*devicePixelRatio,vy:(Math.random()-.5)*.18*devicePixelRatio,r:(Math.random()*1.6+0.5)*devicePixelRatio,a:Math.random()*.55+.18}))}function draw(){ctx.clearRect(0,0,w,h);for(const p of parts){p.x+=p.vx;p.y+=p.vy;if(p.x<0||p.x>w)p.vx*=-1;if(p.y<0||p.y>h)p.vy*=-1;ctx.beginPath();ctx.fillStyle=`rgba(255,190,112,${p.a})`;ctx.arc(p.x,p.y,p.r,0,Math.PI*2);ctx.fill()}for(let i=0;i<parts.length;i++){for(let j=i+1;j<parts.length;j++){const a=parts[i],b=parts[j],dx=a.x-b.x,dy=a.y-b.y,d=Math.hypot(dx,dy),max=130*devicePixelRatio;if(d<max){ctx.strokeStyle=`rgba(124,199,255,${(1-d/max)*.12})`;ctx.lineWidth=devicePixelRatio*.7;ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke()}}}requestAnimationFrame(draw)}resize();addEventListener('resize',resize);draw()}
startParticles();initializeCloudPublish();loadDaily().catch(e=>setStatus('加载失败：'+e.message,'error'));
</script>
</body>
</html>"""


def desktop_json_response(handler: http.server.BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def desktop_text_response(
    handler: http.server.BaseHTTPRequestHandler,
    body: str,
    content_type: str = "text/html; charset=utf-8",
    status: int = 200,
) -> None:
    data = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def desktop_parse_byte_range(value: str, size: int) -> Tuple[int, int]:
    text = str(value or "").strip()
    if size <= 0 or not text.startswith("bytes=") or "," in text:
        raise ValueError("无效的视频读取范围")
    bounds = text[6:]
    if "-" not in bounds:
        raise ValueError("无效的视频读取范围")
    start_text, end_text = bounds.split("-", 1)
    if not start_text and not end_text:
        raise ValueError("无效的视频读取范围")
    try:
        if not start_text:
            suffix = int(end_text)
            if suffix <= 0:
                raise ValueError
            start = max(0, size - suffix)
            return start, size - 1
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    except ValueError as error:
        raise ValueError("无效的视频读取范围") from error
    if start < 0 or start >= size or end < start:
        raise ValueError("无效的视频读取范围")
    return start, min(end, size - 1)


def desktop_video_response(
    handler: http.server.BaseHTTPRequestHandler,
    path: Path,
    range_header: str = "",
) -> None:
    target = Path(path)
    size = target.stat().st_size
    start, end = (0, size - 1)
    status = 200
    if range_header:
        start, end = desktop_parse_byte_range(range_header, size)
        status = 206
    length = end - start + 1
    content_type = mimetypes.guess_type(str(target))[0] or "video/mp4"
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(length))
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Cache-Control", "no-store")
    if status == 206:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    handler.end_headers()
    with target.open("rb") as source:
        source.seek(start)
        remaining = length
        while remaining:
            chunk = source.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            handler.wfile.write(chunk)
            remaining -= len(chunk)


def desktop_version_payload(manifest_path: Path = DEPLOYMENT_MANIFEST_PATH) -> Dict[str, Any]:
    path = Path(manifest_path)
    if not path.is_file():
        return {"ok": False, "commit": "", "script_sha256": "", "error": "尚未写入部署清单"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {"ok": False, "commit": "", "script_sha256": "", "error": str(error)}
    payload = dict(payload) if isinstance(payload, dict) else {}
    payload["ok"] = bool(payload.get("commit") and payload.get("script_sha256"))
    return payload


def desktop_read_json(handler: http.server.BaseHTTPRequestHandler) -> Dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if not length:
        return {}
    raw = handler.rfile.read(length).decode("utf-8")
    return json.loads(raw or "{}")


CLOUD_PUBLISH_MAX_BODY_BYTES = 64 * 1024
CLOUD_PUBLISH_LOCAL_ONLY_ERROR = "云端发布接口只允许本机访问"
SETUP_LOCAL_ONLY_ERROR = "首次设置接口只允许本机访问"
SETUP_UNTRUSTED_ORIGIN_ERROR = "首次设置请求来源不受信任"
SETUP_JSON_ONLY_ERROR = "首次设置只接受同源 JSON 请求"


def desktop_is_loopback_client(handler: http.server.BaseHTTPRequestHandler) -> bool:
    client_host = (handler.client_address or ("",))[0]
    try:
        address = ipaddress.ip_address(str(client_host))
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped is not None and mapped.is_loopback)


def desktop_reject_non_loopback_cloud_client(
    handler: http.server.BaseHTTPRequestHandler,
    path: str,
) -> bool:
    is_cloud_path = path.startswith("/api/cloud/")
    is_setup_path = path.startswith("/api/setup/")
    if not is_cloud_path and not is_setup_path:
        return False
    if desktop_is_loopback_client(handler):
        return False
    message = SETUP_LOCAL_ONLY_ERROR if is_setup_path else CLOUD_PUBLISH_LOCAL_ONLY_ERROR
    desktop_json_response(handler, {"error": message}, status=403)
    return True


def desktop_setup_server_origin(
    handler: http.server.BaseHTTPRequestHandler,
) -> Optional[Tuple[str, str]]:
    server_address = getattr(getattr(handler, "server", None), "server_address", None)
    if not isinstance(server_address, tuple) or len(server_address) < 2:
        return None
    try:
        address = ipaddress.ip_address(str(server_address[0]))
        port = int(server_address[1])
    except (TypeError, ValueError):
        return None
    if not address.is_loopback or port <= 0 or port > 65535:
        return None
    host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    authority = f"{host}:{port}"
    return authority, f"http://{authority}"


def desktop_reject_untrusted_setup_mutation(
    handler: http.server.BaseHTTPRequestHandler,
) -> bool:
    server_origin = desktop_setup_server_origin(handler)
    host_values = handler.headers.get_all("Host") or []
    if (
        server_origin is None
        or len(host_values) != 1
        or host_values[0].strip() != server_origin[0]
    ):
        desktop_json_response(
            handler,
            {"error": SETUP_UNTRUSTED_ORIGIN_ERROR},
            status=403,
        )
        return True

    content_type_values = handler.headers.get_all("Content-Type") or []
    if (
        len(content_type_values) != 1
        or content_type_values[0].split(";", 1)[0].strip().lower() != "application/json"
    ):
        desktop_json_response(
            handler,
            {"error": SETUP_JSON_ONLY_ERROR},
            status=415,
        )
        return True

    origin_values = handler.headers.get_all("Origin") or []
    if len(origin_values) > 1 or (
        origin_values and origin_values[0].strip() != server_origin[1]
    ):
        desktop_json_response(
            handler,
            {"error": SETUP_UNTRUSTED_ORIGIN_ERROR},
            status=403,
        )
        return True

    fetch_site_values = handler.headers.get_all("Sec-Fetch-Site") or []
    if len(fetch_site_values) > 1 or (
        fetch_site_values
        and fetch_site_values[0].strip().lower()
        not in {"same-origin", "same-site", "none"}
    ):
        desktop_json_response(
            handler,
            {"error": SETUP_UNTRUSTED_ORIGIN_ERROR},
            status=403,
        )
        return True
    return False


def desktop_read_cloud_publish_json(handler: http.server.BaseHTTPRequestHandler) -> Dict[str, Any]:
    content_length = handler.headers.get("Content-Length")
    if (
        not content_length
        or any(char < "0" or char > "9" for char in content_length)
    ):
        raise ValueError("invalid cloud publish content length")
    length = int(content_length)
    if length <= 0 or length > CLOUD_PUBLISH_MAX_BODY_BYTES:
        raise ValueError("invalid cloud publish content length")
    raw = handler.rfile.read(length)
    if len(raw) != length:
        raise ValueError("incomplete cloud publish body")
    return json.loads(raw.decode("utf-8"))


def desktop_read_setup_json(handler: http.server.BaseHTTPRequestHandler) -> Dict[str, Any]:
    return desktop_read_cloud_publish_json(handler)


DESKTOP_EXPORT_COLUMNS = [
    ("platform", "平台"),
    ("source_url", "作品链接"),
    ("title", "作品标题"),
    ("caption", "文案/逐字稿"),
    ("cover_url", "封面图链接"),
    ("duration", "时长"),
    ("likes", "点赞"),
    ("comments", "评论"),
    ("shares", "分享"),
    ("published_at", "发布时间"),
    ("status", "状态"),
    ("error", "错误信息"),
]


def desktop_export_rows(db_path: Path, table_id: str) -> List[Dict[str, Any]]:
    return desktop_list_items(db_path, table_id)


def desktop_default_table_id(db_path: Path) -> str:
    tables = desktop_list_tables(db_path)
    return str((tables[0] or {}).get("id") or "") if tables else ""


def desktop_export_csv(db_path: Path, table_id: str) -> bytes:
    from io import StringIO

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([label for _, label in DESKTOP_EXPORT_COLUMNS])
    for item in desktop_export_rows(db_path, table_id):
        writer.writerow([item.get(key) if item.get(key) is not None else "" for key, _ in DESKTOP_EXPORT_COLUMNS])
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def desktop_export_markdown(db_path: Path, table_id: str) -> bytes:
    lines = ["# CHEN 内容采集表", ""]
    rows = desktop_export_rows(db_path, table_id)
    if not rows:
        lines.append("当前表格暂无采集结果。")
    for index, item in enumerate(rows, 1):
        lines.extend([
            f"## {index}. {item.get('title') or '未命名作品'}",
            "",
            f"- 平台：{item.get('platform') or ''}",
            f"- 类型：{'主页批量' if item.get('source_type') == 'profile' else '单作品'}",
            f"- 作品链接：{item.get('source_url') or ''}",
            f"- 封面图链接：{item.get('cover_url') or ''}",
            f"- 时长：{item.get('duration') or ''}",
            f"- 点赞：{item.get('likes') if item.get('likes') is not None else ''}",
            f"- 评论：{item.get('comments') if item.get('comments') is not None else ''}",
            f"- 分享：{item.get('shares') if item.get('shares') is not None else ''}",
            f"- 发布时间：{item.get('published_at') or ''}",
            f"- 状态：{item.get('status') or ''}",
            "",
            "### 文案/逐字稿",
            "",
            item.get("caption") or "",
        ])
        if item.get("error"):
            lines.extend(["", "### 备注", "", item.get("error") or ""])
        lines.append("")
    return "\n".join(lines).encode("utf-8")


def desktop_table_name(db_path: Path, table_id: str) -> str:
    for table in desktop_list_tables(db_path):
        if table.get("id") == table_id:
            return str(table.get("name") or "CHEN内容采集表")
    return "CHEN内容采集表"


def desktop_metric_text(item: Dict[str, Any]) -> str:
    parts = []
    for key, label in (("likes", "赞"), ("comments", "评"), ("shares", "转")):
        value = item.get(key)
        if value is not None and value != "":
            parts.append(f"{label} {value}")
    return " / ".join(parts) if parts else "暂无互动数据"


def desktop_caption_excerpt(text: Any, limit: int = 700) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip() + "..."


def desktop_build_oral_daily_body(db_path: Path, table_id: str) -> str:
    rows = desktop_export_rows(db_path, table_id)
    ready = [item for item in rows if str(item.get("status") or "") in {"成功", "图文作品", "需ASR"}]
    blocked = [item for item in rows if item not in ready]
    ready.sort(
        key=lambda item: (
            int(item.get("likes") or 0) + int(item.get("comments") or 0) * 3 + int(item.get("shares") or 0) * 5,
            str(item.get("updated_at") or ""),
        ),
        reverse=True,
    )
    lines = [
        f"生成时间：{now_text()}",
        f"素材表：{desktop_table_name(db_path, table_id)}",
        "",
        "一、今天先让 Max 看的外部情报",
        "",
    ]
    if not ready:
        lines.append("今天还没有可直接口喷的外部情报。")
    for index, item in enumerate(ready, 1):
        lines.extend(
            [
                f"{index}. {item.get('title') or '未命名情报'}",
                f"平台：{item.get('platform') or ''}",
                f"链接：{item.get('source_url') or ''}",
                f"互动：{desktop_metric_text(item)}",
                f"发布时间：{item.get('published_at') or ''}",
                "原始材料：",
                desktop_caption_excerpt(item.get("caption") or item.get("error") or "", 1200) or "暂无文案/逐字稿。",
                "",
                "Max 可直接改这里：",
                "- 这个情报最值得讲的点：",
                "- 可以口喷的判断：",
                "- 需要追问或补证的地方：",
                "",
            ]
        )
    lines.extend(["二、还不能直接给 Max 讲的素材", ""])
    if not blocked:
        lines.append("没有需要补救的素材。")
    for index, item in enumerate(blocked, 1):
        lines.extend(
            [
                f"{index}. {item.get('title') or item.get('source_url') or '未命名素材'}",
                f"状态：{item.get('status') or ''}",
                f"原因：{item.get('error') or '暂无备注'}",
                f"链接：{item.get('source_url') or ''}",
                "",
            ]
        )
    return "\n".join(lines).strip()


def desktop_save_daily_report(db_path: Path, table_id: str, title: str, body: str) -> Dict[str, Any]:
    desktop_db_init(db_path)
    table_id = table_id or desktop_default_table_id(db_path)
    if not table_id:
        raise ValueError("缺少 table_id")
    safe_title = str(title or "").strip() or "外部情报口喷日报"
    safe_body = str(body or "")
    ts = now_text()
    report_id = str(uuid.uuid4())
    with desktop_connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO daily_reports(id, table_id, title, body, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(table_id) DO UPDATE SET
                title = excluded.title,
                body = excluded.body,
                updated_at = excluded.updated_at
            """,
            (report_id, table_id, safe_title, safe_body, ts, ts),
        )
        row = conn.execute("SELECT * FROM daily_reports WHERE table_id = ?", (table_id,)).fetchone()
    report = desktop_row_to_dict(row)
    report["table_name"] = desktop_table_name(db_path, table_id)
    report["url"] = f"/daily?table_id={urllib.parse.quote(table_id)}"
    return report


def desktop_get_daily_report(db_path: Path, table_id: str = "") -> Dict[str, Any]:
    desktop_db_init(db_path)
    table_id = table_id or desktop_default_table_id(db_path)
    if not table_id:
        raise ValueError("没有可生成日报的采集表。")
    with desktop_connect(db_path) as conn:
        row = conn.execute("SELECT * FROM daily_reports WHERE table_id = ?", (table_id,)).fetchone()
    if row:
        report = desktop_row_to_dict(row)
        report["table_name"] = desktop_table_name(db_path, table_id)
        report["url"] = f"/daily?table_id={urllib.parse.quote(table_id)}"
        return report
    return desktop_save_daily_report(
        db_path,
        table_id,
        "外部情报口喷日报",
        desktop_build_oral_daily_body(db_path, table_id),
    )


def desktop_regenerate_daily_report(db_path: Path, table_id: str) -> Dict[str, Any]:
    return desktop_save_daily_report(
        db_path,
        table_id,
        "外部情报口喷日报",
        desktop_build_oral_daily_body(db_path, table_id),
    )


def desktop_export_max_daily(db_path: Path, table_id: str) -> bytes:
    table_name = desktop_table_name(db_path, table_id)
    rows = desktop_export_rows(db_path, table_id)
    ready = [item for item in rows if str(item.get("status") or "") in {"成功", "图文作品", "需ASR"}]
    blocked = [item for item in rows if item not in ready]
    ready.sort(
        key=lambda item: (
            int(item.get("likes") or 0) + int(item.get("comments") or 0) * 3 + int(item.get("shares") or 0) * 5,
            str(item.get("updated_at") or ""),
        ),
        reverse=True,
    )
    status_counts: Dict[str, int] = {}
    for item in rows:
        status = str(item.get("status") or "未标记")
        status_counts[status] = status_counts.get(status, 0) + 1

    lines = [
        "# MAX 日报",
        "",
        f"- 生成时间：{now_text()}",
        f"- 采集表：{table_name}",
        f"- 素材总数：{len(rows)}",
        f"- 可直接阅读：{len(ready)}",
        f"- 需要补救：{len(blocked)}",
        "",
        "## 今日状态",
        "",
    ]
    if status_counts:
        for status, count in sorted(status_counts.items(), key=lambda pair: (-pair[1], pair[0])):
            lines.append(f"- {status}：{count}")
    else:
        lines.append("- 当前没有素材。")

    lines.extend(["", "## 给 Max 的阅读顺序", ""])
    if not ready:
        lines.append("今天还没有可直接交给 Max 阅读的素材。")
    for index, item in enumerate(ready, 1):
        caption = desktop_caption_excerpt(item.get("caption") or item.get("error") or "")
        lines.extend(
            [
                f"### {index}. {item.get('title') or '未命名作品'}",
                "",
                f"- 平台：{item.get('platform') or ''}",
                f"- 作品链接：{item.get('source_url') or ''}",
                f"- 互动：{desktop_metric_text(item)}",
                f"- 发布时间：{item.get('published_at') or ''}",
                f"- 状态：{item.get('status') or ''}",
                "",
                "#### 给 Max 先看的原始材料",
                "",
                caption or "暂无文案/逐字稿。",
                "",
            ]
        )

    lines.extend(["## 需要补救的素材", ""])
    if not blocked:
        lines.append("没有需要补救的素材。")
    for index, item in enumerate(blocked, 1):
        lines.extend(
            [
                f"{index}. {item.get('title') or item.get('source_url') or '未命名作品'}",
                f"   - 平台：{item.get('platform') or ''}",
                f"   - 状态：{item.get('status') or ''}",
                f"   - 原因：{item.get('error') or '暂无备注'}",
                f"   - 链接：{item.get('source_url') or ''}",
            ]
        )

    lines.extend(
        [
            "",
            "## 使用方式",
            "",
            "把这份 Markdown 直接发给 Max。先看“给 Max 的阅读顺序”，遇到需要补救的素材再回采集助手处理登录、转写或下载问题。",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def desktop_export_json(db_path: Path, table_id: str) -> bytes:
    return json.dumps(desktop_export_rows(db_path, table_id), ensure_ascii=False, indent=2).encode("utf-8")


def desktop_export_bytes(db_path: Path, table_id: str, fmt: str) -> Tuple[bytes, str, str]:
    fmt = (fmt or "csv").lower()
    if fmt in {"max-daily", "max_daily", "max", "daily"}:
        return desktop_export_max_daily(db_path, table_id), "text/markdown; charset=utf-8", ".md"
    if fmt == "markdown":
        return desktop_export_markdown(db_path, table_id), "text/markdown; charset=utf-8", ".md"
    if fmt == "json":
        return desktop_export_json(db_path, table_id), "application/json; charset=utf-8", ".json"
    return desktop_export_csv(db_path, table_id), "text/csv; charset=utf-8", ".csv"


def desktop_export_default_name(db_path: Path, table_id: str, ext: str, fmt: str = "") -> str:
    table_name = desktop_table_name(db_path, table_id)
    safe = re.sub(r'[\\/:*?"<>|\n\r]+', "_", table_name).strip(" .") or "CHEN内容采集表"
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    if (fmt or "").lower() in {"max-daily", "max_daily", "max", "daily"}:
        return f"MAX日报-{safe}-{stamp}{ext}"
    return f"{safe}-{stamp}{ext}"


def desktop_choose_save_path(default_name: str) -> Path:
    script = (
        'POSIX path of (choose file name with prompt "选择导出保存位置" '
        f'default name "{default_name.replace(chr(34), "_")}")'
    )
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("已取消保存。")
    path = result.stdout.strip()
    if not path:
        raise ValueError("没有选择保存位置。")
    return Path(path)


def desktop_save_export_file(
    db_path: Path,
    table_id: str,
    fmt: str,
    output_path: Optional[Path] = None,
) -> Dict[str, Any]:
    if not table_id:
        raise ValueError("缺少 table_id")
    data, content_type, ext = desktop_export_bytes(db_path, table_id, fmt)
    target = output_path or desktop_choose_save_path(desktop_export_default_name(db_path, table_id, ext, fmt))
    if target.suffix.lower() != ext:
        target = target.with_suffix(ext)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"path": str(target), "format": ext.lstrip("."), "content_type": content_type, "bytes": len(data)}


def desktop_setting_get(db_path: Path, key: str, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    desktop_db_init(db_path)
    with desktop_connect(db_path) as conn:
        row = conn.execute("SELECT value_json FROM desktop_settings WHERE key = ?", (key,)).fetchone()
    if not row:
        return dict(default or {})
    try:
        return json.loads(row["value_json"] or "{}")
    except json.JSONDecodeError:
        return dict(default or {})


def desktop_setting_set(db_path: Path, key: str, value: Dict[str, Any]) -> Dict[str, Any]:
    state = dict(value or {})
    state["updated_at"] = now_text()
    desktop_db_init(db_path)
    with desktop_connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO desktop_settings(key, value_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
            """,
            (key, json.dumps(state, ensure_ascii=False), state["updated_at"]),
        )
    return state


VIDEO_DIRECTORY_SETTING_KEYS = {
    "single": "single_video_download_directory",
    "batch": "batch_video_download_directory",
}


def desktop_default_video_directory() -> Path:
    return Path.home() / "Desktop" / "CHEN内容采集助手视频"


def desktop_validate_video_directory(path: Path) -> Path:
    target = Path(path).expanduser()
    if target.exists() and not target.is_dir():
        raise ValueError("所选位置不是文件夹。")
    try:
        target.mkdir(parents=True, exist_ok=True)
    except PermissionError as e:
        raise ValueError("所选文件夹没有写入权限。") from e
    except OSError as e:
        raise ValueError(f"文件夹不存在或不可创建：{e}") from e
    if not os.access(str(target), os.W_OK):
        raise ValueError("所选文件夹没有写入权限。")
    return target.resolve()


def desktop_video_download_settings(db_path: Path) -> Dict[str, str]:
    default = str(desktop_default_video_directory())
    single = desktop_setting_get(db_path, VIDEO_DIRECTORY_SETTING_KEYS["single"], {"path": default})
    batch = desktop_setting_get(db_path, VIDEO_DIRECTORY_SETTING_KEYS["batch"], {"path": default})
    return {
        "single_directory": str(single.get("path") or default),
        "batch_directory": str(batch.get("path") or default),
    }


def desktop_set_video_download_directory(db_path: Path, kind: str, path: Path) -> Dict[str, str]:
    if kind not in VIDEO_DIRECTORY_SETTING_KEYS:
        raise ValueError("未知的视频下载目录类型。")
    target = desktop_validate_video_directory(path)
    desktop_setting_set(db_path, VIDEO_DIRECTORY_SETTING_KEYS[kind], {"path": str(target)})
    return desktop_video_download_settings(db_path)


def desktop_choose_video_directory(db_path: Path, kind: str) -> Dict[str, str]:
    current = Path(desktop_video_download_settings(db_path)[f"{kind}_directory"])
    script = 'POSIX path of (choose folder with prompt "选择视频下载文件夹" default location POSIX file ' + json.dumps(str(current)) + ')'
    result = subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True)
    return desktop_set_video_download_directory(db_path, kind, Path(result.stdout.strip()))


def desktop_open_video_directory(db_path: Path, kind: str) -> Dict[str, Any]:
    directory = Path(desktop_video_download_settings(db_path)[f"{kind}_directory"])
    directory = desktop_validate_video_directory(directory)
    subprocess.run(["open", str(directory)], check=True)
    return {"ok": True, "path": str(directory)}


def desktop_create_download_batch(db_path: Path, item_ids: List[str], mode: str = "batch") -> Dict[str, Any]:
    if mode not in {"single", "batch"}:
        raise ValueError("未知的视频下载模式。")
    unique_ids = list(dict.fromkeys(str(item_id) for item_id in item_ids if str(item_id)))
    if not unique_ids:
        raise ValueError("请先选择要下载的视频。")
    settings = desktop_video_download_settings(db_path)
    directory = settings[f"{mode}_directory"]
    batch_id = str(uuid.uuid4())
    ts = now_text()
    tasks: List[Dict[str, Any]] = []
    with desktop_connect(db_path) as conn:
        conn.execute(
            "INSERT INTO video_download_batches(id, mode, directory, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (batch_id, mode, directory, ts, ts),
        )
        for item_id in unique_ids:
            if conn.execute("SELECT 1 FROM collected_items WHERE id = ?", (item_id,)).fetchone() is None:
                continue
            active = conn.execute(
                "SELECT * FROM video_download_tasks WHERE item_id = ? AND status IN ('queued','preparing','downloading','merging') ORDER BY created_at DESC LIMIT 1",
                (item_id,),
            ).fetchone()
            if active:
                tasks.append(desktop_row_to_dict(active))
                continue
            task_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO video_download_tasks(id, batch_id, item_id, status, stage, created_at, updated_at)
                VALUES (?, ?, ?, 'queued', '等待下载', ?, ?)
                """,
                (task_id, batch_id, item_id, ts, ts),
            )
            tasks.append(desktop_row_to_dict(conn.execute("SELECT * FROM video_download_tasks WHERE id = ?", (task_id,)).fetchone()))
    return {"id": batch_id, "mode": mode, "directory": directory, "tasks": tasks}


def desktop_download_queue_payload(db_path: Path) -> Dict[str, Any]:
    desktop_db_init(db_path)
    with desktop_connect(db_path) as conn:
        batches = [desktop_row_to_dict(row) for row in conn.execute("SELECT * FROM video_download_batches ORDER BY created_at DESC").fetchall()]
        tasks = [desktop_row_to_dict(row) for row in conn.execute(
            """
            SELECT t.*, i.title, i.platform, i.source_url, b.directory
            FROM video_download_tasks t
            JOIN collected_items i ON i.id = t.item_id
            JOIN video_download_batches b ON b.id = t.batch_id
            ORDER BY t.created_at DESC
            """
        ).fetchall()]
    counts = {status: sum(1 for task in tasks if task["status"] == status) for status in ("queued", "preparing", "downloading", "merging", "completed", "failed", "cancelled")}
    return {"batches": batches, "tasks": tasks, "counts": counts}


def desktop_update_download_task(db_path: Path, task_id: str, **updates: Any) -> Dict[str, Any]:
    allowed = {"status", "stage", "downloaded_bytes", "total_bytes", "progress", "error_code", "error_message", "output_path", "method", "completed_at"}
    clean = {key: value for key, value in updates.items() if key in allowed}
    clean["updated_at"] = now_text()
    with desktop_connect(db_path) as conn:
        row = conn.execute("SELECT * FROM video_download_tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("没有找到下载任务。")
        assignments = ", ".join(f"{key} = ?" for key in clean)
        conn.execute(f"UPDATE video_download_tasks SET {assignments} WHERE id = ?", [*clean.values(), task_id])
        updated = conn.execute("SELECT * FROM video_download_tasks WHERE id = ?", (task_id,)).fetchone()
    return desktop_row_to_dict(updated)


def desktop_get_download_task(db_path: Path, task_id: str) -> Optional[Dict[str, Any]]:
    with desktop_connect(db_path) as conn:
        row = conn.execute("SELECT * FROM video_download_tasks WHERE id = ?", (task_id,)).fetchone()
    return desktop_row_to_dict(row) if row else None


def desktop_update_active_download_task(db_path: Path, task_id: str, **updates: Any) -> Optional[Dict[str, Any]]:
    allowed = {"status", "stage", "downloaded_bytes", "total_bytes", "progress", "error_code", "error_message"}
    clean = {key: value for key, value in updates.items() if key in allowed}
    clean["updated_at"] = now_text()
    assignments = ", ".join(f"{key} = ?" for key in clean)
    placeholders = ",".join("?" for _ in DESKTOP_ACTIVE_DOWNLOAD_STATUSES)
    with desktop_connect(db_path) as conn:
        result = conn.execute(
            f"""
            UPDATE video_download_tasks
            SET {assignments}
            WHERE id = ? AND status IN ({placeholders})
            """,
            [*clean.values(), task_id, *DESKTOP_ACTIVE_DOWNLOAD_STATUSES],
        )
        if result.rowcount != 1:
            return None
        row = conn.execute("SELECT * FROM video_download_tasks WHERE id = ?", (task_id,)).fetchone()
    return desktop_row_to_dict(row)


def desktop_complete_download_task(
    db_path: Path,
    task_id: str,
    output_path: str,
    method: str,
) -> Optional[Dict[str, Any]]:
    ts = now_text()
    active_statuses = tuple(status for status in DESKTOP_ACTIVE_DOWNLOAD_STATUSES if status != "queued")
    placeholders = ",".join("?" for _ in active_statuses)
    with desktop_connect(db_path) as conn:
        result = conn.execute(
            f"""
            UPDATE video_download_tasks
            SET status = 'completed', stage = '下载完成', progress = 100.0,
                output_path = ?, method = ?, completed_at = ?, updated_at = ?
            WHERE id = ? AND status IN ({placeholders})
            """,
            (output_path, method, ts, ts, task_id, *active_statuses),
        )
        if result.rowcount != 1:
            return None
        row = conn.execute("SELECT * FROM video_download_tasks WHERE id = ?", (task_id,)).fetchone()
        conn.execute(
            "UPDATE collected_items SET video_path = ?, updated_at = ? WHERE id = ?",
            (output_path, ts, row["item_id"]),
        )
    return desktop_row_to_dict(row)


def desktop_fail_download_task(
    db_path: Path,
    task_id: str,
    error_code: str,
    error_message: str,
) -> Optional[Dict[str, Any]]:
    ts = now_text()
    active_statuses = tuple(status for status in DESKTOP_ACTIVE_DOWNLOAD_STATUSES if status != "queued")
    placeholders = ",".join("?" for _ in active_statuses)
    with desktop_connect(db_path) as conn:
        result = conn.execute(
            f"""
            UPDATE video_download_tasks
            SET status = 'failed', stage = ?, error_code = ?, error_message = ?,
                completed_at = ?, updated_at = ?
            WHERE id = ? AND status IN ({placeholders})
            """,
            (error_code, error_code, error_message, ts, ts, task_id, *active_statuses),
        )
        if result.rowcount != 1:
            return None
        row = conn.execute("SELECT * FROM video_download_tasks WHERE id = ?", (task_id,)).fetchone()
    return desktop_row_to_dict(row)


def classify_video_download_error(error: Any) -> Tuple[str, str]:
    message = str(error or "下载失败")
    lowered = message.lower()
    if "no space left" in lowered or "磁盘空间" in message:
        return "磁盘空间不足", message
    if "permission denied" in lowered or "没有写入权限" in message:
        return "目录无权限", message
    if "cookie" in lowered:
        return "Cookie失效", message
    if "login" in lowered or "sign in" in lowered or "登录" in message:
        return "需登录", message
    if "yt-dlp" in lowered and ("not found" in lowered or "没有找到" in message or "缺失" in message):
        return "yt-dlp缺失", message
    if "403" in lowered or "expired" in lowered or "过期" in message:
        return "视频地址过期", message
    if "timed out" in lowered or "network" in lowered or "urlopen" in lowered:
        return "网络失败", message
    if "unsupported" in lowered or "平台限制" in message or "private" in lowered:
        return "平台限制", message
    return "下载失败", message


def desktop_release_download_worker(db_path: Path, task_id: str, cfg: Dict[str, Any]) -> None:
    key = str(db_path.resolve())
    with DESKTOP_DOWNLOAD_WORKERS_LOCK:
        DESKTOP_DOWNLOAD_WORKERS.setdefault(key, set()).discard(task_id)
    desktop_start_download_worker(db_path, cfg)


def desktop_run_download_task(db_path: Path, task_id: str, cfg: Dict[str, Any]) -> None:
    with desktop_connect(db_path) as conn:
        claimed = conn.execute(
            """
            UPDATE video_download_tasks
            SET status = 'preparing', stage = '获取视频地址', updated_at = ?
            WHERE id = ? AND status = 'queued'
            """,
            (now_text(), task_id),
        )
        row = None
        if claimed.rowcount == 1:
            row = conn.execute(
                """
                SELECT t.*, b.directory FROM video_download_tasks t
                JOIN video_download_batches b ON b.id = t.batch_id WHERE t.id = ?
                """,
                (task_id,),
            ).fetchone()
    if row is None:
        desktop_release_download_worker(db_path, task_id, cfg)
        return
    task = desktop_row_to_dict(row)

    def report(progress: Dict[str, Any]) -> None:
        desktop_update_active_download_task(
            db_path,
            task_id,
            status="downloading",
            stage=str(progress.get("stage") or "正在下载"),
            downloaded_bytes=int(progress.get("downloaded_bytes") or 0),
            total_bytes=int(progress.get("total_bytes") or 0),
            progress=float(progress.get("progress") or 0),
        )

    try:
        result = desktop_save_video_file(
            db_path,
            str(task["item_id"]),
            cfg,
            Path(str(task["directory"])),
            progress_callback=report,
        )
        latest = desktop_get_download_task(db_path, task_id)
        if not latest or str(latest.get("status") or "") in DESKTOP_TERMINAL_DOWNLOAD_STATUSES:
            return
        desktop_complete_download_task(
            db_path,
            task_id,
            str(result.get("path") or ""),
            str(result.get("method") or ""),
        )
    except Exception as e:
        latest = desktop_get_download_task(db_path, task_id)
        if not latest or str(latest.get("status") or "") in DESKTOP_TERMINAL_DOWNLOAD_STATUSES:
            return
        code, message = classify_video_download_error(e)
        desktop_fail_download_task(db_path, task_id, code, message)
    finally:
        desktop_release_download_worker(db_path, task_id, cfg)


def desktop_start_download_worker(db_path: Path, cfg: Dict[str, Any]) -> None:
    desktop_db_init(db_path)
    key = str(db_path.resolve())
    with DESKTOP_DOWNLOAD_WORKERS_LOCK:
        active = DESKTOP_DOWNLOAD_WORKERS.setdefault(key, set())
        available = DESKTOP_DOWNLOAD_MAX_CONCURRENCY - len(active)
        if available <= 0:
            return
        with desktop_connect(db_path) as conn:
            rows = conn.execute("SELECT id FROM video_download_tasks WHERE status = 'queued' ORDER BY created_at LIMIT ?", (available,)).fetchall()
        for row in rows:
            task_id = str(row["id"])
            active.add(task_id)
            threading.Thread(target=desktop_run_download_task, args=(db_path, task_id, cfg), daemon=True).start()


def desktop_retry_failed_downloads(db_path: Path) -> Dict[str, Any]:
    with desktop_connect(db_path) as conn:
        result = conn.execute(
            "UPDATE video_download_tasks SET status = 'queued', stage = '等待重试', error_code = '', error_message = '', updated_at = ? WHERE status = 'failed'",
            (now_text(),),
        )
    return {"ok": True, "count": result.rowcount}


def desktop_cancel_queued_downloads(db_path: Path) -> Dict[str, Any]:
    with desktop_connect(db_path) as conn:
        result = conn.execute(
            "UPDATE video_download_tasks SET status = 'cancelled', stage = '已取消', completed_at = ?, updated_at = ? WHERE status = 'queued'",
            (now_text(), now_text()),
        )
    return {"ok": True, "count": result.rowcount}


def desktop_clear_completed_downloads(db_path: Path) -> Dict[str, Any]:
    with desktop_connect(db_path) as conn:
        result = conn.execute("DELETE FROM video_download_tasks WHERE status IN ('completed', 'cancelled')")
    return {"ok": True, "count": result.rowcount, "files_deleted": 0}


def desktop_open_download_task_path(db_path: Path, task_id: str, folder: bool = False) -> Dict[str, Any]:
    with desktop_connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT t.output_path, b.directory
            FROM video_download_tasks t JOIN video_download_batches b ON b.id = t.batch_id
            WHERE t.id = ?
            """,
            (task_id,),
        ).fetchone()
    if row is None:
        raise ValueError("没有找到下载任务。")
    if folder:
        target = desktop_validate_video_directory(Path(str(row["directory"])))
    else:
        target = Path(str(row["output_path"] or "")).expanduser()
        if not str(row["output_path"] or "") or not target.is_file():
            raise ValueError("视频文件不存在，请重新下载。")
    subprocess.run(["open", str(target)], check=True)
    return {"ok": True, "path": str(target)}


def make_desktop_app_handler(
    cfg: Dict[str, Any],
    db_path: Path,
    cloud_publish_jobs: Any = None,
    *,
    setup_config_path: Path = CONFIG_PATH,
    setup_keychain_writer: Any = None,
    setup_keychain_reader: Any = None,
    setup_keychain_deleter: Any = None,
):
    def update_runtime_config(loaded: Dict[str, Any]):
        return _runtime_config_update(cfg, loaded)

    class DesktopAppHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"橙子助手 {self.address_string()} - {fmt % args}", flush=True)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            try:
                if desktop_reject_non_loopback_cloud_client(self, parsed.path):
                    return
                if parsed.path in ("", "/"):
                    desktop_text_response(self, DESKTOP_APP_HTML)
                    return
                if parsed.path == "/api/version":
                    desktop_json_response(self, desktop_version_payload())
                    return
                if parsed.path == "/daily":
                    desktop_text_response(self, DESKTOP_DAILY_HTML)
                    return
                if parsed.path == "/favicon.ico":
                    self.send_response(204)
                    self.end_headers()
                    return
                if parsed.path == "/api/health":
                    desktop_json_response(self, {"ok": True, "checked_at": now_text()})
                    return
                if parsed.path == "/api/setup/status":
                    with SETUP_TRANSACTION_LOCK:
                        desktop_json_response(self, setup_status(cfg))
                    return
                if parsed.path == "/api/engine/status":
                    desktop_json_response(self, desktop_engine_status(db_path, cfg))
                    return
                if parsed.path == "/api/settings/video-download":
                    desktop_json_response(self, desktop_video_download_settings(db_path))
                    return
                if parsed.path == "/api/video/queue":
                    desktop_json_response(self, desktop_download_queue_payload(db_path))
                    return
                if parsed.path == "/api/tables":
                    desktop_json_response(self, desktop_list_tables(db_path))
                    return
                if parsed.path == "/api/items":
                    params = urllib.parse.parse_qs(parsed.query)
                    table_id = (params.get("table_id") or [""])[0]
                    desktop_json_response(self, desktop_list_items(db_path, table_id))
                    return
                if parsed.path == "/api/daily-report":
                    params = urllib.parse.parse_qs(parsed.query)
                    table_id = (params.get("table_id") or [""])[0]
                    desktop_json_response(self, desktop_get_daily_report(db_path, table_id))
                    return
                if parsed.path == "/api/daily":
                    params = urllib.parse.parse_qs(parsed.query)
                    table_id = (params.get("table_id") or [""])[0]
                    daily_date = (params.get("date") or [""])[0]
                    desktop_json_response(self, desktop_daily_summary(db_path, table_id, daily_date))
                    return
                if parsed.path == "/api/daily/video":
                    params = urllib.parse.parse_qs(parsed.query)
                    item_id = (params.get("item_id") or [""])[0]
                    try:
                        video_path = desktop_daily_video_path(db_path, item_id)
                    except (FileNotFoundError, ValueError) as error:
                        desktop_json_response(self, {"error": str(error)}, status=404)
                        return
                    try:
                        desktop_video_response(self, video_path, self.headers.get("Range") or "")
                    except ValueError as error:
                        desktop_json_response(self, {"error": str(error)}, status=416)
                    return
                if parsed.path == "/api/profile/status":
                    params = urllib.parse.parse_qs(parsed.query)
                    session_id = (params.get("session_id") or [""])[0]
                    desktop_json_response(self, desktop_profile_status(session_id))
                    return
                if parsed.path == "/api/cloud/publish/status":
                    if cloud_publish_jobs is None:
                        desktop_json_response(self, {"error": "云端发布器未配置"}, status=503)
                        return
                    desktop_json_response(self, cloud_publish_jobs.snapshot())
                    return
                if parsed.path == "/api/cloud/latest":
                    if cloud_publish_jobs is None:
                        desktop_json_response(self, {"error": "云端发布器未配置"}, status=503)
                        return
                    desktop_json_response(self, cloud_publish_jobs.latest())
                    return
                if parsed.path in ("/api/export.csv", "/api/export"):
                    params = urllib.parse.parse_qs(parsed.query)
                    table_id = (params.get("table_id") or [""])[0]
                    fmt = (params.get("format") or ["csv"])[0]
                    data, content_type, ext = desktop_export_bytes(db_path, table_id, fmt)
                    filename = desktop_export_default_name(db_path, table_id, ext, fmt)
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Disposition", f'attachment; filename="{urllib.parse.quote(filename)}"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if parsed.path == "/api/cover":
                    params = urllib.parse.parse_qs(parsed.query)
                    url = (params.get("url") or [""])[0]
                    platform = (params.get("platform") or [""])[0]
                    if not url:
                        desktop_json_response(self, {"error": "缺少封面链接"}, status=400)
                        return
                    data, content_type, ext = fetch_binary(url, cfg, platform)
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    if (params.get("download") or [""])[0]:
                        self.send_header("Content-Disposition", f'attachment; filename="cover{ext}"')
                    else:
                        self.send_header("Content-Disposition", f'inline; filename="cover{ext}"')
                    self.send_header("Cache-Control", "private, max-age=3600")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                desktop_json_response(self, {"error": "Not found"}, status=404)
            except Exception as e:
                desktop_json_response(self, {"error": str(e)}, status=500)

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if desktop_reject_non_loopback_cloud_client(self, parsed.path):
                return
            if parsed.path == "/api/setup/save":
                if desktop_reject_untrusted_setup_mutation(self):
                    return
                try:
                    payload = desktop_read_setup_json(self)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    desktop_json_response(self, {"error": "请求内容必须是 JSON 对象"}, status=400)
                    return
                if not isinstance(payload, dict):
                    desktop_json_response(self, {"error": "请求内容必须是 JSON 对象"}, status=400)
                    return
                with SETUP_TRANSACTION_LOCK:
                    try:
                        result = save_setup(
                            payload,
                            setup_config_path,
                            keychain_reader=setup_keychain_reader,
                            keychain_writer=setup_keychain_writer,
                            keychain_deleter=setup_keychain_deleter,
                            runtime_config_updater=update_runtime_config,
                        )
                    except ValueError as error:
                        desktop_json_response(self, {"error": str(error)}, status=400)
                        return
                    except SetupRollbackError:
                        desktop_json_response(
                            self,
                            {"error": SETUP_ROLLBACK_FAILED_ERROR},
                            status=500,
                        )
                        return
                    except Exception:
                        desktop_json_response(
                            self,
                            {"error": "保存首次设置失败，请检查钥匙串和配置目录权限"},
                            status=500,
                        )
                        return
                    desktop_json_response(self, result)
                return
            if parsed.path == "/api/cloud/publish" and cloud_publish_jobs is None:
                desktop_json_response(self, {"error": "云端发布器未配置"}, status=503)
                return
            if parsed.path == "/api/cloud/publish":
                try:
                    payload = desktop_read_cloud_publish_json(self)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    desktop_json_response(self, {"error": "请求内容必须是 JSON 对象"}, status=400)
                    return
                if not isinstance(payload, dict):
                    desktop_json_response(self, {"error": "请求内容必须是 JSON 对象"}, status=400)
                    return
                try:
                    result = cloud_publish_jobs.start(str(payload.get("date") or ""))
                except InvalidPublishDate as error:
                    desktop_json_response(self, {"error": str(error)}, status=400)
                    return
                except PublishAlreadyRunning as error:
                    desktop_json_response(self, {"error": str(error)}, status=409)
                    return
                except Exception as error:
                    desktop_json_response(self, {"error": str(error)}, status=500)
                    return
                desktop_json_response(self, result, status=202)
                return
            try:
                payload = desktop_read_json(self)
                if parsed.path == "/api/tables":
                    table = desktop_create_table(
                        db_path,
                        str(payload.get("name") or ""),
                        str(payload.get("default_platform") or "抖音"),
                    )
                    desktop_json_response(self, table, status=201)
                    return
                if parsed.path == "/api/tables/rename":
                    table = desktop_rename_table(
                        db_path,
                        str(payload.get("table_id") or ""),
                        str(payload.get("name") or ""),
                    )
                    desktop_json_response(self, table)
                    return
                if parsed.path == "/api/tables/delete":
                    result = desktop_delete_table(db_path, str(payload.get("table_id") or ""))
                    desktop_json_response(self, result)
                    return
                if parsed.path == "/api/items/update":
                    item = desktop_update_item(
                        db_path,
                        str(payload.get("item_id") or ""),
                        payload.get("updates") or {},
                    )
                    desktop_json_response(self, item)
                    return
                if parsed.path == "/api/open-url":
                    result = desktop_open_url(str(payload.get("url") or ""))
                    desktop_json_response(self, result)
                    return
                if parsed.path == "/api/cover/save":
                    result = desktop_save_cover_file(
                        str(payload.get("url") or ""),
                        cfg,
                        str(payload.get("platform") or ""),
                    )
                    desktop_json_response(self, result)
                    return
                if parsed.path == "/api/settings/video-download/choose":
                    desktop_json_response(self, desktop_choose_video_directory(db_path, str(payload.get("kind") or "single")))
                    return
                if parsed.path == "/api/settings/video-download/reset":
                    desktop_json_response(self, desktop_set_video_download_directory(db_path, str(payload.get("kind") or "single"), desktop_default_video_directory()))
                    return
                if parsed.path == "/api/settings/video-download/open":
                    desktop_json_response(self, desktop_open_video_directory(db_path, str(payload.get("kind") or "single")))
                    return
                if parsed.path == "/api/video/batches":
                    result = desktop_create_download_batch(db_path, [str(value) for value in payload.get("item_ids") or []], "batch")
                    desktop_start_download_worker(db_path, cfg)
                    desktop_json_response(self, result, status=202)
                    return
                if parsed.path == "/api/video/tasks":
                    result = desktop_create_download_batch(db_path, [str(payload.get("item_id") or "")], "single")
                    desktop_start_download_worker(db_path, cfg)
                    desktop_json_response(self, result, status=202)
                    return
                if parsed.path == "/api/video/retry-failed":
                    result = desktop_retry_failed_downloads(db_path)
                    desktop_start_download_worker(db_path, cfg)
                    desktop_json_response(self, result)
                    return
                if parsed.path == "/api/video/cancel-queued":
                    desktop_json_response(self, desktop_cancel_queued_downloads(db_path))
                    return
                if parsed.path == "/api/video/clear-completed":
                    desktop_json_response(self, desktop_clear_completed_downloads(db_path))
                    return
                if parsed.path == "/api/video/open-output":
                    desktop_json_response(self, desktop_open_download_task_path(db_path, str(payload.get("task_id") or ""), False))
                    return
                if parsed.path == "/api/video/open-folder":
                    desktop_json_response(self, desktop_open_download_task_path(db_path, str(payload.get("task_id") or ""), True))
                    return
                if parsed.path == "/api/video/save":
                    result = desktop_create_download_batch(db_path, [str(payload.get("item_id") or "")], "single")
                    desktop_start_download_worker(db_path, cfg)
                    desktop_json_response(self, result, status=202)
                    return
                if parsed.path == "/api/export/save":
                    result = desktop_save_export_file(
                        db_path,
                        str(payload.get("table_id") or ""),
                        str(payload.get("format") or "csv"),
                    )
                    desktop_json_response(self, result)
                    return
                if parsed.path == "/api/daily-report/save":
                    result = desktop_save_daily_report(
                        db_path,
                        str(payload.get("table_id") or ""),
                        str(payload.get("title") or ""),
                        str(payload.get("body") or ""),
                    )
                    desktop_json_response(self, result)
                    return
                if parsed.path == "/api/daily-report/regenerate":
                    result = desktop_regenerate_daily_report(
                        db_path,
                        str(payload.get("table_id") or ""),
                    )
                    desktop_json_response(self, result)
                    return
                if parsed.path == "/api/daily/add":
                    result = desktop_daily_add_items(
                        db_path,
                        str(payload.get("table_id") or ""),
                        [str(item_id) for item_id in (payload.get("item_ids") or [])],
                        str(payload.get("date") or ""),
                    )
                    desktop_json_response(self, result)
                    return
                if parsed.path == "/api/daily/remove":
                    result = desktop_daily_remove_items(
                        db_path,
                        [str(item_id) for item_id in (payload.get("item_ids") or [])],
                    )
                    desktop_json_response(self, result)
                    return
                if parsed.path == "/api/daily/update":
                    result = desktop_daily_update_card(
                        db_path,
                        str(payload.get("item_id") or ""),
                        payload.get("updates") or {},
                    )
                    desktop_json_response(self, result)
                    return
                if parsed.path == "/api/queue/add":
                    result = desktop_queue_add_urls(
                        db_path,
                        str(payload.get("table_id") or ""),
                        [str(url) for url in (payload.get("urls") or [])],
                        str(payload.get("platform") or ""),
                        str(payload.get("mode") or "single"),
                    )
                    desktop_start_queue_worker(db_path, cfg)
                    desktop_json_response(self, result)
                    return
                if parsed.path == "/api/queue/run":
                    result = desktop_queue_process_once(db_path, cfg, int(payload.get("limit") or 3))
                    desktop_json_response(self, result)
                    return
                if parsed.path == "/api/profile/start":
                    result = desktop_start_profile_session(
                        db_path,
                        str(payload.get("table_id") or ""),
                        str(payload.get("url") or ""),
                        cfg,
                        str(payload.get("platform") or "抖音"),
                    )
                    desktop_json_response(self, result)
                    return
                if parsed.path == "/api/profile/stop":
                    result = desktop_stop_profile_session(str(payload.get("session_id") or ""))
                    desktop_json_response(self, result)
                    return
                if parsed.path == "/api/profile/collect-selected":
                    result = desktop_collect_selected_profile_items(
                        db_path,
                        str(payload.get("table_id") or ""),
                        [str(item_id) for item_id in (payload.get("item_ids") or [])],
                        cfg,
                        str(payload.get("platform") or "抖音"),
                        bool(payload.get("transcribe")),
                    )
                    desktop_json_response(self, result)
                    return
                if parsed.path == "/api/youtube/diagnose":
                    result = youtube_diagnose_url(
                        str(payload.get("url") or ""),
                        cfg,
                        bool(payload.get("probe_download")),
                    )
                    desktop_json_response(self, result)
                    return
                if parsed.path == "/api/youtube/po-provider-enable":
                    enabled = bool(payload.get("enabled", True))
                    result = save_youtube_po_provider_config(CONFIG_PATH, enabled)
                    cfg.setdefault("yt_dlp", {})["youtube_po_token_provider"] = enabled
                    result["message"] = (
                        "已启用 YouTube PO Provider 优先模式；后续下载探测会优先尝试 mweb + Provider。"
                        if enabled
                        else "已关闭 YouTube PO Provider 优先模式。"
                    )
                    desktop_json_response(self, result)
                    return
                if parsed.path == "/api/scrape":
                    table_id = str(payload.get("table_id") or "")
                    platform = str(payload.get("platform") or "")
                    mode = str(payload.get("mode") or "single")
                    urls = payload.get("urls") or []
                    if not table_id:
                        desktop_json_response(self, {"error": "缺少 table_id"}, status=400)
                        return
                    if mode != "single":
                        items = [
                            desktop_save_item(
                                db_path,
                                table_id,
                                {
                                    "platform": platform or detect_platform(str(url)) or "未知",
                                    "source_url": normalize_url(str(url)) or str(url),
                                    "source_type": mode,
                                    "title": "主页批量任务",
                                    "status": "批量任务待接入",
                                    "error": "已保存主页链接；自动展开主页内作品列表尚未接入，当前可先粘贴单作品链接批量采集。",
                                    "raw_metadata_json": "{}",
                                },
                            )
                            for url in urls
                        ]
                    else:
                        items = [
                            desktop_scrape_single_url(db_path, table_id, str(url), cfg, platform)
                            for url in urls
                        ]
                    desktop_json_response(self, {"items": items})
                    return
                desktop_json_response(self, {"error": "Not found"}, status=404)
            except Exception as e:
                desktop_json_response(self, {"error": str(e)}, status=500)

    return DesktopAppHandler


def load_dotenv(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def save_youtube_po_provider_config(path: Path = CONFIG_PATH, enabled: bool = True) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {}
    if path.exists():
        cfg = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            cfg = {}
    yt_dlp_cfg = cfg.setdefault("yt_dlp", {})
    if not isinstance(yt_dlp_cfg, dict):
        yt_dlp_cfg = {}
        cfg["yt_dlp"] = yt_dlp_cfg
    yt_dlp_cfg["youtube_po_token_provider"] = bool(enabled)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"youtube_po_token_provider": bool(enabled)}


def load_config(path: Path = CONFIG_PATH, *, keychain_reader: Any = None) -> Dict[str, Any]:
    load_dotenv()
    if path.exists():
        cfg = json.loads(path.read_text(encoding="utf-8"))
    else:
        cfg = {}
    if not isinstance(cfg, dict):
        raise ValueError("配置文件必须是 JSON 对象")
    reader = keychain_reader or read_keychain_password

    feishu = cfg.setdefault("feishu", {})
    if not isinstance(feishu, dict):
        feishu = {}
        cfg["feishu"] = feishu
    feishu.setdefault("app_id", os.environ.get("FEISHU_APP_ID", ""))
    keychain_service = str(feishu.get("app_secret_keychain_service") or "").strip()
    keychain_account = str(feishu.get("app_secret_keychain_account") or "").strip()
    if keychain_service and keychain_account:
        feishu["app_secret"] = _read_setup_keychain_value(
            reader,
            keychain_service,
            keychain_account,
        )
    else:
        feishu.setdefault("app_secret", os.environ.get("FEISHU_APP_SECRET", ""))
    feishu.setdefault("app_token", os.environ.get("FEISHU_APP_TOKEN", ""))
    feishu.setdefault("table_id", os.environ.get("FEISHU_TABLE_ID", ""))
    feishu.setdefault("mobile_inbox_table_id", os.environ.get("FEISHU_MOBILE_INBOX_TABLE_ID", ""))
    feishu.setdefault("base_url", os.environ.get("FEISHU_BASE_URL", DEFAULT_BASE_URL))

    publisher = cfg.setdefault("publisher", {})
    if not isinstance(publisher, dict):
        publisher = {}
        cfg["publisher"] = publisher
    publisher_service = str(publisher.get("device_token_keychain_service") or "").strip()
    publisher_account = str(publisher.get("device_token_keychain_account") or "").strip()
    if publisher_service and publisher_account:
        publisher["device_token"] = _read_setup_keychain_value(
            reader,
            publisher_service,
            publisher_account,
        )
    else:
        publisher.setdefault("device_token", "")

    cfg.setdefault("fields", {})
    cfg["fields"] = {**DEFAULT_FIELDS, **cfg.get("fields", {})}
    cfg.setdefault("platforms", {})
    cfg.setdefault("asr", {})
    cfg["asr"].setdefault("backend", "local")
    cfg["asr"].setdefault("local_model", "base")
    cfg["asr"].setdefault("language", "zh")
    cfg["asr"].setdefault("initial_prompt", "以下是中文短视频口播逐字稿，请保留自然的中文标点、英文专有名词和段落。")
    cfg["asr"].setdefault("format_transcript", True)
    cfg.setdefault("youtube_safety", {})
    cfg["youtube_safety"].setdefault("enabled", True)
    cfg["youtube_safety"].setdefault("preflight", True)
    cfg["youtube_safety"].setdefault("connectivity_check", False)
    cfg["youtube_safety"].setdefault("connectivity_url", "https://www.youtube.com/generate_204")
    cfg["youtube_safety"].setdefault("throttle_seconds", 3.0)
    cfg["youtube_safety"].setdefault("max_consecutive_network_failures", 2)
    cfg["youtube_safety"].setdefault("open_browser_before_scrape", True)
    cfg["youtube_safety"].setdefault("browser_gate_timeout", 12)
    cfg.setdefault("yt_dlp", {})
    cfg["yt_dlp"].setdefault("enabled", True)
    cfg["yt_dlp"].setdefault("cookies_file", str(HERE / "cookies.txt"))
    cfg["yt_dlp"].setdefault("cookies_from_browser", "")
    cfg["yt_dlp"].setdefault("proxy", "")
    cfg["yt_dlp"].setdefault("extractor_args", [])
    cfg["yt_dlp"].setdefault("js_runtimes", default_js_runtimes())
    cfg["yt_dlp"].setdefault(
        "youtube_retry_extractor_args",
        [
            "youtube:player_client=mweb",
            "youtube:player_client=web_safari",
            "youtube:player_client=ios",
            "youtube:player_client=android",
            "youtube:player_client=tv",
        ],
    )
    cfg["yt_dlp"].setdefault("youtube_po_token", "")
    cfg["yt_dlp"].setdefault("youtube_po_token_provider", False)
    cfg["yt_dlp"].setdefault("download_format", "ba[ext=m4a]/ba/best[ext=mp4][height<=360]/18/best[height<=360]/best")
    login_gate_config(cfg)
    cfg.setdefault("browser_fallback", {})
    cfg["browser_fallback"].setdefault("enabled", True)
    cfg["browser_fallback"].setdefault("channel", "")
    cfg["browser_fallback"].setdefault("executable_path", "")
    if not str(cfg["browser_fallback"].get("executable_path") or "").strip():
        cfg["browser_fallback"]["executable_path"] = default_browser_executable_path()
    cfg["browser_fallback"].setdefault("profile_dir", str(HERE / "browser-profile-cdp"))
    cfg["browser_fallback"].setdefault("timeout", 60)
    cfg["browser_fallback"].setdefault("remote_debugging_port", 9223)
    cfg["browser_fallback"].setdefault("keep_open", True)
    cfg.setdefault("openai", {})
    cfg["openai"].setdefault("transcribe_model", "gpt-4o-transcribe")
    cfg["openai"].setdefault("language", "zh")
    cfg.setdefault("tencent_asr", {})
    cfg["tencent_asr"].setdefault("secret_id", os.environ.get("TENCENTCLOUD_SECRET_ID", ""))
    cfg["tencent_asr"].setdefault("secret_key", os.environ.get("TENCENTCLOUD_SECRET_KEY", ""))
    cfg["tencent_asr"].setdefault("region", os.environ.get("TENCENTCLOUD_REGION", "ap-shanghai"))
    cfg["tencent_asr"].setdefault("engine_model_type", "16k_zh")
    cfg["tencent_asr"].setdefault("res_text_format", 3)
    cfg["tencent_asr"].setdefault("max_local_upload_mb", 5)
    cfg["tencent_asr"].setdefault("poll_interval", 3)
    cfg["tencent_asr"].setdefault("timeout", 180)
    cfg.setdefault("webhook", {})
    cfg["webhook"].setdefault("verification_token", "")
    cfg.setdefault("event", {})
    cfg["event"].setdefault("encrypt_key", "")
    cfg["event"].setdefault("verification_token", cfg["webhook"].get("verification_token", ""))
    cfg["event"].setdefault("scan_interval", 15)
    cfg["event"].setdefault("worker_count", 1)
    cfg.setdefault("mobile_inbox", {})
    cfg["mobile_inbox"].setdefault("enabled", False)
    cfg["mobile_inbox"].setdefault("poll_interval", 10)
    health_config(cfg)
    return cfg


def require_feishu_credentials(cfg: Dict[str, Any]) -> Dict[str, str]:
    feishu = cfg.get("feishu") or {}
    missing = [k for k in ("app_id", "app_secret") if not feishu.get(k)]
    if missing:
        raise SystemExit("缺少飞书配置：" + "、".join(f"feishu.{x}" for x in missing))
    return feishu


def require_feishu(cfg: Dict[str, Any]) -> Dict[str, str]:
    feishu = require_feishu_credentials(cfg)
    missing = [k for k in ("app_token", "table_id") if not feishu.get(k)]
    if missing:
        raise SystemExit("缺少飞书表格配置：" + "、".join(f"feishu.{x}" for x in missing))
    return feishu


def feishu_table_ids(cfg: Dict[str, Any]) -> List[str]:
    feishu = cfg.get("feishu") or {}
    raw_ids = [feishu.get("table_id"), *(feishu.get("table_ids") or [])]
    table_ids: List[str] = []
    for raw in raw_ids:
        table_id = str(raw or "").strip()
        if table_id and table_id not in table_ids:
            table_ids.append(table_id)
    return table_ids


def append_unique_table_id(table_ids: List[str], raw: Any) -> None:
    table_id = str(raw or "").strip()
    if table_id and table_id not in table_ids:
        table_ids.append(table_id)


def discover_feishu_table_ids(cfg: Dict[str, Any]) -> List[str]:
    table_ids = feishu_table_ids(cfg)
    feishu = cfg.get("feishu") or {}
    if not feishu.get("auto_discover_tables", False):
        return table_ids
    try:
        for table in list_tables(cfg):
            append_unique_table_id(table_ids, table.get("table_id") or table.get("id"))
    except Exception as e:
        print(f"自动发现数据表失败：{e}", flush=True)
    return table_ids


def with_table_id(cfg: Dict[str, Any], table_id: str) -> Dict[str, Any]:
    table_cfg = dict(cfg)
    table_cfg["feishu"] = {**(cfg.get("feishu") or {}), "table_id": table_id}
    return table_cfg


def mobile_inbox_table_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    feishu = cfg.get("feishu") or {}
    table_id = str(feishu.get("mobile_inbox_table_id") or "").strip()
    if not table_id:
        raise ValueError("缺少 feishu.mobile_inbox_table_id")
    table_cfg = dict(cfg)
    table_cfg["fields"] = {**DEFAULT_FIELDS, **(cfg.get("fields") or {})}
    table_cfg["feishu"] = {**feishu, "table_id": table_id}
    return table_cfg


def event_worker_count(cfg: Dict[str, Any]) -> int:
    event_cfg = cfg.get("event") or {}
    try:
        raw = event_cfg.get("worker_count", 1)
        count = int(raw if raw is not None else 1)
    except (TypeError, ValueError):
        count = 1
    return max(1, min(8, count))


def login_gate_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    gate = cfg.setdefault("login_gate", {})
    gate["enabled"] = bool(gate.get("enabled", True))
    try:
        gate["retry_interval"] = max(180, int(gate.get("retry_interval") or 180))
    except (TypeError, ValueError):
        gate["retry_interval"] = 180
    try:
        gate["max_retry_attempts"] = min(10, max(1, int(gate.get("max_retry_attempts") or 10)))
    except (TypeError, ValueError):
        gate["max_retry_attempts"] = 10
    try:
        gate["open_cooldown"] = max(60, int(gate.get("open_cooldown") or 300))
    except (TypeError, ValueError):
        gate["open_cooldown"] = 300
    return gate


def health_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    health = cfg.setdefault("health", {})
    health["enabled"] = bool(health.get("enabled", True))
    try:
        health["interval"] = max(300, int(health.get("interval") or 86400))
    except (TypeError, ValueError):
        health["interval"] = 86400
    health["listener_label"] = str(health.get("listener_label") or DEFAULT_EVENT_LISTENER_LABEL)
    health["check_browser"] = bool(health.get("check_browser", True))
    health["check_tables"] = bool(health.get("check_tables", True))
    return health


def now_text() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_local_time(text: str) -> Optional[dt.datetime]:
    value = str(text or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def base_url(feishu: Dict[str, str]) -> str:
    return (feishu.get("base_url") or DEFAULT_BASE_URL).rstrip("/")


def http_json(
    method: str,
    url: str,
    *,
    token: Optional[str] = None,
    body: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 30,
) -> Tuple[int, Any]:
    final_headers = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        final_headers.update(headers)
    if token:
        final_headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=final_headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"code": e.code, "msg": raw[:500]}
    except urllib.error.URLError as e:
        return 0, {"code": "NETWORK_ERROR", "msg": str(getattr(e, "reason", e))}


def tenant_access_token(cfg: Dict[str, Any], force: bool = False) -> str:
    feishu = require_feishu_credentials(cfg)
    if not force and TOKEN_CACHE.exists():
        try:
            cached = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
            if cached.get("tenant_access_token") and cached.get("expires_at", 0) > time.time() + 60:
                return cached["tenant_access_token"]
        except Exception:
            pass

    status, payload = http_json(
        "POST",
        base_url(feishu) + "/open-apis/auth/v3/tenant_access_token/internal",
        body={"app_id": feishu["app_id"], "app_secret": feishu["app_secret"]},
    )
    if status >= 400 or not isinstance(payload, dict) or payload.get("code") != 0:
        raise SystemExit("获取 tenant_access_token 失败：\n" + json.dumps(payload, ensure_ascii=False, indent=2))
    token = payload["tenant_access_token"]
    TOKEN_CACHE.write_text(
        json.dumps({"tenant_access_token": token, "expires_at": time.time() + int(payload.get("expire", 7200))}),
        encoding="utf-8",
    )
    return token


def feishu_api(cfg: Dict[str, Any], method: str, endpoint: str, body: Optional[Dict[str, Any]] = None) -> Any:
    feishu = require_feishu(cfg)
    token = tenant_access_token(cfg)
    status, payload = http_json(method, base_url(feishu) + endpoint, token=token, body=body)
    if status >= 400 or not isinstance(payload, dict) or payload.get("code") not in (0, None):
        raise SystemExit("飞书 API 调用失败：\n" + json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def field_payload(name: str, typ: int, options: Optional[List[str]] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"field_name": name, "type": typ}
    if typ == 3 and options:
        payload["property"] = {"options": [{"name": x} for x in options]}
    return payload


def records_endpoint(cfg: Dict[str, Any]) -> str:
    feishu = require_feishu(cfg)
    app_token = urllib.parse.quote(feishu["app_token"])
    table_id = urllib.parse.quote(feishu["table_id"])
    return f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"


def fields_endpoint(cfg: Dict[str, Any]) -> str:
    feishu = require_feishu(cfg)
    app_token = urllib.parse.quote(feishu["app_token"])
    table_id = urllib.parse.quote(feishu["table_id"])
    return f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"


def tables_endpoint(cfg: Dict[str, Any]) -> str:
    feishu = require_feishu(cfg)
    app_token = urllib.parse.quote(feishu["app_token"])
    return f"/open-apis/bitable/v1/apps/{app_token}/tables"


def list_tables(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    payload = feishu_api(cfg, "GET", tables_endpoint(cfg))
    return (payload.get("data") or {}).get("items") or []


def list_fields(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    payload = feishu_api(cfg, "GET", fields_endpoint(cfg))
    return (payload.get("data") or {}).get("items") or []


def list_records(cfg: Dict[str, Any], page_size: int = 100) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    page_token = ""
    while True:
        query = {"page_size": str(page_size)}
        if page_token:
            query["page_token"] = page_token
        endpoint = records_endpoint(cfg) + "?" + urllib.parse.urlencode(query)
        payload = feishu_api(cfg, "GET", endpoint)
        data = payload.get("data") or {}
        items.extend(data.get("items") or [])
        if not data.get("has_more"):
            return items
        page_token = data.get("page_token") or ""
        if not page_token:
            return items


def get_record(cfg: Dict[str, Any], record_id: str) -> Dict[str, Any]:
    endpoint = records_endpoint(cfg) + "/" + urllib.parse.quote(record_id)
    payload = feishu_api(cfg, "GET", endpoint)
    return (payload.get("data") or {}).get("record") or {}


def update_record(cfg: Dict[str, Any], record_id: str, fields: Dict[str, Any]) -> None:
    endpoint = records_endpoint(cfg) + "/" + urllib.parse.quote(record_id)
    feishu_api(cfg, "PUT", endpoint, {"fields": fields})


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("name") or item.get("link") or ""))
            else:
                parts.append(str(item))
        return "".join(parts).strip()
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or value.get("link") or "").strip()
    return str(value).strip()


def normalize_url(raw: str) -> str:
    raw = raw.strip()
    m = re.search(r"https?://[^\s)）>]+", raw)
    return m.group(0) if m else raw


def normalize_resource_url(url: str, base: str = "") -> str:
    url = (url or "").strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith(("http://", "https://")):
        return url
    if url and base:
        return urllib.parse.urljoin(base, url)
    return url


def detect_platform(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower()
    if "douyin" in host or "iesdouyin" in host:
        return "抖音"
    if "xiaohongshu" in host or "xhslink" in host:
        return "小红书"
    if "bilibili" in host or host.endswith("b23.tv") or host.endswith("bili2233.cn"):
        return "B站"
    if "weixin.qq.com" in host or "channels.weixin" in host:
        return "视频号"
    if "youtube.com" in host or host.endswith("youtu.be"):
        return "YouTube"
    if "instagram.com" in host:
        return "Instagram"
    return "未知"


def login_url_for_platform(platform: str) -> str:
    return LOGIN_URLS.get(platform, "")


def should_trigger_login_gate(status: str) -> bool:
    return status in LOGIN_STATUSES


def login_gate_cooldown_allows(
    platform: str,
    state: Dict[str, float],
    cooldown: int,
    now: Optional[float] = None,
) -> bool:
    current = time.monotonic() if now is None else now
    last_opened = float(state.get(platform) or 0)
    if last_opened and current - last_opened < max(0, cooldown):
        return False
    state[platform] = current
    return True


def open_login_page_once(
    platform: str,
    state: Dict[str, float],
    lock: threading.Lock,
    cooldown: int,
) -> bool:
    login_url = login_url_for_platform(platform)
    if not login_url:
        return False
    with lock:
        allowed = login_gate_cooldown_allows(platform, state, cooldown)
    if not allowed:
        return False
    try:
        subprocess.Popen(["open", login_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"登录守门员：已打开{platform}登录页 {login_url}", flush=True)
        return True
    except Exception as e:
        print(f"登录守门员：打开{platform}登录页失败 -> {e}", flush=True)
        return False


def platform_cookie(cfg: Dict[str, Any], platform: str) -> str:
    info = (cfg.get("platforms") or {}).get(platform) or {}
    env_key = {
        "抖音": "DOUYIN_COOKIE",
        "小红书": "XHS_COOKIE",
        "B站": "BILIBILI_COOKIE",
        "视频号": "WEIXIN_COOKIE",
    }.get(platform, "")
    return info.get("cookie") or (os.environ.get(env_key) if env_key else "") or ""


def has_ytdlp_cookie(cfg: Dict[str, Any]) -> bool:
    ytdlp_cfg = cfg.get("yt_dlp") or {}
    cookies_file = ytdlp_cfg.get("cookies_file") or str(HERE / "cookies.txt")
    if cookies_file and Path(cookies_file).expanduser().exists():
        return True
    return bool((ytdlp_cfg.get("cookies_from_browser") or "").strip())


def fetch_text(url: str, cfg: Dict[str, Any], platform: str) -> Tuple[str, str]:
    headers = dict(TEXT_HEADERS)
    cookie = platform_cookie(cfg, platform)
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as resp:
        final_url = resp.geturl()
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace"), final_url


def fetch_text_optional(url: str, cfg: Dict[str, Any], platform: str) -> Tuple[str, str]:
    try:
        return fetch_text(url, cfg, platform)
    except Exception:
        return "", url


def douyin_aweme_id(url: str) -> str:
    for pat in (r"/video/(\d+)", r"aweme_id=(\d+)", r"/note/(\d+)"):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return ""


def canonical_douyin_video_url(url: str) -> str:
    aweme_id = douyin_aweme_id(url)
    return f"https://www.douyin.com/video/{aweme_id}" if aweme_id else url


def fetch_json_url(url: str, cfg: Dict[str, Any], platform: str) -> Optional[Dict[str, Any]]:
    headers = dict(TEXT_HEADERS)
    cookie = platform_cookie(cfg, platform)
    if cookie:
        headers["Cookie"] = cookie
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def pick_video_url(video: Dict[str, Any]) -> str:
    for key in ("play_addr", "download_addr", "playAddr", "downloadAddr"):
        value = video.get(key)
        if isinstance(value, dict):
            for list_key in ("url_list", "urlList", "urls"):
                for item in value.get(list_key) or []:
                    if isinstance(item, str) and item.startswith(("http://", "https://")):
                        return item
            direct = value.get("url")
            if isinstance(direct, str) and direct.startswith(("http://", "https://")):
                return direct
        got = first_url(value)
        if got.startswith(("http://", "https://")):
            return got
    for item in video.get("bit_rate") or []:
        if isinstance(item, dict):
            got = pick_video_url(item.get("play_addr") or {})
            if got:
                return got
    return ""


def is_media_url(value: str) -> bool:
    if not isinstance(value, str) or not value.startswith(("http://", "https://")):
        return False
    parsed = urllib.parse.urlparse(value)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if (
        "youtube.com" in host
        or host.endswith("youtu.be")
        or (host == "instagram.com" or host.endswith(".instagram.com"))
        or ("bilibili.com" in host and "/video/" in path)
        or ("douyin.com" in host and "/video/" in path)
        or ("xiaohongshu.com" in host and ("/explore/" in path or "/discovery/item/" in path))
        or ("weixin.qq.com" in host and not re.search(r"\.(mp4|m4s|m3u8|mov|mp3|m4a|aac)(?:$|[?#])", path))
    ):
        return False
    if re.search(r"\.(mp4|m4s|m3u8|mov|webm|mp3|m4a|aac|wav)(?:$|[?#])", path):
        return True
    query = urllib.parse.parse_qs(parsed.query)
    mime_type = str((query.get("mime_type") or [""])[0]).lower()
    if mime_type.startswith(("video_", "audio_")):
        return True
    direct_host_tokens = (
        "googlevideo.com",
        "cdninstagram.com",
        "scontent",
        "fbcdn.net",
        "douyinvod.com",
        "douyinpic.com",
        "365yg.com",
        "xhscdn.com",
        "bilivideo.com",
        "akamaized.net",
        "bytecdn",
        "vod",
    )
    return any(token in host for token in direct_host_tokens)


def media_url_or_empty(meta: Dict[str, Any]) -> str:
    got = meta.get("media_url") or ""
    return got if is_media_url(got) else ""


def extract_douyin_api(url: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    aweme_id = douyin_aweme_id(url)
    if not aweme_id:
        return {}
    api = "https://www.douyin.com/aweme/v1/web/aweme/detail/?" + urllib.parse.urlencode({"aweme_id": aweme_id})
    payload = fetch_json_url(api, cfg, "抖音") or {}
    detail = payload.get("aweme_detail") or {}
    if not detail:
        return {}
    return douyin_detail_to_meta(url, url, detail)


def douyin_detail_to_meta(source_url: str, final_url: str, detail: Dict[str, Any]) -> Dict[str, Any]:
    stat = detail.get("statistics") or {}
    video = detail.get("video") or {}
    cover = video.get("cover") or video.get("origin_cover") or video.get("dynamic_cover") or {}
    desc = str(detail.get("desc") or "").strip()
    transcript = pick_first_json(detail, TRANSCRIPT_KEYS)
    return {
        "source_url": source_url,
        "final_url": final_url,
        "platform": "抖音",
        "content_type": "video",
        "title": clean_title(desc[:120]),
        "caption": str(transcript or "").strip(),
        "cover_url": first_url(cover),
        "duration": to_duration(video.get("duration")),
        "likes": to_int(stat.get("digg_count")),
        "comments": to_int(stat.get("comment_count")),
        "shares": to_int(stat.get("share_count")),
        "published_at": to_time_text(detail.get("create_time")),
        "media_url": pick_video_url(video),
    }


def bilibili_bvid_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    text = urllib.parse.unquote(parsed.path + "?" + parsed.query)
    match = re.search(r"(BV[0-9A-Za-z]{6,})", text)
    return match.group(1) if match else ""


def bilibili_aid_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    text = urllib.parse.unquote(parsed.path + "?" + parsed.query)
    match = re.search(r"(?:^|/|[?&])av(\d+)", text, flags=re.I)
    if match:
        return match.group(1)
    query = urllib.parse.parse_qs(parsed.query)
    for key in ("aid", "avid"):
        value = (query.get(key) or [""])[0]
        if str(value).isdigit():
            return str(value)
    return ""


def bilibili_api_url(url: str) -> str:
    bvid = bilibili_bvid_from_url(url)
    if bvid:
        return "https://api.bilibili.com/x/web-interface/view?" + urllib.parse.urlencode({"bvid": bvid})
    aid = bilibili_aid_from_url(url)
    if aid:
        return "https://api.bilibili.com/x/web-interface/view?" + urllib.parse.urlencode({"aid": aid})
    return ""


def bilibili_fetch_json_url(url: str, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    headers = dict(TEXT_HEADERS)
    headers["Referer"] = "https://www.bilibili.com/"
    cookie = platform_cookie(cfg, "B站")
    if cookie:
        headers["Cookie"] = cookie
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def bilibili_view_to_meta(source_url: str, final_url: str, data: Dict[str, Any]) -> Dict[str, Any]:
    stat = data.get("stat") or {}
    title = clean_title(str(data.get("title") or ""))
    cover_url = normalize_resource_url(str(data.get("pic") or data.get("cover") or ""), final_url)
    return {
        "source_url": source_url,
        "final_url": final_url,
        "platform": "B站",
        "content_type": "video",
        "title": title,
        "caption": "",
        "cover_url": cover_url,
        "duration": to_duration(data.get("duration")),
        "likes": to_int(stat.get("like")),
        "comments": to_int(stat.get("reply")),
        "shares": to_int(stat.get("share")),
        "published_at": to_time_text(data.get("pubdate") or data.get("ctime")),
        "media_url": "",
    }


def extract_bilibili_api(url: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    api = bilibili_api_url(url)
    if not api:
        return {}
    payload = bilibili_fetch_json_url(api, cfg) or {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict) or not data.get("title"):
        return {}
    return bilibili_view_to_meta(url, url, data)


def merge_meta(primary: Dict[str, Any], fallback: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(primary)
    for key, value in fallback.items():
        if key == "media_url" and is_media_url(str(value or "")) and not is_media_url(str(out.get(key) or "")):
            out[key] = value
        elif key == "title" and value and not usable_browser_title(str(out.get(key) or "")):
            out[key] = value
        elif out.get(key) in ("", None, [], {}):
            out[key] = value
    return out


def ytdlp_path() -> Optional[str]:
    found = shutil.which("yt-dlp")
    if found:
        return found
    local_bin = Path.home() / ".local" / "bin" / "yt-dlp"
    if local_bin.exists():
        return str(local_bin)
    user_bin = Path.home() / "Library" / "Python" / "3.9" / "bin" / "yt-dlp"
    if user_bin.exists():
        return str(user_bin)
    return None


def default_js_runtimes() -> str:
    local_node = Path.home() / ".local" / "bin" / "node"
    if local_node.exists():
        return f"node:{local_node}"
    found = shutil.which("node")
    return f"node:{found}" if found else ""


def ytdlp_cookie_sources(url: str, cfg: Dict[str, Any]) -> List[str]:
    ytdlp_cfg = cfg.get("yt_dlp") or {}
    configured = str(ytdlp_cfg.get("cookies_from_browser") or "").strip()
    sources: List[str] = [configured] if configured else [""]
    if detect_platform(url) == "YouTube":
        fallback_cfg = cfg.get("browser_fallback") or {}
        login_browser = browser_name_from_executable(str(fallback_cfg.get("executable_path") or ""))
        cookie_browser = browser_name_from_ytdlp_cookie_source(configured)
        profile_dir = str(fallback_cfg.get("profile_dir") or "").strip()
        login_sources: List[str] = []
        if login_browser and profile_dir:
            login_sources.append(f"{login_browser}:{profile_dir}")
        if login_browser:
            login_sources.append(login_browser)
        for source in login_sources:
            if source not in sources and (login_browser != cookie_browser or source != login_browser):
                sources.append(source)
    return sources


def cfg_with_ytdlp_cookie_source(cfg: Dict[str, Any], source: str) -> Dict[str, Any]:
    out = dict(cfg)
    ytdlp_cfg = dict(out.get("yt_dlp") or {})
    ytdlp_cfg["cookies_from_browser"] = source
    out["yt_dlp"] = ytdlp_cfg
    return out


def cfg_with_ytdlp_cookie_file(cfg: Dict[str, Any], cookie_path: Path) -> Dict[str, Any]:
    out = dict(cfg)
    ytdlp_cfg = dict(out.get("yt_dlp") or {})
    ytdlp_cfg["cookies_file"] = str(cookie_path)
    ytdlp_cfg["cookies_from_browser"] = ""
    out["yt_dlp"] = ytdlp_cfg
    return out


def youtube_should_export_browser_cookies(url: str, cfg: Dict[str, Any]) -> bool:
    if detect_platform(url) != "YouTube":
        return False
    ytdlp_cfg = cfg.get("yt_dlp") or {}
    if ytdlp_cfg.get("export_browser_cookies") is False:
        return False
    fallback_cfg = cfg.get("browser_fallback") or {}
    return bool(fallback_cfg.get("enabled", True) and str(fallback_cfg.get("profile_dir") or "").strip())


def netscape_cookie_line(cookie: Dict[str, Any]) -> str:
    domain = str(cookie.get("domain") or "").strip()
    if not domain:
        return ""
    if cookie.get("httpOnly") and not domain.startswith("#HttpOnly_"):
        domain = "#HttpOnly_" + domain
    include_subdomains = "TRUE" if str(cookie.get("domain") or "").startswith(".") else "FALSE"
    path = str(cookie.get("path") or "/")
    secure = "TRUE" if cookie.get("secure") else "FALSE"
    expires = cookie.get("expires")
    try:
        expires_text = str(max(0, int(float(expires or 0))))
    except (TypeError, ValueError):
        expires_text = "0"
    name = str(cookie.get("name") or "")
    value = str(cookie.get("value") or "")
    if not name:
        return ""
    return "\t".join([domain, include_subdomains, path, secure, expires_text, name, value])


def write_netscape_cookies(cookie_path: Path, cookies: List[Dict[str, Any]]) -> Path:
    lines = ["# Netscape HTTP Cookie File"]
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").lower()
        if "youtube.com" not in domain and "google.com" not in domain:
            continue
        line = netscape_cookie_line(cookie)
        if line:
            lines.append(line)
    if len(lines) <= 1:
        raise RuntimeError("专用浏览器没有导出 YouTube/Google Cookie；请先在弹出的 YouTube 页面完成登录验证。")
    cookie_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return cookie_path


def export_youtube_cookies_from_cdp(url: str, cfg: Dict[str, Any], cookie_path: Path) -> Path:
    fallback_cfg = browser_fallback_config(cfg)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError("缺少 Playwright，无法从专用浏览器导出 YouTube Cookie。") from e
    launch_cdp_browser(fallback_cfg, url or "https://www.youtube.com/")
    with sync_playwright() as playwright:
        browser = connect_cdp_browser_with_recovery(playwright, fallback_cfg, "YouTube")
        contexts = list(getattr(browser, "contexts", []) or [])
        if not contexts:
            raise RuntimeError("专用浏览器没有可用上下文，无法导出 YouTube Cookie。")
        cookies = contexts[0].cookies(
            [
                "https://www.youtube.com/",
                "https://youtube.com/",
                "https://accounts.google.com/",
                "https://www.google.com/",
            ]
        )
    return write_netscape_cookies(cookie_path, cookies)


def add_ytdlp_cookie_args(cmd: List[str], cfg: Dict[str, Any]) -> None:
    ytdlp_cfg = cfg.get("yt_dlp") or {}
    cookies_file = ytdlp_cfg.get("cookies_file") or ""
    cookie_path = Path(cookies_file).expanduser() if cookies_file else None
    if cookie_path and not cookie_path.is_absolute():
        cookie_path = HERE / cookie_path
    if cookie_path and not cookie_path.exists() and (HERE / "cookies.txt").exists():
        cookie_path = HERE / "cookies.txt"
    if cookie_path and cookie_path.exists():
        cmd.extend(["--cookies", str(cookie_path)])
    cookies_from_browser = (ytdlp_cfg.get("cookies_from_browser") or "").strip()
    if cookies_from_browser:
        cmd.extend(["--cookies-from-browser", cookies_from_browser])


def add_ytdlp_common_args(cmd: List[str], cfg: Dict[str, Any]) -> None:
    ytdlp_cfg = cfg.get("yt_dlp") or {}
    proxy = str(ytdlp_cfg.get("proxy") or "").strip()
    if proxy:
        cmd.extend(["--proxy", proxy])
    js_runtimes = str(ytdlp_cfg.get("js_runtimes") or "").strip()
    if js_runtimes:
        cmd.extend(["--js-runtimes", js_runtimes])
    extractor_args = ytdlp_cfg.get("extractor_args") or []
    if isinstance(extractor_args, str):
        extractor_args = [x.strip() for x in extractor_args.split(";") if x.strip()]
    for arg in extractor_args:
        arg = str(arg).strip()
        if arg:
            cmd.extend(["--extractor-args", arg])
    add_ytdlp_cookie_args(cmd, cfg)


def ytdlp_configured_extractor_args(cfg: Dict[str, Any]) -> List[str]:
    ytdlp_cfg = cfg.get("yt_dlp") or {}
    extractor_args = ytdlp_cfg.get("extractor_args") or []
    if isinstance(extractor_args, str):
        extractor_args = [x.strip() for x in extractor_args.split(";") if x.strip()]
    return [str(arg).strip() for arg in extractor_args if str(arg).strip()]


def ytdlp_download_strategies(url: str, cfg: Dict[str, Any]) -> List[str]:
    if detect_platform(url) != "YouTube":
        return [""]
    ytdlp_cfg = cfg.get("yt_dlp") or {}
    configured = ytdlp_configured_extractor_args(cfg)
    retry_args = ytdlp_cfg.get("youtube_retry_extractor_args")
    if retry_args is None:
        retry_args = [
            "youtube:player_client=mweb",
            "youtube:player_client=web_safari",
            "youtube:player_client=ios",
            "youtube:player_client=android",
            "youtube:player_client=tv",
        ]
    elif isinstance(retry_args, str):
        retry_args = [x.strip() for x in retry_args.split(";") if x.strip()]
    retry_strategies = [str(arg).strip() for arg in retry_args if str(arg).strip()]
    strategies: List[str] = []
    po_token = str(ytdlp_cfg.get("youtube_po_token") or "").strip()
    has_po_priority = False
    if po_token:
        strategies.append(f"youtube:player_client=mweb;po_token={po_token}")
        has_po_priority = True
    elif ytdlp_cfg.get("youtube_po_token_provider"):
        strategies.append("youtube:player_client=mweb")
        has_po_priority = True
    strategies.extend(configured)
    strategies.extend(retry_strategies)
    out: List[str] = [] if has_po_priority else [""]
    seen: set[str] = set(out)
    for strategy in strategies:
        if strategy not in seen:
            out.append(strategy)
            seen.add(strategy)
    if "" not in seen:
        out.append("")
    return out


def ytdlp_download_formats(cfg: Dict[str, Any]) -> List[str]:
    ytdlp_cfg = cfg.get("yt_dlp") or {}
    configured = str(ytdlp_cfg.get("download_format") or "").strip()
    if not configured:
        configured = "ba[ext=m4a]/ba/best[ext=mp4][height<=360]/18/best[height<=360]/best"
    candidates = [configured, "bestaudio/best", "best"]
    out: List[str] = []
    for fmt in candidates:
        if fmt and fmt not in out:
            out.append(fmt)
    return out


def add_ytdlp_strategy_args(cmd: List[str], strategy: str) -> None:
    strategy = str(strategy or "").strip()
    if strategy:
        cmd.extend(["--extractor-args", strategy])


def youtube_caption_languages(cfg: Dict[str, Any]) -> List[str]:
    configured = (cfg.get("yt_dlp") or {}).get("subtitle_languages") or []
    if isinstance(configured, str):
        configured = [x.strip() for x in configured.split(",")]
    defaults = ["zh-Hans", "zh-CN", "zh", "en", "en-US"]
    langs = [str(x).strip() for x in configured if str(x).strip()] + defaults
    seen: set[str] = set()
    return [x for x in langs if not (x in seen or seen.add(x))]


def youtube_caption_url_from_payload(payload: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    preferred_exts = ["json3", "vtt", "srv3", "srv2", "srv1"]
    for bucket_name in ("subtitles", "automatic_captions"):
        bucket = payload.get(bucket_name) or {}
        for lang in youtube_caption_languages(cfg):
            items = bucket.get(lang) or []
            for ext in preferred_exts:
                for item in items:
                    if str(item.get("ext") or "").lower() == ext and item.get("url"):
                        return str(item["url"])
            for item in items:
                if item.get("url"):
                    return str(item["url"])
    return ""


def clean_youtube_caption_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\[[^\]]{1,30}\]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def youtube_json3_caption_text(raw: str) -> str:
    try:
        data = json.loads(raw)
    except Exception:
        return ""
    parts: List[str] = []
    for event in data.get("events") or []:
        event_parts: List[str] = []
        for seg in event.get("segs") or []:
            got = str(seg.get("utf8") or "")
            if got.strip() and got.strip() != "\\n":
                event_parts.append(got)
        if event_parts:
            parts.append("".join(event_parts))
    return clean_youtube_caption_text(" ".join(parts))


def youtube_vtt_caption_text(raw: str) -> str:
    lines: List[str] = []
    for raw_line in str(raw or "").splitlines():
        line = raw_line.strip()
        if not line or line.upper().startswith("WEBVTT") or "-->" in line or re.fullmatch(r"\d+", line):
            continue
        lines.append(line)
    return clean_youtube_caption_text(" ".join(lines))


def fetch_youtube_caption_url(url: str, cfg: Dict[str, Any]) -> str:
    req = urllib.request.Request(url, headers=TEXT_HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


def youtube_caption_from_payload(payload: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    url = youtube_caption_url_from_payload(payload, cfg)
    if not url:
        return ""
    try:
        raw = fetch_youtube_caption_url(url, cfg)
    except Exception:
        return ""
    parsed = urllib.parse.urlparse(url)
    fmt = (urllib.parse.parse_qs(parsed.query).get("fmt") or [""])[0].lower()
    if fmt == "json3" or raw.lstrip().startswith("{"):
        return youtube_json3_caption_text(raw)
    return youtube_vtt_caption_text(raw)


def youtube_caption_from_initial_player_response(payload: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    captions = payload.get("captions") or {}
    renderer = captions.get("playerCaptionsTracklistRenderer") or {}
    tracks = renderer.get("captionTracks") or []
    if not isinstance(tracks, list):
        return ""
    by_lang: Dict[str, List[Dict[str, Any]]] = {}
    fallback: List[Dict[str, Any]] = []
    for track in tracks:
        if not isinstance(track, dict) or not track.get("baseUrl"):
            continue
        fallback.append(track)
        lang = str(track.get("languageCode") or "").strip()
        if lang:
            by_lang.setdefault(lang, []).append(track)
    ordered: List[Dict[str, Any]] = []
    for lang in youtube_caption_languages(cfg):
        ordered.extend(by_lang.get(lang) or [])
    ordered.extend([track for track in fallback if track not in ordered])
    for track in ordered:
        url = str(track.get("baseUrl") or "")
        if not url:
            continue
        if "fmt=" not in urllib.parse.urlparse(url).query:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}fmt=json3"
        try:
            raw = fetch_youtube_caption_url(url, cfg)
        except Exception:
            continue
        if raw.lstrip().startswith("{"):
            text = youtube_json3_caption_text(raw)
        else:
            text = youtube_vtt_caption_text(raw)
        if text:
            return text
    return ""


def extract_with_ytdlp(url: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    ytdlp_cfg = cfg.get("yt_dlp") or {}
    if not ytdlp_cfg.get("enabled", True):
        return {}
    exe = ytdlp_path()
    if not exe:
        if detect_platform(url) in {"YouTube", "Instagram"}:
            raise RuntimeError("本机没有找到 yt-dlp，无法抓取该平台视频元数据。")
        return {}
    cookie_errors: List[str] = []
    export_tmp_dir: Optional[Path] = None
    attempts: List[Tuple[str, Dict[str, Any]]] = []
    if youtube_should_export_browser_cookies(url, cfg):
        export_tmp_dir = Path(tempfile.mkdtemp(prefix="youtube-cookies-"))
        try:
            cookie_path = export_youtube_cookies_from_cdp(url, cfg, export_tmp_dir / "cookies.txt")
            attempts.append(("专用浏览器导出Cookie", cfg_with_ytdlp_cookie_file(cfg, cookie_path)))
        except Exception as e:
            cookie_errors.append(f"专用浏览器导出Cookie: {str(e)[-500:]}")
    attempts.extend((source or "未配置浏览器Cookie", cfg_with_ytdlp_cookie_source(cfg, source)) for source in ytdlp_cookie_sources(url, cfg))
    try:
        for label, attempt_cfg in attempts:
            cmd = [exe, "--dump-json", "--no-warnings", "--no-playlist", url]
            add_ytdlp_common_args(cmd, attempt_cfg)
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
            if result.returncode == 0 and result.stdout.strip():
                break
            err = (result.stderr or "").strip()
            if "Fresh cookies" in err or "cookies" in err.lower() or "sign in to confirm" in err.lower():
                cookie_errors.append(f"{label}: {err[-500:]}")
                continue
            return {}
        else:
            platform = detect_platform(url)
            sources = [label for label, _ in attempts if label]
            if sources:
                raise RuntimeError(
                    f"yt-dlp 已尝试 {', '.join(sources)}，但{platform}要求刷新登录态。请在专用浏览器重新打开/登录{platform}后再试。"
                )
            raise RuntimeError("yt-dlp 需要登录 Cookie；请配置 cookies.txt 或 cookies_from_browser。")
    finally:
        if export_tmp_dir:
            shutil.rmtree(export_tmp_dir, ignore_errors=True)
    payload = json.loads(result.stdout.splitlines()[-1])
    formats = payload.get("formats") or []
    media_url = payload.get("url") or ""
    if not is_media_url(media_url):
        for fmt in reversed(formats):
            got = fmt.get("url")
            if is_media_url(got):
                media_url = got
                break
    return {
        "source_url": url,
        "final_url": payload.get("webpage_url") or url,
        "platform": detect_platform(url),
        "content_type": "video",
        "title": clean_title(payload.get("title") or payload.get("description") or ""),
        "caption": youtube_caption_from_payload(payload, cfg) if detect_platform(url) == "YouTube" else "",
        "cover_url": payload.get("thumbnail") or "",
        "duration": to_duration(payload.get("duration")),
        "likes": to_int(payload.get("like_count")),
        "comments": to_int(payload.get("comment_count")),
        "shares": to_int(payload.get("repost_count")),
        "published_at": to_time_text(payload.get("timestamp") or payload.get("upload_date")),
        "media_url": media_url,
    }


def fresh_cookie_error(error: Exception) -> bool:
    text = str(error).lower()
    return "fresh cookies" in text or "刷新登录态" in str(error) or "登录态" in str(error)


def browser_fallback_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    raw = dict(cfg.get("browser_fallback") or {})
    raw.setdefault("enabled", True)
    raw.setdefault("channel", "")
    raw.setdefault("executable_path", "")
    raw.setdefault("profile_dir", str(HERE / "browser-profile-cdp"))
    raw.setdefault("timeout", 60)
    raw.setdefault("remote_debugging_port", 9223)
    raw.setdefault("keep_open", True)
    raw["remote_debugging_port"] = int(raw.get("remote_debugging_port") or 9223)
    raw["timeout"] = max(10, int(raw.get("timeout") or 60))
    return raw


def cdp_endpoint(fallback_cfg: Dict[str, Any]) -> str:
    port = int(fallback_cfg.get("remote_debugging_port") or 9223)
    return f"http://127.0.0.1:{port}"


def cdp_browser_available(fallback_cfg: Dict[str, Any]) -> bool:
    try:
        with urllib.request.urlopen(cdp_endpoint(fallback_cfg) + "/json/version", timeout=1) as resp:
            return resp.status == 200
    except Exception:
        return False


def wait_for_cdp_browser(fallback_cfg: Dict[str, Any], timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cdp_browser_available(fallback_cfg):
            return True
        time.sleep(0.25)
    return cdp_browser_available(fallback_cfg)


def cdp_http_json(fallback_cfg: Dict[str, Any], path: str, method: str = "GET") -> Any:
    req = urllib.request.Request(cdp_endpoint(fallback_cfg) + path, data=b"" if method != "GET" else None, method=method)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def cdp_list_targets(fallback_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        targets = cdp_http_json(fallback_cfg, "/json/list")
    except Exception:
        return []
    return targets if isinstance(targets, list) else []


def cdp_create_target(fallback_cfg: Dict[str, Any], url: str) -> Dict[str, Any]:
    encoded = urllib.parse.quote(url or "about:blank", safe="")
    for method in ("PUT", "POST"):
        try:
            target = cdp_http_json(fallback_cfg, f"/json/new?{encoded}", method=method)
            return target if isinstance(target, dict) else {}
        except urllib.error.HTTPError as e:
            if e.code != 405:
                raise
    return {}


def mac_app_bundle_path(executable_path: Path) -> str:
    text = str(executable_path)
    marker = ".app/"
    if marker not in text:
        return ""
    return text[: text.index(marker) + len(".app")]


def cdp_browser_launch_command(fallback_cfg: Dict[str, Any], start_url: str = "about:blank") -> List[str]:
    executable_path = str(fallback_cfg.get("executable_path") or "").strip()
    if not executable_path:
        raise RuntimeError("真实浏览器模式缺少 browser_fallback.executable_path，无法启动专用浏览器。")
    browser_path = Path(executable_path).expanduser()
    if not browser_path.exists():
        raise RuntimeError(f"真实浏览器路径不存在：{browser_path}")
    profile_dir = Path(fallback_cfg.get("profile_dir") or HERE / "browser-profile-cdp").expanduser()
    profile_dir.mkdir(parents=True, exist_ok=True)
    port = int(fallback_cfg.get("remote_debugging_port") or 9223)
    browser_args = [
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--new-window",
        start_url or "about:blank",
    ]
    return [str(browser_path), *browser_args]


def launch_cdp_browser(fallback_cfg: Dict[str, Any], start_url: str = "about:blank") -> None:
    if cdp_browser_available(fallback_cfg):
        return
    subprocess.Popen(cdp_browser_launch_command(fallback_cfg, start_url), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not wait_for_cdp_browser(fallback_cfg, timeout=20):
        raise RuntimeError(f"{BROWSER_NOT_READY_STATUS}：真实浏览器已尝试启动，但 {cdp_endpoint(fallback_cfg)} 没有就绪。请稍后重试或重新打开采集助手专用浏览器。")


def youtube_video_id(url: str) -> str:
    parsed = urllib.parse.urlparse(str(url or ""))
    host = parsed.netloc.lower()
    if host.endswith("youtu.be"):
        return parsed.path.strip("/").split("/")[0]
    if "youtube.com" not in host:
        return ""
    query_id = (urllib.parse.parse_qs(parsed.query).get("v") or [""])[0]
    if query_id:
        return query_id
    match = re.search(r"/(?:shorts|embed)/([^/?#]+)", parsed.path)
    return match.group(1) if match else ""


def cdp_target_for_url(fallback_cfg: Dict[str, Any], url: str) -> Dict[str, Any]:
    target = normalize_url(url)
    target_video_id = youtube_video_id(target)
    blank_target: Dict[str, Any] = {}
    for item in cdp_list_targets(fallback_cfg):
        if item.get("type") != "page" or not item.get("webSocketDebuggerUrl"):
            continue
        page_url = str(item.get("url") or "")
        if page_url in {"about:blank", "chrome://new-tab-page/", "edge://newtab/"} and not blank_target:
            blank_target = item
            continue
        if target_video_id and youtube_video_id(page_url) == target_video_id:
            return item
        if normalize_url(page_url).rstrip("/") == target.rstrip("/"):
            return item
    if blank_target:
        return blank_target
    return cdp_create_target(fallback_cfg, target or "about:blank")


def cdp_page_for_url(context: Any, url: str) -> Any:
    target = normalize_url(url)
    target_video_id = youtube_video_id(target)
    blank_page = None
    for page in list(getattr(context, "pages", []) or []):
        page_url = str(getattr(page, "url", "") or "")
        if page_url in {"about:blank", "chrome://new-tab-page/", "edge://newtab/"} and blank_page is None:
            blank_page = page
            continue
        if target_video_id and youtube_video_id(page_url) == target_video_id:
            return page
        if normalize_url(page_url).rstrip("/") == target.rstrip("/"):
            return page
    if blank_page is not None:
        return blank_page
    return context.new_page()


def stop_cdp_browser(fallback_cfg: Dict[str, Any]) -> None:
    profile_dir = str(Path(fallback_cfg.get("profile_dir") or HERE / "browser-profile-cdp").expanduser())
    port = int(fallback_cfg.get("remote_debugging_port") or 9223)
    pattern = f"{re.escape(profile_dir)}.*remote-debugging-port={port}"
    subprocess.run(["pkill", "-f", pattern], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)


def connect_cdp_browser(playwright: Any, fallback_cfg: Dict[str, Any]) -> Any:
    timeout_ms = min(10000, max(3000, int(fallback_cfg.get("timeout") or 60) * 1000))
    return playwright.chromium.connect_over_cdp(cdp_endpoint(fallback_cfg), timeout=timeout_ms)


def connect_cdp_browser_with_recovery(playwright: Any, fallback_cfg: Dict[str, Any], platform: str) -> Any:
    try:
        return connect_cdp_browser(playwright, fallback_cfg)
    except Exception as first_error:
        stop_cdp_browser(fallback_cfg)
        launch_cdp_browser(fallback_cfg, login_url_for_platform(platform) or "about:blank")
        try:
            return connect_cdp_browser(playwright, fallback_cfg)
        except Exception as second_error:
            raise RuntimeError(
                f"{BROWSER_NOT_READY_STATUS}：真实浏览器连接未就绪；不是{platform}登录问题。请稍后重试，或关闭专用浏览器后重新打开采集助手。"
            ) from second_error or first_error


def launchctl_service_running(label: str) -> bool:
    if sys.platform != "darwin":
        return False
    result = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return result.returncode == 0 and "state = running" in result.stdout


def launchctl_kickstart(label: str) -> bool:
    if sys.platform != "darwin":
        return False
    result = subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def table_health_summary(cfg: Dict[str, Any]) -> Dict[str, Any]:
    names = cfg.get("fields") or DEFAULT_FIELDS
    summary: Dict[str, Any] = {"tables": [], "waiting_login": [], "blank_jobs": []}
    for table_id in discover_feishu_table_ids(cfg):
        table_cfg = with_table_id(cfg, table_id)
        table_info: Dict[str, Any] = {
            "table_id": table_id,
            "rows": 0,
            "status_counts": {},
            "blank_link_rows": 0,
            "problems": [],
        }
        try:
            records = list_records(table_cfg)
        except Exception as e:
            table_info["error"] = str(e)
            summary["tables"].append(table_info)
            continue
        counts: Dict[str, int] = {}
        table_info["rows"] = len(records)
        for index, record in enumerate(records, start=1):
            fields = record.get("fields") or {}
            record_id = str(record.get("record_id") or "")
            url = normalize_url(as_text(fields.get(names["url"])))
            if not url:
                continue
            status = as_text(fields.get(names["status"])) or "空"
            counts[status] = counts.get(status, 0) + 1
            if status == "空" and should_process_blank_record(record, table_cfg):
                table_info["blank_link_rows"] += 1
                summary["blank_jobs"].append((table_id, record_id))
            if status in (LOGIN_STATUSES | RETRY_LOGIN_STATUSES):
                platform = detect_platform(url)
                summary["waiting_login"].append({"table_id": table_id, "record_id": record_id, "platform": platform})
                table_info["problems"].append({
                    "row": index,
                    "record_id": record_id,
                    "status": status,
                    "platform": platform,
                    "error": as_text(fields.get(names["error"]))[:180],
                })
            elif status in BROWSER_RETRY_STATUSES:
                platform = detect_platform(url)
                table_info["problems"].append({
                    "row": index,
                    "record_id": record_id,
                    "status": status,
                    "platform": platform,
                    "error": as_text(fields.get(names["error"]))[:180],
                })
        table_info["status_counts"] = counts
        summary["tables"].append(table_info)
    return summary


def run_health_check(cfg: Dict[str, Any], repair: bool = False) -> Dict[str, Any]:
    health = health_config(cfg)
    result: Dict[str, Any] = {
        "ok": True,
        "checked_at": now_text(),
        "listener": {},
        "browser": {},
        "tables": {},
        "actions": [],
    }

    label = str(health.get("listener_label") or DEFAULT_EVENT_LISTENER_LABEL)
    listener_running = launchctl_service_running(label)
    result["listener"] = {"label": label, "running": listener_running, "status": "running" if listener_running else "stopped"}
    if repair and not listener_running:
        restarted = launchctl_kickstart(label)
        result["listener"]["status"] = "restarted" if restarted else "restart_failed"
        result["actions"].append(f"listener:{result['listener']['status']}")

    if health.get("check_browser", True):
        fallback_cfg = browser_fallback_config(cfg)
        if not fallback_cfg.get("enabled", True):
            result["browser"] = {"status": "disabled"}
        elif cdp_browser_available(fallback_cfg):
            result["browser"] = {"status": "running", "endpoint": cdp_endpoint(fallback_cfg)}
        elif repair:
            try:
                launch_cdp_browser(fallback_cfg, login_url_for_platform("抖音") or "about:blank")
                result["browser"] = {"status": "started", "endpoint": cdp_endpoint(fallback_cfg)}
                result["actions"].append("browser:started")
            except Exception as e:
                result["browser"] = {"status": "start_failed", "error": str(e)}
                result["ok"] = False
        else:
            result["browser"] = {"status": "stopped", "endpoint": cdp_endpoint(fallback_cfg)}

    if health.get("check_tables", True):
        try:
            table_summary = table_health_summary(cfg)
            result["tables"] = table_summary
            if table_summary.get("waiting_login"):
                result["ok"] = False
            if repair and table_summary.get("blank_jobs"):
                if launchctl_kickstart(label):
                    result["actions"].append(f"listener:restarted_for_blank_jobs:{len(table_summary.get('blank_jobs', []))}")
                else:
                    result["actions"].append(f"listener:restart_failed_for_blank_jobs:{len(table_summary.get('blank_jobs', []))}")
        except Exception as e:
            result["tables"] = {"error": str(e)}
            result["ok"] = False
    return result


DOUYIN_BROWSER_DETAIL_SCRIPT = """async (id) => {
    try {
      const resp = await fetch(`/aweme/v1/web/aweme/detail/?aweme_id=${id}`, {
        credentials: "include",
        headers: {accept: "application/json"}
      });
      if (!resp.ok) return {__status: resp.status};
      return await resp.json();
    } catch (error) {
      return {__error: String(error && error.message || error)};
    }
}"""


def fetch_douyin_detail_from_browser_page(page: Any, aweme_id: str, attempts: int = 5, wait_ms: int = 1200) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for index in range(max(1, attempts)):
        try:
            got = page.evaluate(DOUYIN_BROWSER_DETAIL_SCRIPT, aweme_id)
            payload = got if isinstance(got, dict) else {}
        except Exception:
            payload = {}
        detail = payload.get("aweme_detail") if isinstance(payload, dict) else {}
        if isinstance(detail, dict) and detail:
            return detail
        if index < attempts - 1:
            try:
                page.wait_for_timeout(min(wait_ms * (index + 1), 3000))
            except Exception:
                time.sleep(min(wait_ms * (index + 1), 3000) / 1000)
    return {}


def extract_douyin_with_browser_api(url: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    aweme_id = douyin_aweme_id(url)
    if not aweme_id:
        return {}
    target_url = canonical_douyin_video_url(url)
    fallback_cfg = browser_fallback_config(cfg)
    if not fallback_cfg.get("enabled", True):
        return {}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError("真实浏览器模式缺少 Playwright；请先安装 playwright。") from e

    with BROWSER_FALLBACK_LOCK:
        launch_cdp_browser(fallback_cfg, target_url)
        with sync_playwright() as p:
            browser = connect_cdp_browser_with_recovery(p, fallback_cfg, "抖音")
            context = browser.contexts[0] if browser.contexts else browser.new_context(viewport={"width": 1280, "height": 900})
            page = None
            should_close_page = False
            try:
                for ctx_page in context.pages:
                    if aweme_id and aweme_id in (ctx_page.url or ""):
                        page = ctx_page
                        break
                if page is None:
                    for ctx_page in context.pages:
                        if "douyin.com" in (ctx_page.url or ""):
                            page = ctx_page
                            break
                if page is None:
                        page = context.new_page()
                        should_close_page = True
                if aweme_id not in (page.url or ""):
                    try:
                        page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
                    except Exception:
                        pass
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                detail = fetch_douyin_detail_from_browser_page(page, aweme_id)
            finally:
                if should_close_page and page is not None:
                    try:
                        page.close()
                    except Exception:
                        pass
                if not fallback_cfg.get("keep_open", True):
                    browser.close()
    return douyin_detail_to_meta(url, url, detail) if detail else {}


def extract_xiaohongshu_with_browser_fetch(url: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    if "xiaohongshu.com" not in (urllib.parse.urlparse(url).netloc or ""):
        return {}
    fallback_cfg = browser_fallback_config(cfg)
    if not fallback_cfg.get("enabled", True):
        return {}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError("真实浏览器模式缺少 Playwright；请先安装 playwright。") from e

    with BROWSER_FALLBACK_LOCK:
        launch_cdp_browser(fallback_cfg, login_url_for_platform("小红书"))
        with sync_playwright() as p:
            browser = connect_cdp_browser_with_recovery(p, fallback_cfg, "小红书")
            context = browser.contexts[0] if browser.contexts else browser.new_context(viewport={"width": 1280, "height": 900})
            page = None
            should_close_page = False
            try:
                for ctx_page in context.pages:
                    if "xiaohongshu.com" in (ctx_page.url or ""):
                        page = ctx_page
                        break
                if page is None:
                    page = context.new_page()
                    should_close_page = True
                    page.goto(login_url_for_platform("小红书"), wait_until="domcontentloaded", timeout=15000)
                payload = page.evaluate(
                    """async (target) => {
                        try {
                          const resp = await fetch(target, {
                            credentials: "include",
                            headers: {accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
                          });
                          const text = await resp.text();
                          return {status: resp.status, url: resp.url, text: text.slice(0, 1200000)};
                        } catch (error) {
                          return {__error: String(error && error.message || error)};
                        }
                    }""",
                    url,
                )
            finally:
                if should_close_page and page is not None:
                    try:
                        page.close()
                    except Exception:
                        pass
                if not fallback_cfg.get("keep_open", True):
                    browser.close()
    if not isinstance(payload, dict) or payload.get("__error"):
        return {}
    text = str(payload.get("text") or "")
    final_url = str(payload.get("url") or url)
    if not text:
        return {}
    return extract_xiaohongshu_meta(url, text, final_url)


def read_page_content_with_retry(page: Any, attempts: int = 3) -> str:
    last_error: Optional[Exception] = None
    for index in range(max(1, attempts)):
        try:
            return page.content()
        except Exception as e:
            last_error = e
            if "navigating" not in str(e).lower() or index == attempts - 1:
                raise
            try:
                page.wait_for_timeout(800)
            except Exception:
                time.sleep(0.8)
    raise last_error or RuntimeError("读取页面内容失败")


def extract_with_real_browser(url: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    fallback_cfg = browser_fallback_config(cfg)
    if not fallback_cfg.get("enabled", True):
        return {}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError("真实浏览器模式缺少 Playwright；请先安装 playwright。") from e

    platform = detect_platform(url)
    target_url = canonical_douyin_video_url(url) if platform == "抖音" else url
    timeout_ms = fallback_cfg["timeout"] * 1000
    browser = None
    page = None
    browser_api_payload: Dict[str, Any] = {}

    with BROWSER_FALLBACK_LOCK:
        launch_cdp_browser(fallback_cfg, target_url if platform == "抖音" else (login_url_for_platform(platform) or "about:blank"))
        with sync_playwright() as p:
            try:
                browser = connect_cdp_browser(p, fallback_cfg)
            except Exception:
                stop_cdp_browser(fallback_cfg)
                launch_cdp_browser(fallback_cfg, target_url if platform == "抖音" else (login_url_for_platform(platform) or "about:blank"))
                try:
                    browser = connect_cdp_browser(p, fallback_cfg)
                except Exception as e:
                    raise RuntimeError(
                        f"{BROWSER_NOT_READY_STATUS}：真实浏览器连接未就绪；不是{platform}登录问题。请稍后重试，或关闭专用浏览器后重新打开采集助手。"
                    ) from e
            try:
                context = browser.contexts[0] if browser.contexts else browser.new_context(
                    viewport={"width": 1280, "height": 900}
                )
                page = context.new_page()
                try:
                    page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    page.wait_for_timeout(3000)
                    final_url = page.url
                    content = read_page_content_with_retry(page)
                    page_data = page.evaluate(
                        """() => {
                            const meta = (name) => {
                              const escaped = name.replace(/"/g, '\\"');
                              const el = document.querySelector(`meta[property="${escaped}"], meta[name="${escaped}"]`);
                              return el ? (el.getAttribute("content") || "") : "";
                            };
                            const videos = Array.from(document.querySelectorAll("video"))
                              .map(v => v.currentSrc || v.src || "")
                              .filter(Boolean);
                            const primaryVideo = document.querySelector("video");
                            const body = document.body ? document.body.innerText : "";
                            return {
                              title: document.title || "",
                              description: meta("description") || meta("og:description"),
                              cover_url: meta("og:image") || meta("twitter:image"),
                              video_url: meta("og:video") || videos[0] || "",
                              video_duration: primaryVideo && Number.isFinite(primaryVideo.duration) ? primaryVideo.duration : "",
                              body: body.slice(0, 5000)
                            };
                        }"""
                    )
                    browser_api_payload = {}
                    if platform == "抖音":
                        browser_api_payload = page.evaluate(
                            """async () => {
                                const id = location.pathname.match(/\\/video\\/(\\d+)/)?.[1]
                                  || location.search.match(/[?&]aweme_id=(\\d+)/)?.[1]
                                  || "";
                                if (!id) return {};
                                try {
                                  const resp = await fetch(`/aweme/v1/web/aweme/detail/?aweme_id=${id}`, {
                                    credentials: "include",
                                    headers: {accept: "application/json"}
                                  });
                                  if (!resp.ok) return {__status: resp.status};
                                  return await resp.json();
                                } catch (error) {
                                  return {__error: String(error && error.message || error)};
                                }
                            }"""
                        )
                except Exception as e:
                    text = str(e)
                    if "Target page" in text or "context or browser has been closed" in text:
                        raise RuntimeError(
                            f"{BROWSER_CONNECTION_STATUS}：真实浏览器页面已关闭或连接断开；不是{platform}登录问题。请重新打开专用浏览器后再采集。"
                        ) from e
                    raise
            finally:
                if page is not None:
                    try:
                        page.close()
                    except Exception:
                        pass
                if browser and not fallback_cfg.get("keep_open", True):
                    browser.close()

    meta = extract_from_html(url, content, final_url, platform)
    if platform == "抖音" and isinstance(browser_api_payload, dict):
        detail = browser_api_payload.get("aweme_detail") or {}
        if detail:
            meta = merge_meta(meta, douyin_detail_to_meta(url, final_url, detail))
    body = str(page_data.get("body") or "")
    visible_duration = to_duration(page_data.get("video_duration"))
    if platform == "抖音" and visible_duration:
        meta["duration"] = visible_duration
    published_from_body = visible_published_at(body)
    if platform == "抖音" and published_from_body:
        meta["published_at"] = published_from_body
    visible_title = ""
    for line in body.splitlines():
        line = usable_browser_title(line)
        if 6 <= len(line) <= 140 and not re.search(r"登录|验证码|扫码|首页|推荐|关注|消息", line):
            visible_title = line
            break
    browser_title = (
        usable_browser_title(str(page_data.get("description") or ""))
        or usable_browser_title(str(page_data.get("title") or ""))
        or visible_title
    )
    browser_meta = {
        "source_url": url,
        "final_url": final_url,
        "platform": platform,
        "title": browser_title,
        "caption": "",
        "cover_url": normalize_resource_url(page_data.get("cover_url") or "", final_url),
        "duration": "",
        "likes": None,
        "comments": None,
        "shares": None,
        "published_at": "",
        "media_url": normalize_resource_url(page_data.get("video_url") or "", final_url),
    }
    return merge_meta(meta, browser_meta)


async def cdp_evaluate_expression(websocket_url: str, expression: str, timeout: float = 12.0) -> Any:
    try:
        import websockets
    except ImportError as e:
        raise RuntimeError("真实浏览器 CDP 模式缺少 websockets；请先安装 websockets。") from e

    async with websockets.connect(websocket_url, max_size=16_000_000, open_timeout=timeout) as ws:
        msg_id = 1

        async def call(method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            nonlocal msg_id
            current_id = msg_id
            msg_id += 1
            await ws.send(json.dumps({"id": current_id, "method": method, "params": params or {}}))
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                msg = json.loads(raw)
                if msg.get("id") == current_id:
                    if msg.get("error"):
                        raise RuntimeError(json.dumps(msg["error"], ensure_ascii=False))
                    return msg

        await call("Runtime.enable")
        response = await call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
    result = ((response.get("result") or {}).get("result") or {})
    if result.get("subtype") == "error":
        raise RuntimeError(str(result.get("description") or result.get("value") or "CDP Runtime.evaluate failed"))
    return result.get("value")


def cdp_evaluate_sync(websocket_url: str, expression: str, timeout: float = 12.0) -> Any:
    try:
        return asyncio.run(cdp_evaluate_expression(websocket_url, expression, timeout=timeout))
    except RuntimeError as e:
        if "asyncio.run() cannot be called from a running event loop" not in str(e):
            raise
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(cdp_evaluate_expression(websocket_url, expression, timeout=timeout))
    finally:
        loop.close()


YOUTUBE_TRANSCRIPT_CDP_SCRIPT = r"""
(async () => {
  const clean = (text) => String(text || '').replace(/\s+/g, ' ').trim();
  const readSegments = () => {
    const selectors = [
      'ytd-transcript-segment-renderer',
      'yt-formatted-string.segment-text',
      '[class*="segment-text"]',
      '[class*="transcript-segment"]'
    ];
    const parts = [];
    for (const selector of selectors) {
      for (const node of Array.from(document.querySelectorAll(selector))) {
        let text = clean(node.innerText || node.textContent || '');
        text = text.replace(/^\d{1,2}:\d{2}(?::\d{2})?\s*/, '').trim();
        if (text && !/^\d{1,2}:\d{2}(?::\d{2})?$/.test(text)) parts.push(text);
      }
      if (parts.length) break;
    }
    return parts.join(' ');
  };
  const clickByText = (needles) => {
    const nodes = Array.from(document.querySelectorAll('button, [role="button"], ytd-button-renderer, yt-button-shape, yt-formatted-string, tp-yt-paper-button'));
    for (const node of nodes) {
      const text = clean(node.innerText || node.textContent || '');
      if (!text || !needles.some(needle => text.includes(needle))) continue;
      const clickable = node.closest('button, [role="button"], ytd-button-renderer, yt-button-shape, tp-yt-paper-button') || node;
      clickable.click();
      return text;
    }
    return '';
  };
  let got = readSegments();
  if (got) return got;
  clickByText(['...更多', '更多', 'more', 'More']);
  await new Promise(resolve => setTimeout(resolve, 600));
  got = readSegments();
  if (got) return got;
  clickByText(['显示文字稿', '打开文字稿', 'Show transcript', 'Transcript']);
  await new Promise(resolve => setTimeout(resolve, 1200));
  got = readSegments();
  if (got) return got;
  clickByText(['更多操作', 'More actions']);
  await new Promise(resolve => setTimeout(resolve, 500));
  clickByText(['显示文字稿', '打开文字稿', 'Show transcript', 'Transcript']);
  await new Promise(resolve => setTimeout(resolve, 1200));
  return readSegments();
})()
"""


def extract_youtube_transcript_with_cdp(url: str, cfg: Dict[str, Any]) -> str:
    fallback_cfg = browser_fallback_config(cfg)
    if not fallback_cfg.get("enabled", True):
        return ""
    launch_cdp_browser(fallback_cfg, url)
    target = cdp_target_for_url(fallback_cfg, url)
    websocket_url = str(target.get("webSocketDebuggerUrl") or "")
    if not websocket_url:
        raise RuntimeError("真实浏览器 CDP 页面未就绪，无法读取 YouTube 页面。")
    target_video_id = youtube_video_id(url)
    timeout = min(20, max(5, int(fallback_cfg.get("timeout") or 60)))
    deadline = time.time() + timeout
    while time.time() < deadline:
        current_url = str(cdp_evaluate_sync(websocket_url, "location.href", timeout=5) or "")
        if target_video_id and youtube_video_id(current_url) != target_video_id:
            cdp_evaluate_sync(websocket_url, f"location.href = {json.dumps(url)}", timeout=5)
            time.sleep(1.0)
            continue
        ready = str(cdp_evaluate_sync(websocket_url, "document.readyState", timeout=5) or "")
        if ready in {"interactive", "complete"}:
            break
        time.sleep(0.5)
    body_text = str(cdp_evaluate_sync(websocket_url, "document.body ? document.body.innerText : ''", timeout=8) or "")
    lowered = body_text.lower()
    if "确认你不是聊天机器人" in body_text or "sign in to confirm" in lowered:
        raise RuntimeError("YouTube 要求登录验证：请在专用浏览器里完成 YouTube 登录/机器人验证后重试。")
    player_payload = cdp_evaluate_sync(websocket_url, "window.ytInitialPlayerResponse || {}", timeout=8)
    if isinstance(player_payload, dict):
        caption_from_player = youtube_caption_from_initial_player_response(player_payload, cfg)
        if caption_from_player:
            return clean_youtube_caption_text(caption_from_player)
    try:
        transcript = cdp_evaluate_sync(websocket_url, YOUTUBE_TRANSCRIPT_CDP_SCRIPT, timeout=25)
    except (asyncio.TimeoutError, TimeoutError):
        return ""
    return clean_youtube_caption_text(str(transcript or ""))


def extract_youtube_transcript_with_browser(url: str, cfg: Dict[str, Any]) -> str:
    fallback_cfg = browser_fallback_config(cfg)
    if not fallback_cfg.get("enabled", True):
        return ""
    try:
        return extract_youtube_transcript_with_cdp(url, cfg).strip()
    except Exception as cdp_error:
        cdp_error_text = str(cdp_error)
        if "要求登录验证" in cdp_error_text or "缺少 websockets" not in cdp_error_text:
            raise
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError("真实浏览器模式缺少 Playwright；请先安装 playwright。") from e

    timeout_ms = fallback_cfg["timeout"] * 1000
    with BROWSER_FALLBACK_LOCK:
        launch_cdp_browser(fallback_cfg, url)
        with sync_playwright() as p:
            browser = connect_cdp_browser_with_recovery(p, fallback_cfg, "YouTube")
            context = browser.contexts[0] if browser.contexts else browser.new_context(viewport={"width": 1280, "height": 900})
            existing_pages = set(getattr(context, "pages", []) or [])
            page = cdp_page_for_url(context, url)
            page_was_existing_video = page in existing_pages and youtube_video_id(getattr(page, "url", "")) == youtube_video_id(url)
            try:
                if youtube_video_id(getattr(page, "url", "")) != youtube_video_id(url):
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                page.wait_for_timeout(2500)
                body_text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                lowered = str(body_text).lower()
                if "确认你不是聊天机器人" in body_text or "sign in to confirm" in lowered:
                    raise RuntimeError("YouTube 要求登录验证：请在专用浏览器里完成 YouTube 登录/机器人验证后重试。")
                player_payload = page.evaluate(
                    """() => {
                        if (window.ytInitialPlayerResponse) return window.ytInitialPlayerResponse;
                        const scripts = Array.from(document.querySelectorAll('script'));
                        for (const script of scripts) {
                          const text = script.textContent || '';
                          const marker = 'ytInitialPlayerResponse = ';
                          const start = text.indexOf(marker);
                          if (start < 0) continue;
                          const raw = text.slice(start + marker.length);
                          const end = raw.indexOf(';</script>');
                          try {
                            return JSON.parse((end >= 0 ? raw.slice(0, end) : raw).replace(/;\\s*$/, ''));
                          } catch (error) {}
                        }
                        return {};
                    }"""
                ) or {}
                if isinstance(player_payload, dict):
                    caption_from_player = youtube_caption_from_initial_player_response(player_payload, cfg)
                    if caption_from_player:
                        return clean_youtube_caption_text(caption_from_player)
                transcript = page.evaluate(
                    """async () => {
                        const clean = (text) => String(text || '').replace(/\\s+/g, ' ').trim();
                        const clickByText = (needles) => {
                          const nodes = Array.from(document.querySelectorAll('button, [role="button"], ytd-button-renderer, yt-button-shape, yt-formatted-string, tp-yt-paper-button'));
                          for (const node of nodes) {
                            const text = clean(node.innerText || node.textContent || '');
                            if (!text) continue;
                            if (!needles.some(needle => text.includes(needle))) continue;
                            const clickable = node.closest('button, [role="button"], ytd-button-renderer, yt-button-shape, tp-yt-paper-button') || node;
                            clickable.click();
                            return text;
                          }
                          return '';
                        };
                        const readSegments = () => {
                          const selectors = [
                            'ytd-transcript-segment-renderer',
                            'yt-formatted-string.segment-text',
                            '[class*="segment-text"]',
                            '[class*="transcript-segment"]'
                          ];
                          const parts = [];
                          for (const selector of selectors) {
                            for (const node of Array.from(document.querySelectorAll(selector))) {
                              let text = clean(node.innerText || node.textContent || '');
                              text = text.replace(/^\\d{1,2}:\\d{2}(?::\\d{2})?\\s*/, '').trim();
                              if (text && !/^\\d{1,2}:\\d{2}(?::\\d{2})?$/.test(text)) parts.push(text);
                            }
                            if (parts.length) break;
                          }
                          return parts.join(' ');
                        };
                        let got = readSegments();
                        if (got) return got;
                        clickByText(['...更多', '更多', 'more', 'More']);
                        await new Promise(resolve => setTimeout(resolve, 600));
                        got = readSegments();
                        if (got) return got;
                        clickByText(['显示文字稿', '打开文字稿', 'Show transcript', 'Transcript']);
                        await new Promise(resolve => setTimeout(resolve, 1200));
                        got = readSegments();
                        if (got) return got;
                        clickByText(['更多操作', 'More actions']);
                        await new Promise(resolve => setTimeout(resolve, 500));
                        clickByText(['显示文字稿', '打开文字稿', 'Show transcript', 'Transcript']);
                        await new Promise(resolve => setTimeout(resolve, 1200));
                        return readSegments();
                    }"""
                )
            finally:
                if not page_was_existing_video:
                    try:
                        page.close()
                    except Exception:
                        pass
                if not fallback_cfg.get("keep_open", True):
                    browser.close()
    return clean_youtube_caption_text(transcript)


def should_try_browser_fallback(platform: str, meta: Dict[str, Any]) -> bool:
    if platform == "抖音":
        has_counts = any(meta.get(key) not in (None, "") for key in ("likes", "comments", "shares"))
        return bool(
            not meta.get("title")
            or not meta.get("cover_url")
            or not media_url_or_empty(meta)
            or not has_counts
        )
    if platform == "小红书":
        has_counts = any(meta.get(key) not in (None, "") for key in ("likes", "comments", "shares"))
        content_type = str(meta.get("content_type") or "").lower()
        looks_video = content_type == "video" or bool(meta.get("media_url")) or (
            bool(meta.get("duration")) and meta.get("duration") != "图文"
        )
        return bool(
            not meta.get("title")
            or not meta.get("cover_url")
            or not has_counts
            or not meta.get("published_at")
            or (looks_video and not media_url_or_empty(meta))
        )
    return False


def browser_fallback_still_blocked(platform: str, meta: Dict[str, Any]) -> bool:
    if platform == "抖音":
        return should_try_browser_fallback(platform, meta)
    if platform == "小红书":
        if not meta.get("title"):
            return True
        content_type = str(meta.get("content_type") or "").lower()
        looks_video = content_type == "video" or (
            bool(meta.get("duration")) and meta.get("duration") != "图文"
        )
        if looks_video and not media_url_or_empty(meta):
            return True
        return only_title_requires_login(meta)
    return False


def should_try_ytdlp_for_meta(platform: str, meta: Dict[str, Any]) -> bool:
    if not meta.get("title"):
        return True
    if platform == "抖音" and not media_url_or_empty(meta):
        return True
    if platform == "B站" and not media_url_or_empty(meta):
        return True
    if platform in {"YouTube", "Instagram"} and not media_url_or_empty(meta):
        return True
    if platform == "小红书":
        content_type = str(meta.get("content_type") or "").lower()
        looks_video = content_type == "video" or (
            bool(meta.get("duration")) and str(meta.get("duration")) != "图文"
        )
        return bool(looks_video and not media_url_or_empty(meta))
    return False


def load_openai_key(cfg: Dict[str, Any]) -> str:
    load_dotenv()
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if key:
        return key
    raise SystemExit("缺少 OPENAI_API_KEY。请先双击「保存OpenAI密钥.command」保存你手动创建的 key。")


def download_media_file(url: str, cfg: Dict[str, Any], platform: str) -> Path:
    headers = dict(TEXT_HEADERS)
    cookie = platform_cookie(cfg, platform)
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    tmp_dir = Path(tempfile.mkdtemp(prefix="content-asr-"))
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            content_type = resp.headers.get_content_type() or "video/mp4"
            ext = mimetypes.guess_extension(content_type) or ".mp4"
            path = tmp_dir / ("media" + ext)
            with path.open("wb") as f:
                shutil.copyfileobj(resp, f)
            return path
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def download_media_with_ytdlp(url: str, cfg: Dict[str, Any]) -> Path:
    ytdlp_cfg = cfg.get("yt_dlp") or {}
    if not ytdlp_cfg.get("enabled", True):
        raise RuntimeError("yt-dlp 未启用，无法下载平台媒体用于转写。")
    exe = ytdlp_path()
    if not exe:
        raise RuntimeError("本机没有找到 yt-dlp，无法下载平台媒体用于转写。")
    tmp_dir = Path(tempfile.mkdtemp(prefix="content-asr-"))
    output = tmp_dir / "media.%(ext)s"
    download_formats = ytdlp_download_formats(cfg)
    probe_mode = bool(ytdlp_cfg.get("_download_probe"))
    timeout_seconds = int(ytdlp_cfg.get("download_probe_timeout") or 45) if probe_mode else int(ytdlp_cfg.get("download_timeout") or 180)
    max_attempts = int(ytdlp_cfg.get("download_probe_max_attempts") or 0) if probe_mode else 0
    attempt_count = 0
    errors: List[str] = []
    try:
        attempts: List[Tuple[str, Dict[str, Any]]] = []
        if youtube_should_export_browser_cookies(url, cfg):
            try:
                cookie_path = export_youtube_cookies_from_cdp(url, cfg, tmp_dir / "youtube-cookies.txt")
                attempts.append(("专用浏览器导出Cookie", cfg_with_ytdlp_cookie_file(cfg, cookie_path)))
            except Exception as e:
                errors.append(f"专用浏览器导出Cookie: {str(e)[-500:]}")
        attempts.extend((source or "cookie未配置", cfg_with_ytdlp_cookie_source(cfg, source)) for source in ytdlp_cookie_sources(url, cfg))
        for cookie_label, attempt_cfg in attempts:
            for strategy in ytdlp_download_strategies(url, attempt_cfg):
                for download_format in download_formats:
                    if max_attempts and attempt_count >= max_attempts:
                        raise RuntimeError("yt-dlp 下载媒体失败：" + " | ".join(errors[-4:]))
                    attempt_count += 1
                    for stale in tmp_dir.iterdir():
                        if stale.is_file() and stale.name.startswith("media."):
                            stale.unlink(missing_ok=True)
                    cmd = [
                        exe,
                        "--no-playlist",
                        "--no-warnings",
                        "-f",
                        download_format,
                        "-o",
                        str(output),
                        url,
                    ]
                    add_ytdlp_common_args(cmd, attempt_cfg)
                    add_ytdlp_strategy_args(cmd, strategy)
                    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout_seconds)
                    label = f"{cookie_label} / {strategy or 'default'} / {download_format}"
                    if result.returncode == 0:
                        files = [p for p in tmp_dir.iterdir() if p.is_file() and p.name.startswith("media.")]
                        if files:
                            return max(files, key=lambda p: p.stat().st_size)
                        errors.append(f"{label}: yt-dlp 下载媒体后没有生成可转写文件。")
                        continue
                    got = ((result.stderr or result.stdout or "").strip()[-800:])
                    errors.append(f"{label}: {got}")
        raise RuntimeError("yt-dlp 下载媒体失败：" + " | ".join(errors[-4:]))
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def ffmpeg_path() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "ffmpeg"
    if fallback.exists():
        return str(fallback)
    raise RuntimeError("本机没有找到 ffmpeg，无法从视频抽取音频。")


def ensure_ffmpeg_on_path() -> None:
    path = ffmpeg_path()
    ffmpeg_dir = str(Path(path).parent)
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if ffmpeg_dir and ffmpeg_dir not in parts:
        os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")


def extract_audio_file(media_path: Path) -> Path:
    audio_path = media_path.parent / "audio.mp3"
    cmd = [
        ffmpeg_path(),
        "-y",
        "-i",
        str(media_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        "48k",
        str(audio_path),
    ]
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0 or not audio_path.exists() or audio_path.stat().st_size == 0:
        raise RuntimeError("ffmpeg 抽取音频失败：" + (result.stderr or "")[-500:])
    return audio_path


def multipart_mixed(parts: List[Dict[str, Any]]) -> Tuple[bytes, str]:
    boundary = "----codex-" + uuid.uuid4().hex
    chunks: List[bytes] = []
    for part in parts:
        chunks.append(f"--{boundary}\r\n".encode())
        if "filename" in part:
            chunks.append(
                f'Content-Disposition: form-data; name="{part["name"]}"; filename="{part["filename"]}"\r\n'.encode()
            )
            chunks.append(f"Content-Type: {part.get('content_type') or 'application/octet-stream'}\r\n\r\n".encode())
            chunks.append(part["data"])
        else:
            chunks.append(f'Content-Disposition: form-data; name="{part["name"]}"\r\n\r\n'.encode())
            chunks.append(str(part.get("value", "")).encode("utf-8"))
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


def openai_transcribe_file(cfg: Dict[str, Any], path: Path) -> str:
    api_key = load_openai_key(cfg)
    openai_cfg = cfg.get("openai") or {}
    parts = [
        {"name": "model", "value": openai_cfg.get("transcribe_model") or "gpt-4o-transcribe"},
        {"name": "response_format", "value": "json"},
        {
            "name": "file",
            "filename": path.name,
            "content_type": mimetypes.guess_type(str(path))[0] or "video/mp4",
            "data": path.read_bytes(),
        },
    ]
    if openai_cfg.get("language"):
        parts.insert(1, {"name": "language", "value": openai_cfg["language"]})
    body, boundary = multipart_mixed(parts)
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/transcriptions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI 转写失败 HTTP {e.code}: {raw[:500]}")
    text = str(payload.get("text") or "").strip()
    if not text:
        raise RuntimeError("OpenAI 转写返回为空")
    return text


def load_tencent_asr_credentials(cfg: Dict[str, Any]) -> Tuple[str, str]:
    load_dotenv()
    tencent_cfg = cfg.get("tencent_asr") or {}
    secret_id = (os.environ.get("TENCENTCLOUD_SECRET_ID") or tencent_cfg.get("secret_id") or "").strip()
    secret_key = (os.environ.get("TENCENTCLOUD_SECRET_KEY") or tencent_cfg.get("secret_key") or "").strip()
    if not secret_id or not secret_key:
        raise RuntimeError("缺少腾讯云 ASR 密钥：请配置 tencent_asr.secret_id / secret_key。")
    return secret_id, secret_key


def tencent_create_rec_task_payload(audio: bytes, tencent_cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "EngineModelType": tencent_cfg.get("engine_model_type") or "16k_zh",
        "ChannelNum": int(tencent_cfg.get("channel_num") or 1),
        "ResTextFormat": int(tencent_cfg.get("res_text_format") or 3),
        "SourceType": 1,
        "Data": base64.b64encode(audio).decode("ascii"),
        "DataLen": len(audio),
        "FilterDirty": 0,
        "FilterPunc": 0,
        "FilterModal": 0,
        "ConvertNumMode": 1,
    }


def tencent_tc3_headers(
    secret_id: str,
    secret_key: str,
    action: str,
    payload: bytes,
    region: str,
    timestamp: Optional[int] = None,
) -> Dict[str, str]:
    service = "asr"
    host = "asr.tencentcloudapi.com"
    version = "2019-06-14"
    timestamp = int(timestamp or time.time())
    date = dt.datetime.utcfromtimestamp(timestamp).strftime("%Y-%m-%d")
    hashed_payload = hashlib.sha256(payload).hexdigest()
    canonical_headers = f"content-type:application/json; charset=utf-8\nhost:{host}\n"
    signed_headers = "content-type;host"
    canonical_request = "\n".join(["POST", "/", "", canonical_headers, signed_headers, hashed_payload])
    credential_scope = f"{date}/{service}/tc3_request"
    string_to_sign = "\n".join([
        "TC3-HMAC-SHA256",
        str(timestamp),
        credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    def sign(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    secret_date = sign(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = sign(secret_date, service)
    secret_signing = sign(secret_service, "tc3_request")
    signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    authorization = (
        "TC3-HMAC-SHA256 "
        f"Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "Authorization": authorization,
        "Content-Type": "application/json; charset=utf-8",
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Version": version,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Region": region,
    }


def tencent_asr_request(cfg: Dict[str, Any], action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    secret_id, secret_key = load_tencent_asr_credentials(cfg)
    tencent_cfg = cfg.get("tencent_asr") or {}
    region = tencent_cfg.get("region") or "ap-shanghai"
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        "https://asr.tencentcloudapi.com/",
        data=body,
        headers=tencent_tc3_headers(secret_id, secret_key, action, body, region),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"腾讯云 ASR 请求失败 HTTP {e.code}: {raw[:500]}")
    response = data.get("Response") or {}
    if response.get("Error"):
        err = response["Error"]
        raise RuntimeError(f"腾讯云 ASR 失败：{err.get('Code') or ''} {err.get('Message') or ''}".strip())
    return response


def clean_tencent_transcript(text: str) -> str:
    lines: List[str] = []
    for line in (text or "").splitlines():
        clean = re.sub(r"^\s*\[[^\]]+\]\s*", "", line).strip()
        if clean:
            lines.append(clean)
    return "\n".join(lines).strip()


def tencent_result_text(data: Dict[str, Any]) -> str:
    details = data.get("ResultDetail") or []
    sentences = [
        str(item.get("FinalSentence") or "").strip()
        for item in details
        if isinstance(item, dict) and str(item.get("FinalSentence") or "").strip()
    ]
    if sentences:
        return "\n".join(sentences).strip()
    return clean_tencent_transcript(str(data.get("Result") or ""))


def tencent_transcribe_file(cfg: Dict[str, Any], path: Path) -> str:
    tencent_cfg = cfg.get("tencent_asr") or {}
    max_bytes = int(float(tencent_cfg.get("max_local_upload_mb") or 5) * 1024 * 1024)
    audio_size = path.stat().st_size
    if audio_size > max_bytes:
        raise RuntimeError(f"腾讯云本地音频上传限制：{audio_size} bytes > {max_bytes} bytes；已切本地转写。")
    response = tencent_asr_request(
        cfg,
        "CreateRecTask",
        tencent_create_rec_task_payload(path.read_bytes(), tencent_cfg),
    )
    task_id = ((response.get("Data") or {}).get("TaskId"))
    if task_id is None:
        raise RuntimeError("腾讯云 ASR 未返回 TaskId。")

    deadline = time.time() + int(tencent_cfg.get("timeout") or 180)
    interval = max(1, int(tencent_cfg.get("poll_interval") or 3))
    last_status = ""
    while time.time() < deadline:
        time.sleep(interval)
        status_resp = tencent_asr_request(cfg, "DescribeTaskStatus", {"TaskId": int(task_id)})
        data = status_resp.get("Data") or {}
        status = int(data.get("Status") if data.get("Status") is not None else -1)
        last_status = str(data.get("StatusStr") or status)
        if status == 2:
            text = tencent_result_text(data)
            if not text:
                raise RuntimeError("腾讯云 ASR 返回为空。")
            return text
        if status == 3:
            raise RuntimeError("腾讯云 ASR 任务失败：" + str(data.get("ErrorMsg") or last_status))
    raise RuntimeError(f"腾讯云 ASR 超时，最后状态：{last_status or 'unknown'}")


def local_whisper_transcribe_file(cfg: Dict[str, Any], path: Path) -> str:
    try:
        import whisper  # type: ignore
    except ImportError:
        raise RuntimeError("本地 Whisper 未安装。请先运行「安装本地Whisper.command」。")
    ensure_ffmpeg_on_path()
    asr_cfg = cfg.get("asr") or {}
    model_name = asr_cfg.get("local_model") or "base"
    language = asr_cfg.get("language") or "zh"
    model = whisper.load_model(model_name)
    result = model.transcribe(
        str(path),
        language=language,
        fp16=False,
        initial_prompt=asr_cfg.get("initial_prompt") or None,
        condition_on_previous_text=True,
    )
    text = str(result.get("text") or "").strip()
    if not text:
        raise RuntimeError("本地 Whisper 转写返回为空")
    if asr_cfg.get("format_transcript", True):
        text = format_transcript_text(text)
    return text


def format_transcript_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return ""
    if re.search(r"[。！？；：,.!?]", text):
        text = re.sub(r"\s*([。！？；：,.!?])\s*", r"\1", text)
        text = re.sub(r"([。！？])", r"\1\n", text)
        return re.sub(r"\n{2,}", "\n", text).strip()

    break_words = [
        "大家好", "今天", "首先", "第一", "第二", "第三", "然后", "所以", "但是",
        "其实", "因为", "如果", "比如", "最后", "总结一下", "说白了", "你会发现",
    ]
    for word in break_words:
        text = text.replace(word, "。" + word)
    text = text.lstrip("。")

    chunks: List[str] = []
    current = ""
    for part in re.split(r"(。)", text):
        if not part:
            continue
        current += part
        if part == "。" or len(current) >= 80:
            chunks.append(current.rstrip("。") + "。")
            current = ""
    if current:
        chunks.append(current.rstrip("。") + "。")
    return "\n".join(x.strip() for x in chunks if x.strip())


def transcribe_audio_file(cfg: Dict[str, Any], path: Path) -> str:
    backend = ((cfg.get("asr") or {}).get("backend") or "local").lower()
    if backend == "tencent":
        return tencent_transcribe_file(cfg, path)
    if backend == "tencent_auto":
        try:
            return tencent_transcribe_file(cfg, path)
        except Exception as tencent_error:
            try:
                return local_whisper_transcribe_file(cfg, path)
            except Exception as local_error:
                raise RuntimeError(f"腾讯云 ASR 失败：{tencent_error}；本地 Whisper 失败：{local_error}")
    if backend == "openai":
        return openai_transcribe_file(cfg, path)
    if backend == "local":
        return local_whisper_transcribe_file(cfg, path)
    if backend == "auto":
        try:
            return local_whisper_transcribe_file(cfg, path)
        except Exception as local_error:
            try:
                return openai_transcribe_file(cfg, path)
            except Exception as openai_error:
                raise RuntimeError(f"本地 Whisper 失败：{local_error}；OpenAI 失败：{openai_error}")
    raise RuntimeError(f"未知 ASR backend：{backend}，可用值：local / openai / auto / tencent / tencent_auto")


def transcribe_from_meta(cfg: Dict[str, Any], meta: Dict[str, Any]) -> str:
    media_url = media_url_or_empty(meta)
    source_url = str(meta.get("final_url") or meta.get("source_url") or "").strip()
    platform = meta.get("platform") or ""
    fallback_cfg = cfg.get("browser_fallback")
    if platform == "YouTube" and source_url and isinstance(fallback_cfg, dict) and fallback_cfg.get("enabled", True):
        transcript = extract_youtube_transcript_with_browser(source_url, cfg).strip()
        if transcript:
            return format_transcript_text(transcript) if (cfg.get("asr") or {}).get("format_transcript", True) else transcript
    browser_transcript_empty = bool(platform == "YouTube" and source_url and isinstance(fallback_cfg, dict) and fallback_cfg.get("enabled", True))
    try:
        if platform in {"B站", "YouTube", "Instagram"} and source_url:
            path = download_media_with_ytdlp(source_url, cfg)
        elif media_url:
            path = download_media_file(media_url, cfg, platform)
        else:
            raise RuntimeError("未拿到视频/音频直链；需要平台登录 Cookie 或浏览器采集模式。")
    except Exception as e:
        if browser_transcript_empty:
            raise RuntimeError(f"YouTube 浏览器文字稿为空，已尝试音频 ASR 兜底但失败：{e}") from e
        raise
    try:
        audio_path = extract_audio_file(path)
        return transcribe_audio_file(cfg, audio_path)
    finally:
        shutil.rmtree(path.parent, ignore_errors=True)


def meta_content(text: str, *names: str) -> str:
    for name in names:
        patterns = [
            rf'<meta[^>]+(?:property|name)=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']*)["\']',
            rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']{re.escape(name)}["\']',
        ]
        for pat in patterns:
            m = re.search(pat, text, flags=re.I | re.S)
            if m:
                return html.unescape(m.group(1)).strip()
    return ""


def title_tag(text: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.I | re.S)
    return html.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else ""


def javascript_object_to_json(raw: str) -> str:
    """Replace bare JavaScript literals without touching quoted text."""
    replacements = {"undefined": "null", "NaN": "null", "Infinity": "null"}
    out: List[str] = []
    quote = ""
    escaped = False
    i = 0
    while i < len(raw):
        char = raw[i]
        if quote:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            i += 1
            continue
        if char in ('"', "'"):
            quote = char
            out.append(char)
            i += 1
            continue
        replaced = False
        for source, target in replacements.items():
            if not raw.startswith(source, i):
                continue
            before = raw[i - 1] if i else ""
            after_index = i + len(source)
            after = raw[after_index] if after_index < len(raw) else ""
            if (not before or not (before.isalnum() or before in "_$")) and (
                not after or not (after.isalnum() or after in "_$")
            ):
                out.append(target)
                i = after_index
                replaced = True
                break
        if not replaced:
            out.append(char)
            i += 1
    return "".join(out)


def iter_json_objects(text: str) -> Iterable[Any]:
    render = re.search(r'<script[^>]+id=["\']RENDER_DATA["\'][^>]*>(.*?)</script>', text, flags=re.I | re.S)
    if render:
        raw = urllib.parse.unquote(html.unescape(render.group(1)))
        try:
            yield json.loads(raw)
        except Exception:
            pass

    for marker in ("window.__INITIAL_STATE__=", "window.__INITIAL_STATE__ =", "__INITIAL_STATE__="):
        index = text.find(marker)
        if index < 0:
            continue
        raw = html.unescape(text[index + len(marker):]).lstrip()
        try:
            obj, _ = json.JSONDecoder().raw_decode(javascript_object_to_json(raw))
            yield obj
        except Exception:
            continue

    next_data = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', text, flags=re.I | re.S)
    if next_data:
        try:
            yield json.loads(html.unescape(next_data.group(1)))
        except Exception:
            pass

    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', text, flags=re.I | re.S):
        try:
            yield json.loads(html.unescape(m.group(1)))
        except Exception:
            continue


def walk_json(obj: Any) -> Iterable[Tuple[str, Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k), v
            yield from walk_json(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk_json(item)


def pick_first_json(obj: Any, keys: Iterable[str]) -> Any:
    wanted = {x.lower() for x in keys}
    for k, v in walk_json(obj):
        if k.lower() in wanted and v not in ("", None, [], {}):
            return v
    return None


def to_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).replace(",", "").strip()
    m = re.search(r"([\d.]+)\s*([w万kK]?)", text)
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2).lower()
    if unit in ("w", "万"):
        num *= 10000
    elif unit == "k":
        num *= 1000
    return int(num)


def to_duration(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str) and ":" in value:
        return value.strip()
    try:
        seconds = float(value)
        if seconds > 10000:
            seconds = seconds / 1000
        seconds = int(round(seconds))
        return f"{seconds // 60:02d}:{seconds % 60:02d}"
    except Exception:
        return str(value).strip()


def visible_published_at(text: str) -> str:
    m = re.search(r"发布时间[:：]\s*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}(?:[日\s]+\d{1,2}:\d{2}(?::\d{2})?)?)", text or "")
    return m.group(1).strip() if m else ""


def to_time_text(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        value = value.strip()
        if re.search(r"\d{4}[-/年]\d{1,2}", value):
            return value
        if value.isdigit():
            value = int(value)
        else:
            return value
    try:
        ts = int(value)
        if ts > 10_000_000_000:
            ts = ts // 1000
        return dt.datetime.fromtimestamp(ts).strftime("%Y年%m月%d日%H时%M分%S秒")
    except Exception:
        return str(value)


def first_url(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        for item in value:
            got = first_url(item)
            if got:
                return got
    if isinstance(value, dict):
        for key in ("url_list", "urlList", "urls", "backupUrls", "backup_urls"):
            got = first_url(value.get(key))
            if got and got.startswith(("http://", "https://")):
                return got
        for key in (
            "url",
            "urlDefault",
            "urlPre",
            "url_pre",
            "cover",
            "coverUrl",
            "originCover",
            "dynamicCover",
            "thumbnail",
            "thumbnailUrl",
            "src",
            "uri",
            "masterUrl",
            "mainUrl",
            "videoUrl",
        ):
            got = first_url(value.get(key))
            if got:
                return got
    return ""


def clean_title(raw: str) -> str:
    raw = re.sub(r"\s+", " ", raw or "").strip()
    raw = re.sub(r"\s*[-_｜|· ]+\s*(抖音|小红书|微信|视频号|YouTube|Instagram)\s*$", "", raw, flags=re.I).strip()
    return raw


def usable_browser_title(title: str) -> str:
    title = clean_title(title)
    if not title:
        return ""
    lowered = title.lower()
    blocked = {
        "pc tab",
        "new tab",
        "about:blank",
        "microsoft edge",
        "douyin",
        "youtube",
        "instagram",
        "抖音",
        "登录",
        "验证码",
        "安全验证",
        "开启读屏标签",
        "视频数据加载中",
    }
    if lowered in blocked or title in blocked:
        return ""
    if re.fullmatch(r"\d{4}\s*[©@]\s*", title):
        return ""
    if re.fullmatch(r"\d{1,2}:\d{2}\s*/\s*\d{1,2}:\d{2}", title):
        return ""
    if re.search(r"[京沪粤浙苏津渝闽湘鲁豫冀辽吉黑皖鄂桂琼川贵云藏陕甘青宁新]icp备?\d+号", title, re.I):
        return ""
    if re.search(r"登录|验证码|扫码|安全验证|访问过于频繁|读屏标签|数据加载中|加载中", title):
        return ""
    return title


def usable_caption_text(caption: str) -> str:
    caption = str(caption or "").strip()
    if not caption:
        return ""
    if re.search(r"cache_switch|language_list|language_code|only_oversea|字幕配置", caption):
        return ""
    return caption


def xhs_note_id_from_url(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    match = re.search(r"/(?:item|explore)/([A-Za-z0-9]+)", path)
    return match.group(1) if match else ""


def xhs_note_candidates(obj: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(obj, dict):
        note = obj.get("note")
        if isinstance(note, dict):
            yield note
        note_map = obj.get("noteDetailMap") or obj.get("note_detail_map")
        if isinstance(note_map, dict):
            for note_id, item in note_map.items():
                if isinstance(item, dict):
                    nested = item.get("note") or item.get("noteDetail") or item.get("detail")
                    if isinstance(nested, dict):
                        candidate = dict(nested)
                        candidate.setdefault("_map_note_id", note_id)
                        yield candidate
                    elif any(k in item for k in ("title", "desc", "interactInfo", "video", "imageList")):
                        candidate = dict(item)
                        candidate.setdefault("_map_note_id", note_id)
                        yield candidate
        for value in obj.values():
            yield from xhs_note_candidates(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from xhs_note_candidates(item)


def xhs_video_url(note: Dict[str, Any]) -> str:
    video = note.get("video") or note.get("videoInfo") or note.get("video_info") or {}
    for _, value in walk_json(video):
        if isinstance(value, str) and value.startswith(("http://", "https://")) and re.search(r"\.(mp4|m3u8)(?:\?|$)", value):
            return value
    return ""


def xhs_cover_url(note: Dict[str, Any]) -> str:
    for key in ("cover", "coverUrl", "imageList", "image_list", "images", "image"):
        got = first_url(note.get(key))
        if got:
            return got
    return ""


def extract_xiaohongshu_meta(url: str, text: str, final_url: str) -> Dict[str, Any]:
    candidates: List[Dict[str, Any]] = []
    for obj in iter_json_objects(text):
        for note in xhs_note_candidates(obj):
            if note.get("title") or note.get("desc") or note.get("interactInfo") or note.get("video") or note.get("imageList"):
                candidates.append(note)

    target_note_id = xhs_note_id_from_url(final_url) or xhs_note_id_from_url(url)

    def note_score(note: Dict[str, Any]) -> int:
        note_id = str(note.get("noteId") or note.get("note_id") or note.get("id") or note.get("_map_note_id") or "")
        score = 1000 if target_note_id and note_id == target_note_id else 0
        score += 10 if note.get("title") else 0
        score += 8 if note.get("interactInfo") or note.get("interact_info") else 0
        score += 6 if note.get("video") or note.get("videoInfo") or note.get("video_info") else 0
        score += 4 if note.get("imageList") or note.get("image_list") else 0
        return score

    best_note = max(candidates, key=note_score) if candidates else {}

    metas = {
        "title": meta_content(text, "og:title", "twitter:title", "title") or title_tag(text),
        "description": meta_content(text, "description", "og:description", "twitter:description"),
        "cover_url": meta_content(text, "og:image", "twitter:image"),
        "video_url": meta_content(text, "og:video"),
        "video_time": meta_content(text, "og:videotime"),
        "likes": meta_content(text, "og:xhs:note_like"),
        "comments": meta_content(text, "og:xhs:note_comment"),
    }

    interact = best_note.get("interactInfo") or best_note.get("interact_info") or {}
    title = clean_title(str(best_note.get("title") or best_note.get("displayTitle") or metas["title"] or ""))
    desc = str(best_note.get("desc") or best_note.get("description") or metas["description"] or "").strip()
    media_url = xhs_video_url(best_note) or metas["video_url"]
    is_video = bool(media_url or str(best_note.get("type") or "").lower() == "video" or best_note.get("video"))
    cover_url = xhs_cover_url(best_note) or metas["cover_url"]
    duration = pick_first_json(best_note, ["duration", "videoDuration", "video_duration"]) or metas["video_time"]

    share_count = (
        interact.get("shareCount")
        or interact.get("share_count")
        or best_note.get("shareCount")
        or best_note.get("share_count")
    )

    return {
        "source_url": url,
        "final_url": final_url,
        "platform": "小红书",
        "content_type": "video" if is_video else "image",
        "title": title or desc[:80],
        "caption": "" if is_video else desc,
        "cover_url": normalize_resource_url(cover_url, final_url),
        "duration": to_duration(duration) if is_video else "图文",
        "likes": to_int(
            interact.get("likedCount")
            or interact.get("likeCount")
            or interact.get("liked_count")
            or best_note.get("likedCount")
            or metas["likes"]
        ),
        "comments": to_int(
            interact.get("commentCount")
            or interact.get("comment_count")
            or best_note.get("commentCount")
            or metas["comments"]
        ),
        "shares": to_int(share_count),
        "published_at": to_time_text(best_note.get("time") or best_note.get("createTime") or best_note.get("publishTime")),
        "media_url": normalize_resource_url(media_url, final_url),
    }


def extract_from_html(url: str, text: str, final_url: str, platform: str) -> Dict[str, Any]:
    platform = detect_platform(url)
    if platform == "小红书":
        return extract_xiaohongshu_meta(url, text, final_url)

    metas = {
        "title": meta_content(text, "og:title", "twitter:title", "title") or title_tag(text),
        "description": meta_content(text, "description", "og:description", "twitter:description"),
        "cover_url": meta_content(text, "og:image", "twitter:image"),
        "video_url": meta_content(text, "og:video", "og:video:url", "twitter:player:stream", "twitter:player"),
    }

    found: Dict[str, Any] = {}
    for obj in iter_json_objects(text):
        title = pick_first_json(obj, ["title", "desc", "description", "noteTitle", "displayTitle"])
        transcript = pick_first_json(obj, TRANSCRIPT_KEYS)
        cover = pick_first_json(obj, ["cover", "coverUrl", "originCover", "dynamicCover", "image", "thumbnailUrl"])
        media = pick_first_json(obj, ["mediaUrl", "media_url", "videoUrl", "video_url", "masterUrl", "mainUrl", "playUrl", "play_url"])
        duration = pick_first_json(obj, ["duration", "durationMillis", "videoDuration"])
        likes = pick_first_json(obj, ["diggCount", "likedCount", "likes", "likeCount", "liked_count"])
        comments = pick_first_json(obj, ["commentCount", "comments", "comment_count"])
        shares = pick_first_json(obj, ["shareCount", "shares", "share_count"])
        published = pick_first_json(obj, ["createTime", "create_time", "publishTime", "time", "datePublished"])
        if title and not found.get("title"):
            found["title"] = title
        if transcript and not found.get("caption"):
            found["caption"] = transcript
        if cover and not found.get("cover_url"):
            found["cover_url"] = first_url(cover)
        if media and not found.get("media_url"):
            found["media_url"] = first_url(media)
        if duration and not found.get("duration"):
            found["duration"] = to_duration(duration)
        if likes is not None and found.get("likes") is None:
            found["likes"] = to_int(likes)
        if comments is not None and found.get("comments") is None:
            found["comments"] = to_int(comments)
        if shares is not None and found.get("shares") is None:
            found["shares"] = to_int(shares)
        if published and not found.get("published_at"):
            found["published_at"] = to_time_text(published)

    title = usable_browser_title(str(found.get("title") or metas["title"] or ""))
    description = str(metas["description"] or "").strip()
    caption = usable_caption_text(str(found.get("caption") or ""))
    if not title and description:
        title = usable_browser_title(description[:80])
    media_url = normalize_resource_url(found.get("media_url") or metas["video_url"] or "", final_url)
    content_type = "video" if media_url or found.get("duration") else ""

    return {
        "source_url": url,
        "final_url": final_url,
        "platform": platform,
        "content_type": content_type,
        "title": title,
        "caption": caption,
        "cover_url": found.get("cover_url") or metas["cover_url"],
        "duration": found.get("duration") or "",
        "likes": found.get("likes"),
        "comments": found.get("comments"),
        "shares": found.get("shares"),
        "published_at": found.get("published_at") or "",
        "media_url": media_url,
    }


def extract_from_page(url: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    platform = detect_platform(url)

    if platform == "抖音":
        api_meta = extract_douyin_api(url, cfg)
        if api_meta.get("title") or api_meta.get("caption"):
            return api_meta

    if platform in {"YouTube", "Instagram"}:
        try:
            ytdlp_meta = extract_with_ytdlp(url, cfg)
            if ytdlp_meta.get("title") or ytdlp_meta.get("caption") or media_url_or_empty(ytdlp_meta):
                return ytdlp_meta
        except RuntimeError as e:
            fallback_cfg = cfg.get("browser_fallback") or {}
            if platform == "YouTube" and fallback_cfg.get("enabled", True):
                browser_meta = extract_with_real_browser(url, cfg)
                transcript = extract_youtube_transcript_with_browser(url, cfg).strip()
                if transcript:
                    browser_meta["caption"] = transcript
                if browser_meta.get("title") or browser_meta.get("caption") or media_url_or_empty(browser_meta):
                    return browser_meta
            raise

    text, final_url = fetch_text(url, cfg, platform)
    meta = extract_from_html(url, text, final_url, platform)
    tried_browser_fallback = False

    if platform == "B站":
        meta = merge_meta(meta, extract_bilibili_api(final_url or url, cfg))

    if platform == "抖音" and not (meta.get("title") or meta.get("caption") or meta.get("cover_url")):
        aweme_id = douyin_aweme_id(url)
        if aweme_id:
            share_url = f"https://www.iesdouyin.com/share/video/{aweme_id}/"
            share_text, share_final = fetch_text_optional(share_url, cfg, platform)
            if share_text:
                meta = merge_meta(meta, extract_from_html(share_url, share_text, share_final, platform))
    fallback_cfg = cfg.get("browser_fallback") or {}
    if platform == "抖音" and fallback_cfg.get("enabled", True) and should_try_browser_fallback(platform, meta):
        browser_api_url = final_url if douyin_aweme_id(final_url) else url
        try:
            meta = merge_meta(meta, extract_douyin_with_browser_api(browser_api_url, cfg))
        except RuntimeError as e:
            if not meta.get("title"):
                raise
            meta["browser_api_error"] = str(e)
    if platform == "小红书" and fallback_cfg.get("enabled", True) and should_try_browser_fallback(platform, meta):
        browser_fetch_url = final_url if xhs_note_id_from_url(final_url) else url
        try:
            meta = merge_meta(meta, extract_xiaohongshu_with_browser_fetch(browser_fetch_url, cfg))
        except RuntimeError as e:
            if not meta.get("title"):
                raise
            meta["browser_fetch_error"] = str(e)
    fallback = globals().get("extract_with_real_browser")
    if (
        platform in {"抖音", "小红书"}
        and should_try_browser_fallback(platform, meta)
        and fallback_cfg.get("enabled", True)
        and callable(fallback)
    ):
        fallback_meta = fallback(url, cfg)
        tried_browser_fallback = True
        meta = merge_meta(meta, fallback_meta)
        if browser_fallback_still_blocked(platform, meta):
            raise RuntimeError(f"等待登录：已打开{platform}真实浏览器窗口，请完成登录并打开/播放目标内容，系统会自动重试。")
    if should_try_ytdlp_for_meta(platform, meta):
        try:
            meta = merge_meta(meta, extract_with_ytdlp(url, cfg))
        except RuntimeError as e:
            if platform == "抖音" and fresh_cookie_error(e) and not tried_browser_fallback and fallback_cfg.get("enabled", True) and callable(fallback):
                fallback_meta = fallback(url, cfg)
                meta = merge_meta(meta, fallback_meta)
                if not should_try_browser_fallback(platform, meta):
                    return meta
                raise RuntimeError("等待登录：已打开抖音真实浏览器窗口，请完成登录并打开/播放目标内容，系统会自动重试。") from e
            if not meta.get("title"):
                raise
            meta["yt_dlp_error"] = str(e)
    return meta


def fetch_binary(url: str, cfg: Dict[str, Any], platform: str) -> Tuple[bytes, str, str]:
    headers = dict(TEXT_HEADERS)
    cookie = platform_cookie(cfg, platform)
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        content_type = resp.headers.get_content_type() or "image/jpeg"
        ext = mimetypes.guess_extension(content_type) or ".jpg"
        return resp.read(), content_type, ext


def ensure_http_url(url: str) -> str:
    url = url.strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("只支持打开或保存 http/https 链接")
    return url


def desktop_open_url(url: str) -> Dict[str, Any]:
    url = ensure_http_url(url)
    ok = webbrowser.open(url)
    return {"ok": bool(ok), "url": url}


def desktop_save_cover_file(
    url: str,
    cfg: Dict[str, Any],
    platform: str = "",
    downloads_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    url = ensure_http_url(url)
    data, content_type, ext = fetch_binary(url, cfg, platform)
    ext = ext if ext.startswith(".") else ".jpg"
    target_dir = downloads_dir or (Path.home() / "Downloads")
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"CHEN封面-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}{ext}"
    counter = 2
    while path.exists():
        path = target_dir / f"CHEN封面-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{counter}{ext}"
        counter += 1
    path.write_bytes(data)
    return {"ok": True, "path": str(path), "content_type": content_type, "bytes": len(data)}


def safe_download_filename(text: str, fallback: str = "video") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\n\r\t]+', "_", str(text or "")).strip(" ._")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned or fallback)[:80].strip(" ._") or fallback


def media_extension_from_url(url: str, content_type: str = "") -> str:
    parsed = urllib.parse.urlparse(url)
    path_ext = Path(parsed.path).suffix.lower()
    if path_ext in {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".m3u8", ".mp3", ".m4a", ".aac", ".wav"}:
        return path_ext
    query = urllib.parse.parse_qs(parsed.query)
    mime_type = str((query.get("mime_type") or [""])[0]).lower()
    if mime_type in {"video_mp4", "audio_mp4"}:
        return ".mp4"
    if mime_type == "video_webm":
        return ".webm"
    if mime_type in {"audio_m4a", "audio_mp4a"}:
        return ".m4a"
    guessed = mimetypes.guess_extension(content_type or "")
    if guessed in {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".mp3", ".m4a", ".aac", ".wav"}:
        return guessed
    return ".mp4"


def unique_download_path(directory: Path, stem: str, ext: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    ext = ext if ext.startswith(".") else f".{ext}"
    path = directory / f"{stem}{ext}"
    counter = 2
    while path.exists():
        path = directory / f"{stem}-{counter}{ext}"
        counter += 1
    return path


def download_media_url_to_file(
    url: str,
    cfg: Dict[str, Any],
    platform: str,
    target: Path,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    url = ensure_http_url(url)
    headers = dict(TEXT_HEADERS)
    cookie = platform_cookie(cfg, platform)
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    part_path = Path(str(target) + ".part")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            content_type = resp.headers.get_content_type() or "video/mp4"
            total_bytes = int(resp.headers.get("Content-Length") or 0)
            downloaded_bytes = 0
            with part_path.open("wb") as f:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded_bytes += len(chunk)
                    if progress_callback:
                        progress_callback({
                            "stage": "正在下载",
                            "downloaded_bytes": downloaded_bytes,
                            "total_bytes": total_bytes,
                            "progress": round(downloaded_bytes * 100 / total_bytes, 1) if total_bytes else 0.0,
                        })
            os.replace(part_path, target)
    except Exception:
        part_path.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        raise
    return {"content_type": content_type, "bytes": target.stat().st_size}


def meta_from_desktop_item(item: Dict[str, Any]) -> Dict[str, Any]:
    raw = item.get("raw_metadata_json") or "{}"
    try:
        meta = json.loads(raw) if isinstance(raw, str) else {}
    except Exception:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    meta.setdefault("platform", item.get("platform") or detect_platform(item.get("source_url") or ""))
    meta.setdefault("source_url", item.get("source_url") or "")
    meta.setdefault("title", item.get("title") or "")
    return meta


def desktop_save_video_file(
    db_path: Path,
    item_id: str,
    cfg: Dict[str, Any],
    downloads_dir: Optional[Path] = None,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    item = desktop_get_item(db_path, item_id)
    meta = meta_from_desktop_item(item)
    source_url = str(item.get("source_url") or meta.get("source_url") or "").strip()
    platform = str(meta.get("platform") or item.get("platform") or detect_platform(source_url) or "未知")
    if not source_url:
        raise ValueError("这条记录没有作品链接，无法下载视频。")

    media_url = media_url_or_empty(meta)
    if not media_url:
        refreshed = extract_from_page(source_url, cfg)
        meta = merge_meta(meta, refreshed)
        platform = str(meta.get("platform") or platform)
        media_url = media_url_or_empty(meta)

    target_dir = downloads_dir or (Path.home() / "Downloads" / "CHEN内容采集助手")
    title = safe_download_filename(meta.get("title") or item.get("title") or platform)
    stem = safe_download_filename(f"{platform}-{title}-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}")

    direct_error = ""
    tried_media_urls: set = set()
    for attempt in range(2):
        if not media_url or media_url in tried_media_urls:
            break
        tried_media_urls.add(media_url)
        ext = media_extension_from_url(media_url)
        target = unique_download_path(target_dir, stem, ext)
        try:
            if progress_callback is None:
                info = download_media_url_to_file(media_url, cfg, platform, target)
            else:
                info = download_media_url_to_file(media_url, cfg, platform, target, progress_callback=progress_callback)
            return {
                "ok": True,
                "path": str(target),
                "bytes": info.get("bytes") or target.stat().st_size,
                "content_type": info.get("content_type") or "video/mp4",
                "method": "media_url",
                "platform": platform,
            }
        except Exception as e:
            direct_error = str(e)
            target.unlink(missing_ok=True)
            if attempt == 0:
                refreshed = extract_from_page(source_url, cfg)
                meta = merge_meta(meta, refreshed)
                platform = str(meta.get("platform") or platform)
                fresh_media_url = media_url_or_empty(refreshed) or media_url_or_empty(meta)
                if fresh_media_url and fresh_media_url not in tried_media_urls:
                    media_url = fresh_media_url
                    continue
            break

    temp_path: Optional[Path] = None
    try:
        temp_path = download_media_with_ytdlp(source_url, cfg)
        ext = temp_path.suffix or ".mp4"
        target = unique_download_path(target_dir, stem, ext)
        shutil.copy2(temp_path, target)
        return {
            "ok": True,
            "path": str(target),
            "bytes": target.stat().st_size,
            "content_type": mimetypes.guess_type(str(target))[0] or "video/mp4",
            "method": "yt-dlp",
            "platform": platform,
        }
    except Exception as e:
        if direct_error:
            raise RuntimeError(f"视频直链下载失败，刷新后仍不可用：{direct_error}；yt-dlp 兜底失败：{e}") from e
        raise
    finally:
        if temp_path is not None:
            shutil.rmtree(temp_path.parent, ignore_errors=True)


def multipart_form_data(fields: Dict[str, str], file_field: str, filename: str, content_type: str, data: bytes) -> Tuple[bytes, str]:
    boundary = "----codex-" + uuid.uuid4().hex
    chunks: List[bytes] = []
    for key, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
            str(value).encode(),
            b"\r\n",
        ])
    chunks.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        data,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    return b"".join(chunks), boundary


def attachment_parent_node(cfg: Dict[str, Any]) -> str:
    feishu = require_feishu(cfg)
    app_token = feishu["app_token"]
    token = tenant_access_token(cfg)
    endpoint = "/open-apis/wiki/v2/spaces/get_node?" + urllib.parse.urlencode({"token": app_token})
    status, payload = http_json("GET", base_url(feishu) + endpoint, token=token)
    if status == 200 and isinstance(payload, dict) and payload.get("code") == 0:
        node = ((payload.get("data") or {}).get("node") or {})
        if node.get("obj_type") == "bitable" and node.get("obj_token"):
            return str(node["obj_token"])
    return app_token


def upload_cover_to_feishu(cfg: Dict[str, Any], cover_url: str, platform: str) -> Optional[str]:
    """Best-effort upload for attachment/image fields. If Feishu rejects it, caller falls back to URL."""
    if not cover_url:
        return None
    feishu = require_feishu(cfg)
    try:
        data, content_type, ext = fetch_binary(cover_url, cfg, platform)
        body, boundary = multipart_form_data(
            {
                "file_name": "cover" + ext,
                "parent_type": "bitable_image",
                "parent_node": attachment_parent_node(cfg),
                "size": str(len(data)),
            },
            "file",
            "cover" + ext,
            content_type,
            data,
        )
        token = tenant_access_token(cfg)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        req = urllib.request.Request(
            base_url(feishu) + "/open-apis/drive/v1/medias/upload_all",
            data=body,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=40) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if payload.get("code") == 0:
            data_obj = payload.get("data") or {}
            file_obj = data_obj.get("file") or {}
            return data_obj.get("file_token") or file_obj.get("file_token")
    except Exception:
        return None
    return None


def keep_existing_fields(fields: Dict[str, Any], field_types: Dict[str, int]) -> Dict[str, Any]:
    return {k: v for k, v in fields.items() if k in field_types}


def build_update_fields(cfg: Dict[str, Any], meta: Dict[str, Any], field_types: Dict[str, int]) -> Dict[str, Any]:
    names = cfg["fields"]
    out: Dict[str, Any] = {
        names["status"]: "成功",
        names["fetched_at"]: now_text(),
        names["error"]: "",
    }
    for key, field_name in (
        ("platform", names["platform"]),
        ("title", names["title"]),
        ("caption", names["caption"]),
        ("duration", names["duration"]),
        ("published_at", names["published_at"]),
    ):
        value = meta.get(key)
        if value not in (None, ""):
            out[field_name] = value

    for key, field_name in (("likes", names["likes"]), ("comments", names["comments"]), ("shares", names["shares"])):
        if meta.get(key) is not None:
            out[field_name] = meta[key]

    cover_url = meta.get("cover_url") or ""
    if cover_url:
        out[names["cover_url"]] = cover_url
        cover_name = names["cover"]
        cover_type = field_types.get(cover_name)
        if cover_type in (17, 18):
            token = upload_cover_to_feishu(cfg, cover_url, meta.get("platform") or "")
            if token:
                out[cover_name] = [{"file_token": token}]
        else:
            out[cover_name] = cover_url
    return keep_existing_fields({k: v for k, v in out.items() if k and v is not None}, field_types)


def clear_untrusted_media_fields(cfg: Dict[str, Any], field_types: Dict[str, int]) -> Dict[str, Any]:
    names = cfg["fields"]
    fields: Dict[str, Any] = {}
    for key in ("caption", "cover_url", "duration", "published_at"):
        field_name = names.get(key)
        if field_name in field_types:
            fields[field_name] = ""
    cover_name = names.get("cover")
    if cover_name in field_types:
        fields[cover_name] = [] if field_types.get(cover_name) in (17, 18) else ""
    return fields


def metadata_quality_message(meta: Dict[str, Any]) -> str:
    platform = meta.get("platform")
    if platform not in {"小红书", "B站", "YouTube", "Instagram"}:
        return ""
    missing: List[str] = []
    checks = [
        ("title", "作品标题"),
        ("cover_url", "封面"),
        ("duration", "时长"),
        ("likes", "点赞"),
        ("comments", "评论"),
    ]
    if platform not in {"YouTube", "Instagram"}:
        checks.append(("shares", "分享"))
    checks.append(("published_at", "发布时间"))
    for key, label in checks:
        if key == "title":
            missing_title = not usable_browser_title(str(meta.get("title") or ""))
            if missing_title:
                missing.append(label)
            continue
        if meta.get(key) in (None, ""):
            missing.append(label)
    if meta.get("content_type") == "video" and not media_url_or_empty(meta):
        missing.append("视频直链")
    if not missing:
        return ""
    return f"{platform}页面未暴露或未解析到：" + "、".join(missing) + "。"


def only_title_requires_login(meta: Dict[str, Any]) -> bool:
    if meta.get("platform") not in {"抖音", "小红书"}:
        return False
    if not meta.get("title") or meta.get("caption"):
        return False
    has_counts = any(meta.get(key) not in (None, "") for key in ("likes", "comments", "shares"))
    return bool(not meta.get("cover_url") and not media_url_or_empty(meta) and not has_counts)


def status_fields(cfg: Dict[str, Any], status: str, error: str, field_types: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    names = cfg["fields"]
    fields = {
        names["status"]: status,
        names["fetched_at"]: now_text(),
        names["error"]: error[:1000],
    }
    return keep_existing_fields(fields, field_types) if field_types is not None else fields


def classify_processing_error(error: Exception) -> str:
    text = str(error)
    lowered = text.lower()
    if BROWSER_NOT_READY_STATUS in text or "没有就绪" in text or "连接未就绪" in text or "connect_over_cdp" in lowered:
        return BROWSER_NOT_READY_STATUS
    if BROWSER_CONNECTION_STATUS in text or "target page, context or browser has been closed" in lowered:
        return BROWSER_CONNECTION_STATUS
    if (
        "等待登录" in text
    ):
        return WAITING_LOGIN_STATUS
    if "没有找到 yt-dlp" in text or "yt-dlp not found" in lowered or "yt-dlp缺失" in text:
        return "yt-dlp缺失"
    if "字幕缺失" in text or "没有可用字幕" in text or "no subtitles" in lowered or "no captions" in lowered:
        return "字幕缺失"
    if (
        "eof occurred in violation of protocol" in lowered
        or "_ssl.c" in lowered
        or "urlopen error" in lowered
        or "timed out" in lowered
        or "connection reset" in lowered
        or "remote end closed connection" in lowered
    ):
        return "网络异常"
    if (
        "刷新登录态" in text
        or "登录态" in text
        or "重新登录" in text
        or "要求登录" in text
        or "登录验证" in text
        or "login required" in lowered
        or "sign in to confirm" in lowered
    ):
        return "需登录"
    if "cookie" in lowered or "cookies" in lowered or "需要登录 cookie" in lowered or "fresh cookies" in lowered:
        return "需Cookie"
    if "图文" in text or "图片作品" in text or "image note" in lowered:
        return "图文作品"
    if "无音频" in text or "没有音频" in text or "音频流为空" in text or "no audio" in lowered:
        return "无音频"
    if "未拿到视频/音频直链" in text or "页面没有暴露标题" in text or "风控" in text or "captcha" in lowered:
        return "平台限制"
    if "下载" in text or "download" in lowered or "ffmpeg" in lowered or "抽取音频失败" in text:
        return "下载失败"
    if "asr" in lowered or "whisper" in lowered or "转写" in text or "transcrib" in lowered or "openai" in lowered:
        return "ASR失败"
    return "待人工确认"


def challenge_response(payload: Dict[str, Any]) -> Optional[Dict[str, str]]:
    challenge = payload.get("challenge")
    if isinstance(challenge, str) and challenge:
        return {"challenge": challenge}
    return None


def extract_record_ids(payload: Any) -> List[str]:
    found: List[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and re.fullmatch(r"rec[A-Za-z0-9_-]{8,}", value) and value not in found:
            found.append(value)

    def walk(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                if child_key in {"record_id", "recordId"}:
                    add(child_value)
                walk(child_value, child_key)
            return
        if isinstance(value, list):
            for item in value:
                walk(item, key)
            return
        if key in {"record_id", "recordId"}:
            add(value)

    walk(payload)
    return found


def extract_bitable_action_record_ids(event: Any) -> List[str]:
    found: List[str] = []
    event_data = getattr(event, "event", None)
    action_list = getattr(event_data, "action_list", None) or []
    for action in action_list:
        record_id = getattr(action, "record_id", None)
        if isinstance(record_id, str) and record_id and record_id not in found:
            found.append(record_id)
    return found


def extract_bitable_action_jobs(event: Any) -> List[Tuple[str, str]]:
    jobs: List[Tuple[str, str]] = []
    event_data = getattr(event, "event", None)
    default_table_id = str(getattr(event_data, "table_id", "") or "").strip()
    action_list = getattr(event_data, "action_list", None) or []
    for action in action_list:
        record_id = str(getattr(action, "record_id", "") or "").strip()
        table_id = str(getattr(action, "table_id", "") or default_table_id).strip()
        job = (table_id, record_id)
        if record_id and job not in jobs:
            jobs.append(job)
    return jobs


def waiting_login_retry_due(fields: Dict[str, Any], cfg: Dict[str, Any], now: Optional[dt.datetime] = None) -> bool:
    names = cfg["fields"]
    fetched_at = parse_local_time(as_text(fields.get(names["fetched_at"])))
    if fetched_at is None:
        return True
    current = now or dt.datetime.now()
    interval = int(login_gate_config(cfg).get("retry_interval") or 180)
    return (current - fetched_at).total_seconds() >= interval


def should_process_blank_record(record: Dict[str, Any], cfg: Dict[str, Any], now: Optional[dt.datetime] = None) -> bool:
    fields = record.get("fields") or {}
    names = cfg["fields"]
    url = normalize_url(as_text(fields.get(names["url"])))
    if not url:
        return False
    title = as_text(fields.get(names["title"]))
    caption = as_text(fields.get(names["caption"]))
    status = as_text(fields.get(names["status"]))
    error = as_text(fields.get(names["error"]))
    if "launch_persistent_context" in error or "remote-debugging-pipe" in error:
        return True
    if title and not usable_browser_title(title):
        return True
    if not title and not caption and not status:
        return True
    if not title and not caption and status in LOGIN_STATUSES:
        return True
    if not title and not caption and status in RETRY_LOGIN_STATUSES:
        return waiting_login_retry_due(fields, cfg, now)
    if not title and not caption and status in BROWSER_RETRY_STATUSES:
        return waiting_login_retry_due(fields, cfg, now)
    return bool(title and not caption and status in RETRY_TRANSCRIPT_STATUSES)


def should_transcribe_record(record: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    fields = record.get("fields") or {}
    names = cfg["fields"]
    url = normalize_url(as_text(fields.get(names["url"])))
    title = as_text(fields.get(names["title"]))
    caption = as_text(fields.get(names["caption"]))
    status = as_text(fields.get(names["status"]))
    if not url or caption:
        return False
    if status in RETRY_TRANSCRIPT_STATUSES:
        return True
    return bool(not status and not title)


def process_record(record: Dict[str, Any], cfg: Dict[str, Any], field_types: Dict[str, int], transcribe: bool = True) -> str:
    names = cfg["fields"]
    record_id = record.get("record_id") or record.get("id") or ""
    fields = record.get("fields") or {}
    url = normalize_url(as_text(fields.get(names["url"])))
    if not record_id:
        raise RuntimeError("飞书事件没有 record_id。")
    if not url:
        update_record(cfg, record_id, status_fields(cfg, "跳过", "作品链接为空。", field_types))
        return "skipped"

    meta = extract_from_page(url, cfg)
    if only_title_requires_login(meta):
        login_meta = {
            "platform": meta.get("platform"),
            "title": meta.get("title"),
        }
        update_fields = build_update_fields(cfg, login_meta, field_types)
        update_fields.update(clear_untrusted_media_fields(cfg, field_types))
        update_fields.update(
            status_fields(
                cfg,
                WAITING_LOGIN_STATUS,
                "已从分享文本识别标题，但未抓到封面、视频直链或互动数据；请在专用浏览器登录平台后系统会自动重试。",
                field_types,
            )
        )
        if update_fields:
            update_record(cfg, record_id, update_fields)
        return "metadata_incomplete"
    update_fields = build_update_fields(cfg, meta, field_types)
    needs_asr = bool(not meta.get("caption") and not str(meta.get("duration") or "").endswith("图"))
    quality_message = metadata_quality_message(meta)
    existing_caption = as_text(fields.get(names["caption"]))
    if not meta.get("title"):
        update_fields.update(status_fields(cfg, "平台限制", "已访问链接，但页面没有暴露标题；可能需要登录 Cookie、刷新登录态或官方接口。", field_types))
    elif not meta.get("caption"):
        if existing_caption:
            update_fields.update(status_fields(cfg, "信息不完整" if quality_message else "成功", quality_message, field_types))
        elif str(meta.get("duration") or "").endswith("图"):
            message = "已抓到作品信息；这是图文作品，没有视频逐字稿。"
            if quality_message:
                message += quality_message
            update_fields.update(status_fields(cfg, "图文作品", message, field_types))
        else:
            status = "转写中" if transcribe else "待转写"
            message = "已抓到作品信息和封面；页面没有自带字幕，等待视频音频 ASR 转写。"
            if quality_message:
                message += quality_message
            update_fields.update(status_fields(cfg, status, message, field_types))
    elif quality_message:
        update_fields.update(status_fields(cfg, "信息不完整", quality_message, field_types))
    if update_fields:
        update_record(cfg, record_id, update_fields)

    if transcribe and not existing_caption and needs_asr:
        try:
            text = transcribe_from_meta(cfg, meta)
        except Exception as e:
            update_record(cfg, record_id, status_fields(cfg, classify_processing_error(e), str(e), field_types))
            return "failed"
        update_record(cfg, record_id, keep_existing_fields({
            names["caption"]: text,
            names["status"]: "信息不完整" if quality_message else "成功",
            names["fetched_at"]: now_text(),
            names["error"]: quality_message,
        }, field_types))
        return "transcribed"
    if needs_asr and not transcribe:
        return "metadata_synced"
    return "synced"


def cmd_auth_test(_: argparse.Namespace) -> None:
    cfg = load_config()
    token = tenant_access_token(cfg, force=True)
    print(f"飞书连接成功：tenant_access_token={token[:8]}...（已缓存）")


def cmd_init_fields(_: argparse.Namespace) -> None:
    cfg = load_config()
    existing = {x.get("field_name") for x in list_fields(cfg)}
    created, skipped, failed = [], [], []
    for name, typ, options in FIELD_SPECS:
        if name in existing:
            skipped.append(name)
            continue
        try:
            feishu_api(cfg, "POST", fields_endpoint(cfg), field_payload(name, typ, options))
            created.append(name)
        except SystemExit as e:
            failed.append((name, str(e)))
    print(f"字段初始化完成：新建 {len(created)} 个，已存在 {len(skipped)} 个，失败 {len(failed)} 个")
    if created:
        print("新建字段：" + "、".join(created))
    if failed:
        for name, err in failed:
            print(f"- {name}: {err[:300]}")


def cmd_test_url(args: argparse.Namespace) -> None:
    cfg = load_config()
    try:
        meta = extract_from_page(normalize_url(args.url), cfg)
    except Exception as e:
        meta = {
            "source_url": normalize_url(args.url),
            "platform": detect_platform(normalize_url(args.url)),
            "title": "",
            "caption": "",
            "error": str(e),
        }
    print(json.dumps(meta, ensure_ascii=False, indent=2))


def should_process(record: Dict[str, Any], cfg: Dict[str, Any], all_rows: bool) -> Tuple[bool, str]:
    fields = record.get("fields") or {}
    names = cfg["fields"]
    url = normalize_url(as_text(fields.get(names["url"])))
    if not url:
        return False, ""
    if all_rows:
        return True, url
    title = as_text(fields.get(names["title"]))
    status = as_text(fields.get(names["status"]))
    if title and status == "成功":
        return False, url
    if status in HOLD_STATUSES:
        return False, url
    error = as_text(fields.get(names["error"]))
    if "Cookie" in error and not has_ytdlp_cookie(cfg):
        return False, url
    return True, url


def cmd_sync(args: argparse.Namespace) -> None:
    cfg = load_config()
    names = cfg["fields"]
    field_types = {x.get("field_name"): x.get("type") for x in list_fields(cfg)}
    records = list_records(cfg)
    done = skipped = failed = attempted = 0
    for record in records:
        if args.limit and attempted >= args.limit:
            break
        ok, url = should_process(record, cfg, args.all)
        if not ok:
            skipped += 1
            continue
        record_id = record.get("record_id")
        if not record_id:
            continue
        print(f"抓取：{url}", flush=True)
        try:
            meta = extract_from_page(url, cfg)
            update_fields = build_update_fields(cfg, meta, field_types)
            if not meta.get("title"):
                update_fields.update(status_fields(cfg, "平台限制", "已访问链接，但页面没有暴露标题；可能需要登录 Cookie、刷新登录态或官方接口。", field_types))
            elif not meta.get("caption"):
                if str(meta.get("duration") or "").endswith("图"):
                    update_fields.update(status_fields(cfg, "图文作品", "已抓到作品信息；这是图文作品，没有视频逐字稿。", field_types))
                else:
                    update_fields.update(status_fields(cfg, "待转写", "已抓到作品信息和封面；页面没有自带字幕，等待视频音频 ASR 转写。", field_types))
            if update_fields:
                update_record(cfg, record_id, update_fields)
            done += 1
        except Exception as e:
            failed += 1
            try:
                fields = status_fields(cfg, classify_processing_error(e), str(e), field_types)
                if fields:
                    update_record(cfg, record_id, fields)
            except Exception:
                pass
            print(f"失败：{e}", flush=True)
    print(f"完成：处理 {done} 条，跳过 {skipped} 条，失败 {failed} 条", flush=True)
    if names.get("cover") == "封面":
        print("提示：如果你的「封面」字段是附件字段但没有显示图片，请先看「封面图链接」字段。")


def cmd_clean_fake_transcripts(args: argparse.Namespace) -> None:
    cfg = load_config()
    names = cfg["fields"]
    field_types = {x.get("field_name"): x.get("type") for x in list_fields(cfg)}
    if names["caption"] not in field_types:
        raise SystemExit(f"表里没有字段：{names['caption']}")
    records = list_records(cfg)
    cleaned = skipped = 0
    for record in records:
        fields = record.get("fields") or {}
        title = as_text(fields.get(names["title"]))
        caption = as_text(fields.get(names["caption"]))
        if title and caption and title == caption:
            update = keep_existing_fields({
                names["caption"]: "",
                names["status"]: "待转写",
                names["fetched_at"]: now_text(),
                names["error"]: "已清空错误文案：原内容只是作品标题/描述，不是视频逐字稿。",
            }, field_types)
            update_record(cfg, record["record_id"], update)
            cleaned += 1
            print(f"清空：{title[:60]}", flush=True)
        else:
            skipped += 1
    print(f"完成：清空 {cleaned} 条，跳过 {skipped} 条", flush=True)


def cmd_transcribe_url(args: argparse.Namespace) -> None:
    cfg = load_config()
    meta = extract_from_page(normalize_url(args.url), cfg)
    text = transcribe_from_meta(cfg, meta)
    print(text)


def cmd_transcribe_missing(args: argparse.Namespace) -> None:
    cfg = load_config()
    names = cfg["fields"]
    field_types = {x.get("field_name"): x.get("type") for x in list_fields(cfg)}
    records = list_records(cfg)
    done = skipped = failed = attempted = 0
    for record in records:
        if args.limit and attempted >= args.limit:
            break
        fields = record.get("fields") or {}
        url = normalize_url(as_text(fields.get(names["url"])))
        title = as_text(fields.get(names["title"]))
        caption = as_text(fields.get(names["caption"]))
        error = as_text(fields.get(names["error"]))
        if not url or (caption and not args.all):
            skipped += 1
            continue
        if not args.all and not title and "Cookie" in error:
            skipped += 1
            continue
        attempted += 1
        print(f"转写：{url}", flush=True)
        try:
            meta = extract_from_page(url, cfg)
            text = transcribe_from_meta(cfg, meta)
            update = keep_existing_fields({
                names["caption"]: text,
                names["status"]: "成功",
                names["fetched_at"]: now_text(),
                names["error"]: "",
            }, field_types)
            update_record(cfg, record["record_id"], update)
            done += 1
        except Exception as e:
            failed += 1
            update = keep_existing_fields({
                names["status"]: "部分成功",
                names["fetched_at"]: now_text(),
                names["error"]: f"逐字稿转写未完成：{e}",
            }, field_types)
            try:
                update_record(cfg, record["record_id"], update)
            except Exception:
                pass
            print(f"失败：{e}", flush=True)
    print(f"完成：转写 {done} 条，跳过 {skipped} 条，失败 {failed} 条", flush=True)


def cmd_format_transcripts(args: argparse.Namespace) -> None:
    cfg = load_config()
    names = cfg["fields"]
    field_types = {x.get("field_name"): x.get("type") for x in list_fields(cfg)}
    records = list_records(cfg)
    done = skipped = 0
    for record in records:
        if args.limit and done >= args.limit:
            break
        fields = record.get("fields") or {}
        caption = as_text(fields.get(names["caption"]))
        if not caption:
            skipped += 1
            continue
        formatted = format_transcript_text(caption)
        if formatted == caption:
            skipped += 1
            continue
        update_record(cfg, record["record_id"], keep_existing_fields({
            names["caption"]: formatted,
            names["fetched_at"]: now_text(),
        }, field_types))
        done += 1
    print(f"完成：格式化 {done} 条，跳过 {skipped} 条", flush=True)


def webhook_worker(
    cfg: Dict[str, Any],
    jobs: "queue.Queue[Tuple[str, str]]",
    stop_event: threading.Event,
    pending: Optional[set[str]] = None,
    pending_lock: Optional[threading.Lock] = None,
    log_prefix: str = "Webhook",
    login_open_state: Optional[Dict[str, float]] = None,
    login_open_lock: Optional[threading.Lock] = None,
    retry_attempts: Optional[Dict[str, int]] = None,
    retry_lock: Optional[threading.Lock] = None,
) -> None:
    while not stop_event.is_set():
        try:
            table_id, record_id = jobs.get(timeout=0.5)
        except queue.Empty:
            continue
        job_key = f"{table_id}:{record_id}"
        table_cfg = with_table_id(cfg, table_id) if table_id else cfg
        try:
            print(f"{log_prefix}处理：table_id={table_id or '-'} record_id={record_id}", flush=True)
            field_types = {x.get("field_name"): x.get("type") for x in list_fields(table_cfg)}
            record = get_record(table_cfg, record_id)
            if not record:
                raise RuntimeError(f"未找到记录：{record_id}")
            if not should_process_blank_record(record, table_cfg):
                print(f"{log_prefix}跳过：table_id={table_id or '-'} record_id={record_id} -> 当前状态不需要处理", flush=True)
                continue
            result = process_record(record, table_cfg, field_types, transcribe=should_transcribe_record(record, table_cfg))
            if retry_attempts is not None and retry_lock is not None:
                with retry_lock:
                    retry_attempts.pop(job_key, None)
            print(f"{log_prefix}完成：table_id={table_id or '-'} record_id={record_id} -> {result}", flush=True)
        except Exception as e:
            print(f"{log_prefix}失败：table_id={table_id or '-'} record_id={record_id} -> {e}", flush=True)
            try:
                field_types = {x.get("field_name"): x.get("type") for x in list_fields(table_cfg)}
                status = classify_processing_error(e)
                error = str(e)
                if should_trigger_login_gate(status):
                    try:
                        record = get_record(table_cfg, record_id)
                    except Exception:
                        record = {}
                    url = normalize_url(as_text((record.get("fields") or {}).get((table_cfg.get("fields") or DEFAULT_FIELDS)["url"])))
                    platform = detect_platform(url)
                    message = f"{error} 已打开{platform}登录页；请完成登录并播放/打开目标内容，系统会自动重试。"
                    update_record(table_cfg, record_id, status_fields(table_cfg, WAITING_LOGIN_STATUS, message, field_types))
                    gate = login_gate_config(table_cfg)
                    if login_open_state is not None and login_open_lock is not None:
                        open_login_page_once(platform, login_open_state, login_open_lock, int(gate.get("open_cooldown") or 300))
                    if pending is not None and pending_lock is not None and retry_attempts is not None and retry_lock is not None:
                        schedule_login_retry(table_cfg, jobs, pending, pending_lock, retry_attempts, retry_lock, table_id, record_id)
                else:
                    update_record(table_cfg, record_id, status_fields(table_cfg, status, error, field_types))
            except Exception:
                pass
        finally:
            if pending is not None and pending_lock is not None:
                with pending_lock:
                    pending.discard(job_key)
            jobs.task_done()


def queue_record_ids(
    jobs: "queue.Queue[Tuple[str, str]]",
    pending: set[str],
    pending_lock: threading.Lock,
    record_ids: Iterable[str],
    source: str,
    table_id: str = "",
) -> List[str]:
    queued: List[str] = []
    with pending_lock:
        for record_id in record_ids:
            job_key = f"{table_id}:{record_id}"
            if not record_id or job_key in pending:
                continue
            pending.add(job_key)
            jobs.put((table_id, record_id))
            queued.append(record_id)
    if queued:
        print(f"{source}入队：table_id={table_id or '-'} record_ids={queued}", flush=True)
    return queued


def schedule_login_retry(
    cfg: Dict[str, Any],
    jobs: "queue.Queue[Tuple[str, str]]",
    pending: set[str],
    pending_lock: threading.Lock,
    retry_attempts: Dict[str, int],
    retry_lock: threading.Lock,
    table_id: str,
    record_id: str,
) -> bool:
    gate = login_gate_config(cfg)
    if not gate.get("enabled", True):
        return False
    job_key = f"{table_id}:{record_id}"
    with retry_lock:
        attempt = retry_attempts.get(job_key, 0) + 1
        retry_attempts[job_key] = attempt
    max_attempts = max(1, int(gate.get("max_retry_attempts") or 10))
    if attempt > max_attempts:
        print(f"登录守门员：重试次数已达上限 table_id={table_id or '-'} record_id={record_id}", flush=True)
        return False
    delay = max(180, int(gate.get("retry_interval") or 180))

    def retry() -> None:
        queued = queue_record_ids(jobs, pending, pending_lock, [record_id], f"登录守门员重试#{attempt}", table_id)
        if not queued:
            print(f"登录守门员：重试跳过，任务仍在队列中 table_id={table_id or '-'} record_id={record_id}", flush=True)

    timer = threading.Timer(delay, retry)
    timer.daemon = True
    timer.start()
    print(f"登录守门员：{delay}秒后重试 table_id={table_id or '-'} record_id={record_id} attempt={attempt}/{max_attempts}", flush=True)
    return True


def scan_missing_records_once(
    cfg: Dict[str, Any],
    jobs: "queue.Queue[Tuple[str, str]]",
    pending: set[str],
    pending_lock: threading.Lock,
) -> None:
    for table_id in discover_feishu_table_ids(cfg):
        try:
            table_cfg = with_table_id(cfg, table_id)
            records = list_records(table_cfg)
            record_ids = [
                record.get("record_id") or ""
                for record in records
                if should_process_blank_record(record, table_cfg)
            ]
            queue_record_ids(jobs, pending, pending_lock, record_ids, "补扫", table_id)
        except Exception as e:
            print(f"补扫失败：table_id={table_id} -> {e}", flush=True)


def mark_scanner_heartbeat(state: Dict[str, Any], status: str) -> None:
    state["last_seen"] = time.monotonic()
    state["status"] = status


def scanner_heartbeat_stale(state: Dict[str, Any], interval: int, now: Optional[float] = None) -> bool:
    last_seen = float(state.get("last_seen") or 0)
    if not last_seen:
        return False
    threshold = max(60, max(5, interval) * 3)
    return (now if now is not None else time.monotonic()) - last_seen > threshold


def missed_record_scanner(
    cfg: Dict[str, Any],
    jobs: "queue.Queue[Tuple[str, str]]",
    stop_event: threading.Event,
    pending: set[str],
    pending_lock: threading.Lock,
    interval: int,
    heartbeat: Optional[Dict[str, Any]] = None,
    heartbeat_lock: Optional[threading.Lock] = None,
) -> None:
    while not stop_event.wait(max(5, interval)):
        try:
            if heartbeat is not None and heartbeat_lock is not None:
                with heartbeat_lock:
                    mark_scanner_heartbeat(heartbeat, "running")
            scan_missing_records_once(cfg, jobs, pending, pending_lock)
            if heartbeat is not None and heartbeat_lock is not None:
                with heartbeat_lock:
                    mark_scanner_heartbeat(heartbeat, "finished")
        except Exception as e:
            if heartbeat is not None and heartbeat_lock is not None:
                with heartbeat_lock:
                    mark_scanner_heartbeat(heartbeat, "failed")
            print(f"补扫失败：自动发现数据表 -> {e}", flush=True)


def scanner_watchdog(
    stop_event: threading.Event,
    heartbeat: Dict[str, Any],
    heartbeat_lock: threading.Lock,
    interval: int,
) -> None:
    while not stop_event.wait(max(30, max(5, interval))):
        with heartbeat_lock:
            stale = scanner_heartbeat_stale(heartbeat, interval)
            snapshot = dict(heartbeat)
        if stale:
            print(f"补扫心跳超时，准备重启监听进程：{snapshot}", flush=True)
            os._exit(75)


def make_webhook_handler(cfg: Dict[str, Any], jobs: "queue.Queue[Tuple[str, str]]"):
    pending: set[str] = set()
    pending_lock = threading.Lock()
    webhook_cfg = cfg.get("webhook") or {}
    verification_token = webhook_cfg.get("verification_token") or ""

    class FeishuWebhookHandler(http.server.BaseHTTPRequestHandler):
        server_version = "ChenFeishuWebhook/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"Webhook请求：{self.address_string()} - {fmt % args}", flush=True)

        def write_json(self, status: int, payload: Dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if urllib.parse.urlparse(self.path).path in {"/", "/health"}:
                self.write_json(200, {"ok": True, "service": "feishu-webhook"})
                return
            self.write_json(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:
            path = urllib.parse.urlparse(self.path).path
            if path not in {"/", "/feishu/webhook"}:
                self.write_json(404, {"ok": False, "error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8") if raw else "{}")
            except json.JSONDecodeError:
                self.write_json(400, {"ok": False, "error": "invalid json"})
                return

            challenge = challenge_response(payload)
            if challenge:
                print("Webhook验证：已返回 challenge", flush=True)
                self.write_json(200, challenge)
                return

            if verification_token:
                got = payload.get("token") or (payload.get("header") or {}).get("token") or self.headers.get("X-Lark-Request-Token") or ""
                if got != verification_token:
                    self.write_json(403, {"ok": False, "error": "bad token"})
                    return

            record_ids = extract_record_ids(payload)
            print(f"Webhook事件：record_ids={record_ids}", flush=True)
            queued: List[str] = []
            with pending_lock:
                for record_id in record_ids:
                    job_key = f":{record_id}"
                    if job_key in pending:
                        continue
                    pending.add(job_key)
                    jobs.put(("", record_id))
                    queued.append(record_id)

            def clear_pending() -> None:
                jobs.join()
                with pending_lock:
                    for record_id in queued:
                        pending.discard(f":{record_id}")

            if queued:
                threading.Thread(target=clear_pending, daemon=True).start()
            self.write_json(200, {"ok": True, "queued": queued})

    return FeishuWebhookHandler


def cmd_webhook_server(args: argparse.Namespace) -> None:
    cfg = load_config()
    jobs: "queue.Queue[Tuple[str, str]]" = queue.Queue()
    stop_event = threading.Event()
    worker = threading.Thread(target=webhook_worker, args=(cfg, jobs, stop_event), daemon=True)
    worker.start()
    server = http.server.ThreadingHTTPServer((args.host, args.port), make_webhook_handler(cfg, jobs))
    print(f"飞书 Webhook 服务已启动：http://{args.host}:{args.port}/feishu/webhook", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("正在停止飞书 Webhook 服务。", flush=True)
    finally:
        stop_event.set()
        server.shutdown()
        server.server_close()


def cmd_event_listener(args: argparse.Namespace) -> None:
    cfg = load_config()
    feishu = require_feishu_credentials(cfg)
    jobs: "queue.Queue[Tuple[str, str]]" = queue.Queue()
    stop_event = threading.Event()
    pending: set[str] = set()
    pending_lock = threading.Lock()
    login_open_state: Dict[str, float] = {}
    login_open_lock = threading.Lock()
    retry_attempts: Dict[str, int] = {}
    retry_lock = threading.Lock()
    worker_count = event_worker_count(cfg)
    for index in range(worker_count):
        worker = threading.Thread(
            target=webhook_worker,
            args=(
                cfg,
                jobs,
                stop_event,
                pending,
                pending_lock,
                f"长连接-{index + 1}",
                login_open_state,
                login_open_lock,
                retry_attempts,
                retry_lock,
            ),
            daemon=True,
        )
        worker.start()

    try:
        import lark_oapi as lark
        from lark_oapi.ws import Client as LarkWsClient
    except ImportError as e:
        raise SystemExit("缺少飞书官方 SDK：请先运行 python3 -m pip install --user -U lark-oapi") from e

    event_cfg = cfg.get("event") or {}
    encrypt_key = event_cfg.get("encrypt_key") or ""
    verification_token = event_cfg.get("verification_token") or ""
    scan_interval = int(event_cfg.get("scan_interval") or 15)
    scanner_heartbeat_state: Dict[str, Any] = {}
    scanner_heartbeat_lock = threading.Lock()
    scanner = threading.Thread(
        target=missed_record_scanner,
        args=(cfg, jobs, stop_event, pending, pending_lock, scan_interval, scanner_heartbeat_state, scanner_heartbeat_lock),
        daemon=True,
    )
    scanner.start()
    watchdog = threading.Thread(
        target=scanner_watchdog,
        args=(stop_event, scanner_heartbeat_state, scanner_heartbeat_lock, scan_interval),
        daemon=True,
    )
    watchdog.start()

    def on_bitable_record_changed(event: Any) -> None:
        event_jobs = extract_bitable_action_jobs(event)
        if event_jobs:
            print(f"长连接事件：jobs={event_jobs}", flush=True)
            for table_id, record_id in event_jobs:
                queue_record_ids(jobs, pending, pending_lock, [record_id], "长连接事件", table_id)
            return
        record_ids = extract_bitable_action_record_ids(event)
        print(f"长连接事件：record_ids={record_ids}", flush=True)
        queue_record_ids(jobs, pending, pending_lock, record_ids, "长连接事件")

    event_handler = (
        lark.EventDispatcherHandler.builder(encrypt_key, verification_token)
        .register_p2_drive_file_bitable_record_changed_v1(on_bitable_record_changed)
        .build()
    )
    client = LarkWsClient(
        feishu["app_id"],
        feishu["app_secret"],
        event_handler=event_handler,
        domain=base_url(feishu),
    )
    print(f"飞书长连接监听已启动，worker={worker_count}。按 Ctrl+C 停止。", flush=True)
    try:
        client.start()
    except KeyboardInterrupt:
        print("正在停止飞书长连接监听。", flush=True)
    finally:
        stop_event.set()


def cmd_watch(args: argparse.Namespace) -> None:
    cfg = load_config()
    print(f"开始监听飞书表格：每 {args.interval} 秒扫描一次。按 Ctrl+C 停止。", flush=True)
    while True:
        try:
            cmd_sync(argparse.Namespace(limit=args.sync_limit, all=False))
            cmd_transcribe_missing(argparse.Namespace(limit=args.transcribe_limit, all=False))
        except KeyboardInterrupt:
            print("已停止监听。", flush=True)
            return
        except Exception as e:
            print(f"监听循环出错：{e}", flush=True)
        time.sleep(args.interval)


def print_health_result(result: Dict[str, Any], as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return
    print(f"健康检查：{'正常' if result.get('ok') else '需要处理'}  {result.get('checked_at')}", flush=True)
    listener = result.get("listener") or {}
    print(f"- 飞书长连接监听：{listener.get('status')} ({listener.get('label')})", flush=True)
    browser = result.get("browser") or {}
    if browser:
        text = browser.get("status")
        if browser.get("error"):
            text += "：" + str(browser.get("error"))[:160]
        print(f"- 专用浏览器：{text}", flush=True)
    tables = (result.get("tables") or {}).get("tables") or []
    for table in tables:
        if table.get("error"):
            print(f"- 表 {table.get('table_id')}：读取失败：{table.get('error')}", flush=True)
            continue
        counts = "，".join(f"{k}:{v}" for k, v in (table.get("status_counts") or {}).items()) or "无链接"
        print(f"- 表 {table.get('table_id')}：{counts}；空白待处理 {table.get('blank_link_rows', 0)}", flush=True)
        for problem in (table.get("problems") or [])[:5]:
            print(
                f"  第{problem.get('row')}行 {problem.get('status')} {problem.get('platform')}：{problem.get('error')}",
                flush=True,
            )
    actions = result.get("actions") or []
    if actions:
        print("- 修复动作：" + "，".join(actions), flush=True)


def cmd_health_check(args: argparse.Namespace) -> None:
    cfg = load_config()
    result = run_health_check(cfg, repair=args.repair)
    print_health_result(result, as_json=args.json)


def cmd_health_daemon(args: argparse.Namespace) -> None:
    cfg = load_config()
    health = health_config(cfg)
    interval = max(int(args.interval or 0) or int(health.get("interval") or 300), 300)
    print(f"健康守护已启动：每 {interval} 秒自检并修复。按 Ctrl+C 停止。", flush=True)
    while True:
        try:
            result = run_health_check(cfg, repair=True)
            print_health_result(result, as_json=False)
        except KeyboardInterrupt:
            print("健康守护已停止。", flush=True)
            return
        except Exception as e:
            print(f"健康守护异常：{e}", flush=True)
        time.sleep(interval)


def desktop_service_is_healthy(host: str, port: int) -> bool:
    url = f"http://{host}:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=1.5) as resp:
            if resp.status != 200:
                return False
            payload = json.loads(resp.read().decode("utf-8"))
            return bool(payload.get("ok"))
    except Exception:
        return False


def cmd_desktop_app(args: argparse.Namespace) -> None:
    cfg = load_config()
    db_path = Path(args.db) if args.db else DESKTOP_DB_PATH
    desktop_db_init(db_path)
    desktop_start_queue_worker(db_path, cfg)
    desktop_start_mobile_inbox_worker(db_path, cfg)
    publisher_data_root = DATA_ROOT / "max_daily_cloud"
    cloud_publish_jobs = PublishJobManager(
        publisher_data_root / "publisher" / "config.json",
        publisher_data_root / ".publisher-state" / "latest-report.json",
        share_url_reader=lambda: read_fixed_collaboration_url(
            publisher_data_root / "publisher" / "config.json"
        ),
    )
    url = f"http://{args.host}:{args.port}/"
    try:
        server = http.server.ThreadingHTTPServer(
            (args.host, args.port),
            make_desktop_app_handler(cfg, db_path, cloud_publish_jobs),
        )
    except OSError as e:
        if getattr(e, "errno", None) == 48:
            if desktop_service_is_healthy(args.host, args.port):
                print(f"橙子内容采集助手已在运行：{url}", flush=True)
                if args.open:
                    webbrowser.open(url)
                return
            raise SystemExit(
                f"端口 {args.port} 已被占用，但已有服务没有响应健康检查。"
                "请退出旧的 CHEN 内容采集助手后再打开。"
            )
        raise
    print(f"橙子内容采集助手已启动：{url}", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("橙子内容采集助手已停止。", flush=True)
    finally:
        server.server_close()


def cmd_export_max_daily(args: argparse.Namespace) -> None:
    db_path = Path(args.db) if args.db else DESKTOP_DB_PATH
    desktop_db_init(db_path)
    table_id = str(args.table_id or "").strip()
    if not table_id:
        tables = desktop_list_tables(db_path)
        if not tables:
            raise SystemExit("没有可导出的采集表。")
        table_id = str(tables[0].get("id") or "")
    output = Path(args.output).expanduser() if args.output else None
    result = desktop_save_export_file(db_path, table_id, "max-daily", output)
    print(f"MAX 日报已生成：{result['path']}", flush=True)


def cmd_make_config(_: argparse.Namespace) -> None:
    if CONFIG_PATH.exists():
        raise SystemExit(f"已存在 {CONFIG_PATH}，不覆盖。")
    example = {
        "feishu": {
            "app_id": "cli_在这里填AppID",
            "app_secret": "在这里填AppSecret",
            "app_token": "base后面的bascn或base token",
            "table_id": "tbl开头的数据表ID",
            "base_url": "https://open.feishu.cn",
        },
        "fields": DEFAULT_FIELDS,
        "platforms": {
            "抖音": {"cookie": ""},
            "小红书": {"cookie": ""},
            "视频号": {"cookie": ""},
        },
        "event": {
            "encrypt_key": "",
            "verification_token": "",
            "scan_interval": 15,
        },
    }
    CONFIG_PATH.write_text(json.dumps(example, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已创建 {CONFIG_PATH}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="飞书多维表格作品链接采集器")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("make-config", help="创建 config.json 样例")
    s.set_defaults(fn=cmd_make_config)

    s = sub.add_parser("auth-test", help="测试飞书凭证和表格权限")
    s.set_defaults(fn=cmd_auth_test)

    s = sub.add_parser("init-fields", help="在飞书表里补齐采集字段")
    s.set_defaults(fn=cmd_init_fields)

    s = sub.add_parser("test-url", help="只测试一个作品链接，不写飞书")
    s.add_argument("url")
    s.set_defaults(fn=cmd_test_url)

    s = sub.add_parser("sync", help="扫描飞书表格，抓取并写回")
    s.add_argument("--limit", type=int, default=0, help="最多处理几条，0 表示不限")
    s.add_argument("--all", action="store_true", help="重新处理所有有链接的记录")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("clean-fake-transcripts", help="清空与标题完全相同的伪逐字稿")
    s.set_defaults(fn=cmd_clean_fake_transcripts)

    s = sub.add_parser("transcribe-url", help="测试单个链接的音频转写，不写飞书")
    s.add_argument("url")
    s.set_defaults(fn=cmd_transcribe_url)

    s = sub.add_parser("transcribe-missing", help="转写飞书表格里文案为空的记录")
    s.add_argument("--limit", type=int, default=0, help="最多处理几条，0 表示不限")
    s.add_argument("--all", action="store_true", help="重转所有有链接的记录")
    s.set_defaults(fn=cmd_transcribe_missing)

    s = sub.add_parser("format-transcripts", help="给已有逐字稿做轻量标点和分段")
    s.add_argument("--limit", type=int, default=0, help="最多处理几条，0 表示不限")
    s.set_defaults(fn=cmd_format_transcripts)

    s = sub.add_parser("webhook-server", help="启动飞书事件 Webhook 服务")
    s.add_argument("--host", default="127.0.0.1", help="监听地址")
    s.add_argument("--port", type=int, default=8787, help="监听端口")
    s.set_defaults(fn=cmd_webhook_server)

    s = sub.add_parser("event-listener", help="启动飞书长连接事件监听")
    s.set_defaults(fn=cmd_event_listener)

    s = sub.add_parser("watch", help="持续监听飞书表格新链接并自动抓取/转写")
    s.add_argument("--interval", type=int, default=60, help="扫描间隔秒数")
    s.add_argument("--sync-limit", type=int, default=20, help="每轮最多抓取元数据条数")
    s.add_argument("--transcribe-limit", type=int, default=1, help="每轮最多转写条数")
    s.set_defaults(fn=cmd_watch)

    s = sub.add_parser("health-check", help="检查并可自动修复监听、浏览器和表格积压")
    s.add_argument("--repair", action="store_true", help="发现监听/浏览器问题时自动修复")
    s.add_argument("--json", action="store_true", help="以 JSON 输出检查结果")
    s.set_defaults(fn=cmd_health_check)

    s = sub.add_parser("health-daemon", help="常驻健康守护，定时自检并自动修复")
    s.add_argument("--interval", type=int, default=0, help="自检间隔秒数，最低 300 秒")
    s.set_defaults(fn=cmd_health_daemon)

    s = sub.add_parser("desktop-app", help="启动橙子内容采集助手本地软件界面")
    s.add_argument("--host", default="127.0.0.1", help="监听地址")
    s.add_argument("--port", type=int, default=51216, help="监听端口")
    s.add_argument("--db", default="", help="本地数据库路径，默认 desktop_collector.sqlite3")
    s.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    s.set_defaults(fn=cmd_desktop_app)

    s = sub.add_parser("export-max-daily", help="把本地采集表导出成可直接给 Max 阅读的 Markdown 日报")
    s.add_argument("--db", default="", help="本地数据库路径，默认 desktop_collector.sqlite3")
    s.add_argument("--table-id", default="", help="采集表 ID，默认使用最近更新的采集表")
    s.add_argument("--output", default="", help="输出路径；不填则弹出系统保存窗口")
    s.set_defaults(fn=cmd_export_max_daily)

    return p


def main() -> None:
    args = build_parser().parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
