# -*- coding: utf-8 -*-
"""MuMu 模拟器（MuMuManager.exe）生命周期控制。"""

from __future__ import annotations

import asyncio
import csv
import json
import re
import time
from pathlib import Path
from typing import Any

from . import util


class MuMuError(RuntimeError):
    pass


class MuMuController:
    """封装 MuMuManager 的 launch/shutdown/info/hide_window 等命令。"""

    # MuMu 在后台常驻的用户级进程（不含 VM 内部进程）
    MUMU_PROCESS_NAMES: tuple[str, ...] = (
        "MuMuNxMain.exe",
        "MuMuNxService.exe",
        "MuMuNxDevice.exe",
        "MuMuNxSVC.exe",
        "MuMuRemoteBackend.exe",
        "MumuRemoteHealthd.exe",
        "MuMuPlayer.exe",
        "MuMuVMMHeadless.exe",
    )
    # MuMu 的 Windows 服务名（只用于状态展示；插件不会修改/停止系统服务）
    MUMU_SERVICE_NAMES: tuple[str, ...] = ("MuMuRemoteService",)

    def __init__(self, config: dict, logger=None):
        self.config = config
        self.logger = logger
        self.manager_path = util.guess_mumu_manager()
        self.adb_path = util.guess_adb(self.manager_path)
        self.vm_index = int(self._cfg("vm_index", 0) or 0)
        self._last_info: dict[str, Any] = {}
        self._last_info_ts = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ 基础
    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        if value is None or value == "":
            return default
        return value

    def _require_manager(self) -> str:
        if not self.manager_path:
            raise MuMuError(
                "找不到 MuMuManager.exe，请在插件配置里填写 mumu_manager_path（MuMu 安装目录/nx_main/MuMuManager.exe）"
            )
        return self.manager_path

    def _require_adb(self) -> str:
        if not self.adb_path:
            raise MuMuError(
                "找不到 adb.exe，请在插件配置里填写 adb_path（通常在 MuMuManager.exe 同目录）"
            )
        return self.adb_path

    async def _manager(self, *args: str, timeout: float = 30) -> tuple[int, str, str]:
        manager = self._require_manager()
        rc, out, err = await util.run_process(
            [manager, *args], timeout=timeout, cwd=str(Path(manager).parent)
        )
        return rc, util.decode(out), util.decode(err)

    async def version(self) -> str:
        try:
            rc, out, _ = await self._manager("version", timeout=15)
            if rc == 0:
                data = json.loads(out.strip() or "{}")
                return str(data.get("version", ""))
        except Exception:
            pass
        return ""

    # ------------------------------------------------------------------ 状态
    async def info(self, use_cache: bool = False) -> dict[str, Any]:
        now = time.time()
        if use_cache and now - self._last_info_ts < 2.0 and self._last_info:
            return self._last_info
        manager = self._require_manager()
        rc, out, err = await self._manager(
            "info", "-v", str(self.vm_index), timeout=20
        )
        raw = (out or "").strip()
        data: dict[str, Any] = {}
        if raw:
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    # 单机版返回 {"0": {...}}，也兼容直接返回详情
                    if str(self.vm_index) in obj and isinstance(
                        obj[str(self.vm_index)], dict
                    ):
                        data = obj[str(self.vm_index)]
                    elif "index" in obj:
                        data = obj
            except Exception:
                match = re.search(r"\{.*\}", raw, re.S)
                if match:
                    try:
                        obj = json.loads(match.group(0))
                        if str(self.vm_index) in obj:
                            data = obj[str(self.vm_index)]
                        elif isinstance(obj, dict):
                            data = obj
                    except Exception:
                        data = {}
        if not data:
            data = {
                "index": str(self.vm_index),
                "is_process_started": False,
                "is_android_started": False,
                "raw_error": util.truncate(err or out, 300),
            }
        self._last_info = data
        self._last_info_ts = time.time()
        if not data.get("adb_host_ip"):
            data["adb_host_ip"] = "127.0.0.1"
        if not data.get("adb_port"):
            data["adb_port"] = 16384 + self.vm_index
        return data

    async def is_running(self) -> bool:
        try:
            data = await self.info()
            return bool(data.get("is_process_started"))
        except Exception:
            return False

    async def is_android_started(self) -> bool:
        try:
            data = await self.info()
            return bool(data.get("is_android_started"))
        except Exception:
            return False

    def serial(self, info: dict[str, Any] | None = None) -> str:
        if info is None:
            info = self._last_info or {}
        host = info.get("adb_host_ip") or "127.0.0.1"
        port = info.get("adb_port") or (16384 + self.vm_index)
        return f"{host}:{port}"

    # ------------------------------------------------------------------ 启停
    async def launch(self) -> None:
        rc, out, err = await self._manager(
            "control", "-v", str(self.vm_index), "launch", timeout=120
        )
        if rc != 0:
            raise MuMuError(f"启动模拟器失败: {util.truncate(out or err, 300)}")
        # 启动后立即尝试隐藏窗口（窗口出现前调用会失败，所以轮询）
        asyncio.create_task(self._hide_loop())

    async def _hide_loop(self, timeout: float = 60) -> None:
        if not self._cfg("keep_window_hidden", True):
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            ok = await self.hide_window(quiet=True)
            if ok:
                # 再确认两次，防止窗口在完全初始化后又弹出来
                await asyncio.sleep(3)
                await self.hide_window(quiet=True)
                await asyncio.sleep(3)
                await self.hide_window(quiet=True)
                return
            await asyncio.sleep(1.5)

    async def hide_window(self, quiet: bool = False) -> bool:
        try:
            rc, out, _ = await self._manager(
                "control", "-v", str(self.vm_index), "hide_window", timeout=15
            )
            if rc == 0:
                return True
            if not quiet:
                self.logger.warning(f"隐藏模拟器窗口失败: {util.truncate(out, 200)}")
        except Exception as exc:
            if not quiet and self.logger:
                self.logger.warning(f"隐藏模拟器窗口异常: {exc!r}")
        return False

    async def show_window(self) -> bool:
        rc, _, _ = await self._manager(
            "control", "-v", str(self.vm_index), "show_window", timeout=15
        )
        return rc == 0

    async def shutdown(self, timeout: float = 60) -> bool:
        try:
            rc, out, err = await self._manager(
                "control", "-v", str(self.vm_index), "shutdown", timeout=timeout
            )
            if rc != 0:
                if self.logger:
                    self.logger.warning(
                        f"关闭模拟器失败: {util.truncate(out or err, 200)}"
                    )
                return False
            return True
        except Exception as exc:
            if self.logger:
                self.logger.warning(f"关闭模拟器异常: {exc!r}")
            return False

    async def restart(self, wait: bool = True, timeout: float | None = None):
        await self.ensure_stopped()
        await self.launch()
        if wait:
            return await self.wait_android_started(timeout=timeout)

    async def ensure_stopped(self) -> None:
        try:
            if await self.is_running():
                await self.shutdown()
                for _ in range(30):
                    if not await self.is_running():
                        return
                    await asyncio.sleep(1)
        except MuMuError:
            raise
        except Exception:
            pass

    # ------------------------------------------------------------------ 后台生命周期
    async def list_processes(self) -> list[str]:
        """列出当前在跑的 MuMu 用户级进程（用 tasklist 一次拿全）。"""
        names = {name.lower() for name in self.MUMU_PROCESS_NAMES}
        try:
            rc, out, err = await util.run_process(
                ["tasklist.exe", "/FO", "CSV", "/NH"], timeout=15
            )
            text = util.decode(out) + util.decode(err)
            running: list[str] = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = next(csv.reader([line]))
                except Exception:
                    row = line.split(",")
                if not row:
                    continue
                name = row[0].strip().strip('"')
                if name.lower() in names:
                    running.append(name)
            return sorted(set(running))
        except Exception:
            return []

    async def service_state(self, name: str) -> str:
        """返回服务的状态（RUNNING/STOPPED/UNKNOWN）。"""
        try:
            rc, out, err = await util.run_process(["sc.exe", "query", name], timeout=15)
            text = util.decode(out) + util.decode(err)
            match = re.search(r"STATE\s*:\s*\d+\s+([A-Z_]+)", text)
            if match:
                return match.group(1)
        except Exception:
            pass
        return "UNKNOWN"

    async def kill_processes(self) -> tuple[list[str], list[str]]:
        """结束 MuMu 的用户级后台进程（MuMuNxService 等），返回 (成功, 失败)。"""
        killed: list[str] = []
        failed: list[str] = []
        for name in await self.list_processes():
            try:
                rc, out, err = await util.run_process(
                    ["taskkill.exe", "/F", "/IM", name], timeout=15
                )
                if rc == 0:
                    killed.append(name)
                else:
                    failed.append(name)
            except Exception:
                failed.append(name)
        return killed, failed

    async def cleanup(self, reason: str = "") -> dict[str, Any]:
        """手机模式结束后关闭 MuMu 主程序/后台进程（可选项）。

        - 只结束本次使用后残留的 MuMu 用户级进程（MuMuNxMain / MuMuNxService 等）；
        - **不修改任何系统设置、服务启动类型或自启动项**；
        - 由 `close_mumu_app_on_exit` 配置控制，默认开；关掉就完全由用户手动收尾。
        """
        if not bool(self._cfg("close_mumu_app_on_exit", True)):
            return {"skipped": "close_mumu_app_on_exit=false"}
        before = await self.list_processes()
        if not before:
            return {"skipped": "no_mumu_processes"}
        killed, failed = await self.kill_processes()
        await asyncio.sleep(0.5)
        # 二次清理：有些组件会在主进程退出后重新拉起
        killed2, failed2 = await self.kill_processes()
        remaining = await self.list_processes()
        summary = {
            "reason": reason,
            "before": before,
            "killed": killed + [n for n in killed2 if n not in killed],
            "failed": sorted(set(failed + failed2)),
            "remaining": remaining,
        }
        if self.logger:
            self.logger.info(
                f"MuMu 主程序清理（{reason or '—'}）: 结束 {summary['killed'] or '无'}；"
                f"剩余 {remaining or '无'}"
            )
        return summary

    # ------------------------------------------------------------------ adb 连接
    async def adb_connect(self, serial: str) -> bool:
        adb = self._require_adb()
        rc, out, err = await util.run_process(
            [adb, "connect", serial], timeout=20
        )
        text = util.decode(out) + util.decode(err)
        return rc == 0 and "connected" in text.lower()

    async def adb_disconnect(self, serial: str) -> None:
        try:
            adb = self._require_adb()
            await util.run_process([adb, "disconnect", serial], timeout=10)
        except Exception:
            pass

    async def wait_android_started(self, timeout: float | None = None) -> dict[str, Any]:
        """等待 Android 启动完成 + adb 可连接，返回 info。"""
        if timeout is None:
            timeout = float(self._cfg("start_timeout_sec", 180))
        deadline = time.time() + timeout
        last_info: dict[str, Any] = {}
        while time.time() < deadline:
            last_info = await self.info(use_cache=False)
            if last_info.get("is_android_started"):
                serial = self.serial(last_info)
                adb = self._require_adb()
                await util.run_process([adb, "connect", serial], timeout=15)
                rc, out, err = await util.run_process(
                    [adb, "-s", serial, "shell", "getprop", "sys.boot_completed"],
                    timeout=15,
                )
                if (util.decode(out) + util.decode(err)).strip() == "1":
                    if self._cfg("keep_window_hidden", True):
                        await self.hide_window(quiet=True)
                    return last_info
            await asyncio.sleep(2)
        raise MuMuError(
            f"等待 Android 启动超时（{int(timeout)}s），当前状态: {util.truncate(json.dumps(last_info, ensure_ascii=False), 300)}"
        )

    # ------------------------------------------------------------------ 设置
    async def get_settings(self) -> dict[str, str]:
        try:
            rc, out, _ = await self._manager("setting", "-v", str(self.vm_index), "-a", timeout=20)
            if rc == 0:
                return {str(k): str(v) for k, v in json.loads(out or "{}").items()}
        except Exception:
            pass
        return {}

    async def set_setting(self, key: str, value: str) -> bool:
        try:
            rc, _, _ = await self._manager(
                "setting", "-v", str(self.vm_index), "-k", key, "--value", value,
                timeout=20,
            )
            return rc == 0
        except Exception:
            return False

    async def apply_performance_profile(self) -> None:
        """按配置调整模拟器性能，让后台隐藏运行时更省资源/更流畅。"""
        if not bool(self._cfg("apply_performance_profile", True)):
            return
        want = {
            "max_frame_rate": str(self._cfg("max_frame_rate", 60)),
            "dynamic_adjust_frame_rate": "true",
            "app_keptlive": "true",
        }
        for key, value in want.items():
            try:
                await self.set_setting(key, value)
            except Exception:
                continue
