#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, NamedTuple, Optional


DEFAULT_INSTALL_ROOT = Path.home() / "Library" / "Application Support" / "ChenContentLinkCollector"
DEFAULT_APP_PATH = Path.home() / "Desktop" / "CHEN 内容采集助手.app"
LAUNCH_AGENT_LABEL = "com.chen.content-link-collector.desktop-app"
LAUNCH_AGENT_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
BUNDLE_IDENTIFIER = "com.chen.content-collector.native"
HEALTH_URL = "http://127.0.0.1:51216/api/version"
PRESERVED_PUBLISHER_FILES = {"config.json", ".publish-state.json"}


class LegacyServiceState(NamedTuple):
    enabled: bool
    running: bool


def run_git(source_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=source_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assert_source_matches_master(source_root: Path) -> str:
    source_root = source_root.resolve()
    head = run_git(source_root, "rev-parse", "HEAD")
    master = run_git(source_root, "rev-parse", "refs/heads/master")
    if head != master:
        raise RuntimeError(f"拒绝部署：来源提交 {head[:12]} 不是当前 master {master[:12]}。")
    if run_git(source_root, "status", "--porcelain"):
        raise RuntimeError("拒绝部署：master 工作区存在未提交修改。")
    return head


@contextlib.contextmanager
def exclusive_deploy_lock(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("内容采集助手正在部署，请等待当前部署完成。") from error
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def run_verification(source_root: Path) -> None:
    env = dict(os.environ)
    env["PYTHONPYCACHEPREFIX"] = str(Path(tempfile.gettempdir()) / "chen-collector-deploy-pycache")
    subprocess.run([sys.executable, "-m", "unittest", "-v"], cwd=source_root, env=env, check=True)
    subprocess.run(
        [sys.executable, "-m", "unittest", "-v", "test_build_desktop_app.py"],
        cwd=source_root,
        env=env,
        check=True,
    )
    cloud_root = source_root / "max_daily_cloud"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "unittest",
            "-v",
            "publisher/test_media_proxy.py",
            "publisher/test_max_daily_publisher.py",
            "publisher/test_local_publish_jobs.py",
        ],
        cwd=cloud_root,
        env=env,
        check=True,
    )
    subprocess.run(["npm", "test"], cwd=cloud_root, env=env, check=True)
    subprocess.run(["npm", "run", "typecheck"], cwd=cloud_root, env=env, check=True)


def backup_installation(install_root: Path, commit: str, app_path: Optional[Path] = None) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = install_root / "backups" / f"{stamp}-{commit[:12]}"
    backup.mkdir(parents=True, exist_ok=False)
    for relative in (Path("content_link_collector.py"), Path("deployment_manifest.json")):
        source = install_root / relative
        if source.exists():
            shutil.copy2(source, backup / relative.name)
    publisher = install_root / "max_daily_cloud" / "publisher"
    if publisher.is_dir():
        shutil.copytree(publisher, backup / "publisher")
    if app_path is not None and app_path.is_dir():
        shutil.copytree(app_path, backup / "desktop_app")
    return backup


def build_native_app(source_root: Path, output_app: Path, commit: str) -> Path:
    builder_path = source_root / "scripts" / "build_desktop_app.py"
    spec = importlib.util.spec_from_file_location("chen_build_desktop_app", builder_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载原生 App 构建器。")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_app(source_root, output_app, commit)


def atomic_install(source_root: Path, install_root: Path) -> None:
    install_root.mkdir(parents=True, exist_ok=True)
    source_script = source_root / "content_link_collector.py"
    source_publisher = source_root / "max_daily_cloud" / "publisher"
    if not source_script.is_file():
        raise RuntimeError("部署来源缺少 content_link_collector.py。")
    if not source_publisher.is_dir():
        raise RuntimeError("部署来源缺少 max_daily_cloud/publisher。")

    script_temp = install_root / f".content_link_collector.{uuid.uuid4().hex}.tmp"
    shutil.copy2(source_script, script_temp)
    os.replace(script_temp, install_root / "content_link_collector.py")

    publisher_target = install_root / "max_daily_cloud" / "publisher"
    publisher_target.parent.mkdir(parents=True, exist_ok=True)
    publisher_temp = publisher_target.parent / f".publisher.{uuid.uuid4().hex}.tmp"
    shutil.copytree(source_publisher, publisher_temp)
    if publisher_target.is_dir():
        for name in PRESERVED_PUBLISHER_FILES:
            existing = publisher_target / name
            if existing.is_file():
                shutil.copy2(existing, publisher_temp / name)
    previous = publisher_target.parent / f".publisher.{uuid.uuid4().hex}.previous"
    try:
        if publisher_target.exists():
            os.replace(publisher_target, previous)
        os.replace(publisher_temp, publisher_target)
    finally:
        if publisher_temp.exists():
            shutil.rmtree(publisher_temp)
        if previous.exists():
            shutil.rmtree(previous)


def atomic_install_app(candidate: Path, app_path: Path) -> None:
    app_path.parent.mkdir(parents=True, exist_ok=True)
    previous = app_path.parent / f".{app_path.name}.{uuid.uuid4().hex}.previous"
    try:
        if app_path.exists():
            os.replace(app_path, previous)
        os.replace(candidate, app_path)
    except Exception:
        if not app_path.exists() and previous.exists():
            os.replace(previous, app_path)
        raise
    finally:
        if previous.exists():
            shutil.rmtree(previous)


def restore_backup(install_root: Path, backup: Path, app_path: Optional[Path] = None) -> None:
    script_backup = backup / "content_link_collector.py"
    if script_backup.is_file():
        shutil.copy2(script_backup, install_root / "content_link_collector.py")
    manifest_backup = backup / "deployment_manifest.json"
    manifest_target = install_root / "deployment_manifest.json"
    if manifest_backup.is_file():
        shutil.copy2(manifest_backup, manifest_target)
    elif manifest_target.exists():
        manifest_target.unlink()
    publisher_backup = backup / "publisher"
    publisher_target = install_root / "max_daily_cloud" / "publisher"
    if publisher_target.exists():
        shutil.rmtree(publisher_target)
    if publisher_backup.is_dir():
        publisher_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(publisher_backup, publisher_target)
    if app_path is not None:
        app_backup = backup / "desktop_app"
        if app_path.exists():
            shutil.rmtree(app_path)
        if app_backup.is_dir():
            app_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(app_backup, app_path)


def write_manifest(install_root: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
    target = install_root / "deployment_manifest.json"
    temp = install_root / f".deployment_manifest.{uuid.uuid4().hex}.tmp"
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, target)
    return payload


def capture_legacy_service_state() -> LegacyServiceState:
    domain = f"gui/{os.getuid()}"
    running_result = subprocess.run(
        ["launchctl", "print", f"{domain}/{LAUNCH_AGENT_LABEL}"],
        check=False,
        capture_output=True,
        text=True,
    )
    disabled_result = subprocess.run(
        ["launchctl", "print-disabled", domain],
        check=False,
        capture_output=True,
        text=True,
    )
    disabled_pattern = rf'"?{re.escape(LAUNCH_AGENT_LABEL)}"?\s*=>\s*(?:true|disabled)'
    disabled = re.search(disabled_pattern, disabled_result.stdout) is not None
    return LegacyServiceState(enabled=not disabled, running=running_result.returncode == 0)


def disable_legacy_service(state: LegacyServiceState) -> None:
    service = f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}"
    if state.enabled:
        subprocess.run(["launchctl", "disable", service], check=True)
    if state.running:
        subprocess.run(["launchctl", "bootout", service], check=True)


def restore_legacy_service(state: LegacyServiceState) -> None:
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{LAUNCH_AGENT_LABEL}"
    if state.enabled:
        subprocess.run(["launchctl", "enable", service], check=False)
        if state.running and LAUNCH_AGENT_PLIST.is_file():
            subprocess.run(["launchctl", "bootstrap", domain, str(LAUNCH_AGENT_PLIST)], check=False)
            subprocess.run(["launchctl", "kickstart", "-k", service], check=False)
    else:
        subprocess.run(["launchctl", "disable", service], check=False)


def quit_native_app() -> None:
    subprocess.run(
        [
            "/usr/bin/osascript",
            "-e",
            f'tell application id "{BUNDLE_IDENTIFIER}" to quit',
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def open_native_app(app_path: Path) -> None:
    subprocess.run(["/usr/bin/open", "-n", str(app_path)], check=True)


def validate_runtime_ownership(
    process_output: str,
    app_path: Path,
    install_root: Path,
    legacy_state: LegacyServiceState,
) -> None:
    if legacy_state.enabled or legacy_state.running:
        raise RuntimeError("旧桌面 LaunchAgent 仍处于启用或运行状态。")

    app_executable = str(app_path / "Contents" / "MacOS" / "CHEN内容采集助手")
    service_script = str(install_root / "content_link_collector.py")
    rows = []
    for line in process_output.splitlines():
        match = re.match(r"^\s*(\d+)\s+(\d+)\s+(.+)$", line)
        if match:
            rows.append((int(match.group(1)), int(match.group(2)), match.group(3)))

    app_pids = [pid for pid, _, command in rows if command == app_executable or command.startswith(f"{app_executable} ")]
    if len(app_pids) != 1:
        raise RuntimeError("无法确认唯一的正式原生 App 进程。")

    service_rows = [
        (pid, parent_pid, command)
        for pid, parent_pid, command in rows
        if service_script in command and " desktop-app " in f" {command} "
    ]
    if len(service_rows) != 1:
        raise RuntimeError("无法确认唯一的正式桌面 Python 服务进程。")
    _, service_parent_pid, _ = service_rows[0]
    if service_parent_pid != app_pids[0]:
        raise RuntimeError("正式桌面 Python 服务的父进程不是原生 App。")


def verify_runtime_ownership(app_path: Path, install_root: Path) -> None:
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,ppid=,command="],
        check=True,
        capture_output=True,
        text=True,
    )
    validate_runtime_ownership(
        result.stdout,
        app_path,
        install_root,
        capture_legacy_service_state(),
    )


def default_health_check(expected_commit: str, attempts: int = 120) -> bool:
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("commit") == expected_commit:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def deploy(
    source_root: Path,
    install_root: Path = DEFAULT_INSTALL_ROOT,
    *,
    app_path: Path = DEFAULT_APP_PATH,
    restart: bool = True,
    verify: bool = True,
    health_check: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    source_root = Path(source_root).resolve()
    install_root = Path(install_root).expanduser().resolve()
    app_path = Path(app_path).expanduser().resolve()
    commit = assert_source_matches_master(source_root)
    with exclusive_deploy_lock(install_root / ".deploy.lock"):
        if verify:
            run_verification(source_root)
        candidate = app_path.parent / f".{app_path.name}.{uuid.uuid4().hex}.candidate"
        build_native_app(source_root, candidate, commit)
        legacy_state = capture_legacy_service_state() if restart else None
        backup = backup_installation(install_root, commit, app_path)
        try:
            if restart:
                quit_native_app()
                disable_legacy_service(legacy_state)
            atomic_install(source_root, install_root)
            atomic_install_app(candidate, app_path)
            manifest = write_manifest(
                install_root,
                {
                    "commit": commit,
                    "source_root": str(source_root),
                    "script_sha256": sha256_file(install_root / "content_link_collector.py"),
                    "app_path": str(app_path),
                    "app_bundle_identifier": BUNDLE_IDENTIFIER,
                    "deployed_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                    "backup_path": str(backup),
                },
            )
            if restart:
                open_native_app(app_path)
            checker = health_check
            uses_default_health_check = checker is None and restart
            if checker is None and restart:
                checker = lambda: default_health_check(commit)
            if checker is not None and not checker():
                raise RuntimeError("部署后的健康检查失败。")
            if uses_default_health_check:
                verify_runtime_ownership(app_path, install_root)
            return manifest
        except Exception:
            if restart:
                quit_native_app()
            restore_backup(install_root, backup, app_path)
            if restart:
                restore_legacy_service(legacy_state)
            raise
        finally:
            if candidate.exists():
                shutil.rmtree(candidate)


def main() -> None:
    parser = argparse.ArgumentParser(description="部署 CHEN 内容采集助手正式桌面运行版")
    parser.add_argument("--source", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--install-root", default=str(DEFAULT_INSTALL_ROOT))
    parser.add_argument("--app-path", default=str(DEFAULT_APP_PATH))
    parser.add_argument("--no-verify", action="store_true")
    parser.add_argument("--no-restart", action="store_true")
    args = parser.parse_args()
    manifest = deploy(
        Path(args.source),
        Path(args.install_root),
        app_path=Path(args.app_path),
        restart=not args.no_restart,
        verify=not args.no_verify,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
