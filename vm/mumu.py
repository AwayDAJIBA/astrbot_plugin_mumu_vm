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
        # 先把「快速隐藏窗口」的循环放出去，再启动模拟器：
        # MuMu 启动瞬间会弹出主窗口，等 launch 返回再 hide 会让用户看到几秒。
        previous_foreground = self._foreground_window()
        hide_task = asyncio.create_task(
            self._hide_loop(previous_foreground=previous_foreground)
        )
        try:
            rc, out, err = await self._manager(
                "control", "-v", str(self.vm_index), "launch", timeout=120
            )
        except Exception:
            hide_task.cancel()
            raise
        if rc != 0:
            hide_task.cancel()
            raise MuMuError(f"启动模拟器失败: {util.truncate(out or err, 300)}")

    async def _hide_loop(
        self, timeout: float = 90, previous_foreground: int = 0
    ) -> None:
        """启动阶段快速藏窗口（轮询 80ms），Android 起来后改成屏幕外显示。

        不能等 launch 命令返回再 hide：MuMu 会先把主窗口摆到桌面上，用户正在
        打游戏时这个窗口会弹出来抢焦点。这里在 launch 还没返回时就开始每 80ms
        把任何可见的 MuMu 窗口移出屏幕/隐藏；Android 启动完成后用
        SW_SHOWNOACTIVATE 在屏幕外恢复显示，不抢前台焦点。
        """
        if not self._cfg("keep_window_hidden", True):
            return
        deadline = time.time() + timeout
        last_check = 0.0
        while time.time() < deadline:
            try:
                self._move_windows_offscreen()
                self._hide_windows()
                # MuMu 启动时可能抢走前台；尽量把用户原来的游戏窗口还回去
                if previous_foreground and self._foreground_is_mumu():
                    self._restore_foreground(previous_foreground)
            except Exception:
                pass
            await asyncio.sleep(0.06)
            now = time.time()
            if now - last_check < 1.0:
                continue
            last_check = now
            try:
                started = await self.is_android_started()
            except Exception:
                started = False
            if started:
                # 启动完成后再恢复屏幕外渲染；不激活、不抢游戏焦点
                try:
                    await self.hide_for_agent(quiet=True)
                    await asyncio.sleep(1.0)
                    await self.hide_for_agent(quiet=True)
                    if previous_foreground and self._foreground_is_mumu():
                        self._restore_foreground(previous_foreground)
                except Exception:
                    pass
                return

    def window_hide_mode(self) -> str:
        """窗口隐藏策略：offscreen（默认，保持渲染/视频）或 hide（真正隐藏）。"""
        mode = str(self._cfg("window_hide_mode", "offscreen") or "offscreen").strip().lower()
        return mode if mode in ("offscreen", "hide") else "offscreen"

    async def hide_for_agent(self, quiet: bool = False) -> bool:
        """按配置隐藏窗口；默认移到屏幕外但保持 show，避免视频/解码器被暂停。

        MuMu 的 hide_window 会销毁/重连 Surface，B 站等视频 App 会因此停止播放
        （logcat 可见 MediaCodec/surface reclaim），画面静止甚至闪烁绿屏。
        offscreen 模式用户桌面上看不到窗口，但模拟器仍认为窗口可见，视频继续播放。
        """
        if self.window_hide_mode() == "hide":
            ok = await self.hide_window(quiet=quiet)
            self._hide_windows()
            return ok
        moved = await self.show_offscreen()
        return bool(moved) or True

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

    # ------------------------------------------------------------------ 窗口渲染控制
    # MuMu 的 hide_window 会让部分 App（例如 B 站）认为窗口不可见而暂停视频，
    # 同时画面不继续渲染，抽帧就会得到静帧。这里提供"移到屏幕外但保持 show"
    # 的隐藏方式：用户桌面看不到窗口，但 Android/模拟器认为它可见，视频继续播放。
    @staticmethod
    def _mumu_process_ids() -> set[int]:
        """MuMu 相关进程 PID（含无标题的渲染窗口进程），缓存 2 秒。"""
        now = time.time()
        cached = getattr(MuMuController, "_mumu_pid_cache", None)
        if cached and now - float(cached[0]) < 2.0:
            return set(cached[1])
        pids: set[int] = set()
        try:
            import psutil

            for proc in psutil.process_iter(["pid", "name"]):
                name = str((proc.info or {}).get("name") or "").lower()
                if "mumu" in name or "nxdevice" in name or "nxmain" in name:
                    pids.add(int(proc.info["pid"]))
        except Exception:
            pass
        MuMuController._mumu_pid_cache = (now, set(pids))
        return pids

    @staticmethod
    def _iter_mumu_windows() -> list[tuple[int, str, bool]]:
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            mumu_pids = MuMuController._mumu_process_ids()
            windows: list[tuple[int, str, bool]] = []

            def _callback(hwnd, _lparam):
                length = user32.GetWindowTextLengthW(hwnd)
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                title = buffer.value
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                # 优先按 MuMu 进程 PID 判断，避免把标题里恰好带 “MuMu” 的
                # 浏览器/AstrBot WebUI 窗口一起隐藏或移出屏幕。
                by_pid = int(pid.value) in mumu_pids
                known_title = title in ("MuMu安卓设备", "MuMu模拟器") or title.startswith(
                    "MuMuPlayer"
                )
                by_title = (not mumu_pids) and (
                    known_title or (bool(title) and "MuMu" in title and "模拟器" in title)
                )
                if by_pid or by_title:
                    label = title or f"MuMu#{int(hwnd)}"
                    windows.append((int(hwnd), label, bool(user32.IsWindowVisible(hwnd))))
                return True

            user32.EnumWindows(
                ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(
                    _callback
                ),
                0,
            )
            return windows
        except Exception:
            return []

    @staticmethod
    def _foreground_window() -> int:
        try:
            import ctypes

            return int(ctypes.windll.user32.GetForegroundWindow() or 0)
        except Exception:
            return 0

    @staticmethod
    def _restore_foreground(hwnd: int) -> None:
        if not hwnd:
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            if not user32.IsWindow(hwnd):
                return
            if int(user32.GetForegroundWindow() or 0) == int(hwnd):
                return
            current = int(user32.GetForegroundWindow() or 0)
            current_thread = 0
            if current:
                current_thread = int(
                    user32.GetWindowThreadProcessId(current, None) or 0
                )
            self_thread = int(kernel32.GetCurrentThreadId())
            attached = False
            if current_thread and current_thread != self_thread:
                try:
                    attached = bool(
                        user32.AttachThreadInput(self_thread, current_thread, True)
                    )
                except Exception:
                    attached = False
            try:
                user32.SetForegroundWindow(hwnd)
            finally:
                if attached:
                    try:
                        user32.AttachThreadInput(self_thread, current_thread, False)
                    except Exception:
                        pass
        except Exception:
            pass

    @classmethod
    def _foreground_is_mumu(cls) -> bool:
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return False
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return int(pid.value) in cls._mumu_process_ids()
        except Exception:
            return False

    @classmethod
    def _move_windows_offscreen(cls) -> list[str]:
        try:
            import ctypes

            user32 = ctypes.windll.user32
            moved: list[str] = []
            for hwnd, title, _visible in cls._iter_mumu_windows():
                user32.SetWindowPos(
                    hwnd, 0, -32000, -32000, 0, 0, 0x0004 | 0x0010 | 0x0001
                )
                moved.append(title)
            return moved
        except Exception:
            return []

    @classmethod
    def _show_windows_noactivate(cls) -> list[str]:
        """用 SW_SHOWNOACTIVATE 显示 MuMu 窗口，避免抢用户游戏的前台焦点。"""
        shown: list[str] = []
        try:
            import ctypes

            user32 = ctypes.windll.user32
            for hwnd, title, _visible in cls._iter_mumu_windows():
                user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
                user32.SetWindowPos(
                    hwnd,
                    0,
                    -32000,
                    -32000,
                    0,
                    0,
                    0x0001 | 0x0004 | 0x0010 | 0x0040,  # NOSIZE|NOZORDER|NOACTIVATE|SHOWWINDOW
                )
                shown.append(title)
        except Exception:
            return []
        return shown

    @classmethod
    def _hide_windows(cls) -> list[str]:
        try:
            import ctypes

            user32 = ctypes.windll.user32
            hidden: list[str] = []
            for hwnd, title, visible in cls._iter_mumu_windows():
                if visible:
                    user32.ShowWindow(hwnd, 0)
                    hidden.append(title)
            return hidden
        except Exception:
            return []

    async def show_offscreen(self) -> list[str]:
        """把所有 MuMu 窗口移到屏幕外并显示，且不抢当前游戏的前台焦点。"""
        previous = self._foreground_window()
        self._move_windows_offscreen()
        shown = self._show_windows_noactivate()
        await asyncio.sleep(0.25)
        self._move_windows_offscreen()
        visible = [title for _, title, vis in self._iter_mumu_windows() if vis]
        if visible:
            return shown or visible
        # 直接 ShowWindow 没有恢复渲染时，退回 MuMuManager show，但马上把前台还给用户
        async with self._lock:
            ok = await self.show_window()
        await asyncio.sleep(0.25)
        self._move_windows_offscreen()
        if ok:
            self._restore_foreground(previous)
        if self.logger and not ok:
            self.logger.warning("MuMu 屏幕外显示失败（show_window 返回失败）")
        return shown

    async def hide_all_windows(self) -> list[str]:
        """恢复真正隐藏：hide_window + 隐藏所有 MuMu 窗口（含启动器）。"""
        await self.hide_window(quiet=True)
        return self._hide_windows()

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
                        await self.hide_for_agent(quiet=True)
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
