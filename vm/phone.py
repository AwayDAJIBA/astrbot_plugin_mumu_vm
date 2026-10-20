# -*- coding: utf-8 -*-
"""Phone：把 MuMu 控制器 + adb + 截图 + UI 解析组合成"一部手机"。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from . import util
from .adb import AdbClient, KEYCODE_MAP
from .mumu import MuMuController, MuMuError
from .screen import Grid, SampleResult, ScreenCapture, Shot
from .ui import UiNode, UiSnapshot, parse_ui_xml


class Phone:
    def __init__(
        self,
        config: dict,
        data_dir: Path,
        shot_dir: Path,
        logger=None,
        assets_dir: Path | None = None,
    ):
        self.config = config
        self.data_dir = data_dir
        self.shot_dir = shot_dir
        self.logger = logger
        self.assets_dir = assets_dir or (data_dir / "assets")
        self.mumu = MuMuController(config, logger=logger)
        self.adb: AdbClient | None = None
        self.screen: ScreenCapture | None = None
        self._lock = asyncio.Lock()
        self._started_at = 0.0
        self._last_ready_check = 0.0
        self._size_cache: dict[str, int] = {}
        self._ui_cache: tuple[float, UiSnapshot] | None = None
        self._ime_ready = False

    # ------------------------------------------------------------------ 配置
    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        if value is None or value == "":
            return default
        return value

    @property
    def is_started(self) -> bool:
        return self.adb is not None and self.screen is not None

    # ------------------------------------------------------------------ 生命周期
    async def ensure_started(self, force: bool = False) -> dict[str, Any]:
        """确保模拟器已启动、窗口隐藏、adb 可连接。返回状态信息。"""
        async with self._lock:
            t0 = time.time()
            info = await self.mumu.info(use_cache=False)
            booted = bool(info.get("is_android_started"))
            if force and booted:
                booted = True
            if not booted:
                if self.logger:
                    self.logger.info("MuMu 虚拟机未启动，正在启动（首次开机可能较慢）…")
                try:
                    await self.mumu.launch()
                except MuMuError:
                    raise
                info = await self.mumu.wait_android_started()
                self._ui_cache = None
                if self.logger:
                    self.logger.info(f"MuMu 虚拟机已启动，耗时 {time.time() - t0:.1f}s")
            serial = self.mumu.serial(info)
            adb = AdbClient(self.mumu._require_adb(), serial, logger=self.logger)
            state = await adb.device_state()
            if state != "device":
                await adb.connect()
                await asyncio.sleep(0.8)
                state = await adb.device_state()
            if state != "device":
                raise MuMuError(
                    f"adb 无法连接 {serial}（state={state}）。请检查 MuMu 的 ADB 调试是否开启。"
                )
            self.adb = adb
            self.screen = ScreenCapture(adb, self.config, self.shot_dir, logger=self.logger)
            if self._cfg("keep_window_hidden", True):
                await self.mumu.hide_window(quiet=True)
            await self._post_boot()
            self._started_at = time.time()
            self._last_ready_check = time.time()
            size = await self.window_size(refresh=True)
            return {
                "serial": serial,
                "screen": f"{size.get('width')}x{size.get('height')}",
                "boot_seconds": round(time.time() - t0, 1),
            }

    async def _post_boot(self) -> None:
        assert self.adb is not None
        adb = self.adb
        try:
            await adb.shell("svc power stayon true", timeout=15)
            await adb.wake_up()
            await asyncio.sleep(0.5)
            await adb.dismiss_keyguard()
            try:
                await adb.shell(
                    "settings put system screen_off_timeout 2147483647", timeout=10
                )
            except Exception:
                pass
            await self._ensure_ime(adb)
        except Exception as exc:
            if self.logger:
                self.logger.warning(f"开机后初始化动作部分失败（可忽略）: {exc!r}")

    async def _ensure_ime(self, adb: AdbClient | None = None) -> bool:
        """安装/启用 ADBKeyboard，用于输入中文。"""
        adb = adb or self.adb
        if adb is None:
            return False
        ime_id = str(
            self._cfg("text_input_ime", "com.android.adbkeyboard/.AdbIME")
        )
        package = ime_id.split("/")[0]
        try:
            installed = await adb.is_package_installed(package)
            if not installed and bool(self._cfg("auto_install_adb_keyboard", True)):
                apk = self.assets_dir / "ADBKeyboard.apk"
                if not apk.exists():
                    apk = self.assets_dir.parent / "assets" / "ADBKeyboard.apk"
                if apk.exists():
                    if self.logger:
                        self.logger.info("正在向虚拟机安装 ADBKeyboard（中文输入用）…")
                    await adb.install_apk(str(apk))
                    await asyncio.sleep(2.5)
                    installed = await adb.is_package_installed(package)
            if not installed:
                return False
            current = await adb.current_ime()
            if current != ime_id:
                for attempt in range(4):
                    ok = await adb.set_ime(ime_id)
                    if ok:
                        break
                    await asyncio.sleep(1.0 + attempt)
            self._ime_ready = True
            return self._ime_ready
        except Exception as exc:
            if self.logger:
                self.logger.warning(f"初始化输入法失败: {exc!r}")
            return False

    async def stop(self) -> bool:
        async with self._lock:
            if self.adb:
                try:
                    serial = self.mumu.serial(await self.mumu.info(use_cache=True))
                    await self.mumu.adb_disconnect(serial)
                except Exception:
                    pass
            self.adb = None
            self.screen = None
            self._ui_cache = None
            self._size_cache = {}
            try:
                return await self.mumu.shutdown()
            except Exception:
                return False

    async def restart(self) -> dict[str, Any]:
        await self.stop()
        await asyncio.sleep(1.0)
        return await self.ensure_started()

    async def ensure_ready(self) -> None:
        """每次操作前快速检查连接，掉线时自动重连。"""
        if self.adb is None or self.screen is None:
            await self.ensure_started()
            return
        now = time.time()
        if now - self._last_ready_check < 8.0:
            return
        self._last_ready_check = now
        state = await self.adb.device_state()
        if state != "device":
            await self.adb.connect()
            await asyncio.sleep(0.5)
            state = await self.adb.device_state()
            if state != "device":
                await self.ensure_started()

    # ------------------------------------------------------------------ 状态
    async def status(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "manager": self.mumu.manager_path,
            "adb": self.mumu.adb_path,
            "serial": self.mumu.serial(),
            "ffmpeg": self.screen.ffmpeg if self.screen else util.guess_ffmpeg(str(self._cfg("ffmpeg_path", ""))),
            "ime_ready": self._ime_ready,
        }
        try:
            info = await self.mumu.info(use_cache=False)
            data.update(
                {
                    "vm_version": await self.mumu.version(),
                    "vm_index": self.mumu.vm_index,
                    "process_started": bool(info.get("is_process_started")),
                    "android_started": bool(info.get("is_android_started")),
                    "player_state": info.get("player_state", ""),
                    "window_hidden": not self._window_visible(info),
                }
            )
        except Exception as exc:
            data["vm_error"] = util.truncate(repr(exc), 200)
        if self.adb:
            try:
                data["device_state"] = await self.adb.device_state()
                data["focus"] = (await self.adb.current_focus()).get("package", "")
                size = await self.window_size()
                data["screen"] = f"{size.get('width')}x{size.get('height')}"
            except Exception as exc:
                data["adb_error"] = util.truncate(repr(exc), 200)
        else:
            data["device_state"] = "not_connected"
        return data

    def _window_visible(self, info: dict[str, Any]) -> bool:
        if not util.IS_WINDOWS:
            return False
        handle = info.get("main_wnd") or ""
        if not handle:
            return False
        try:
            import ctypes

            hwnd = int(str(handle), 16)
            return bool(ctypes.windll.user32.IsWindowVisible(hwnd))
        except Exception:
            return False

    async def window_size(self, refresh: bool = False) -> dict[str, int]:
        if self._size_cache and not refresh:
            return dict(self._size_cache)
        if not self.adb:
            return {"width": 0, "height": 0, "density": 0}
        size = await self.adb.window_size()
        if size.get("width"):
            self._size_cache = size
        return size or {"width": 0, "height": 0, "density": 0}

    async def status_text(self) -> str:
        st = await self.status()
        lines = [
            f"模拟器: {'运行中' if st.get('android_started') else '未启动'}（{st.get('player_state') or '—'}）",
            f"窗口: {'已隐藏（仅 Agent 可见）' if st.get('window_hidden') else '可见/未运行'}",
            f"adb: {st.get('device_state')}  屏幕: {st.get('screen') or '—'}",
            f"前台应用: {st.get('focus') or '—'}",
        ]
        if st.get("vm_error"):
            lines.append(f"⚠️ {st['vm_error']}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 截图
    async def screenshot(
        self,
        max_width: int | None = None,
        region: str = "",
        delay: float = 0.0,
        annotate: bool = False,
    ) -> Shot:
        await self.ensure_ready()
        assert self.screen is not None
        shot = await self.screen.single(
            max_width=max_width, region=region, delay=delay, annotate=annotate
        )
        self._ui_cache = None
        return shot

    async def grid(
        self,
        frames: int = 8,
        fps: int = 10,
        cols: int = 4,
        scale: float = 0.0,
        dedupe: bool = True,
    ) -> Grid:
        await self.ensure_ready()
        assert self.screen is not None
        grid = await self.screen.grid(
            frames=frames, fps=fps, cols=cols, scale=scale, dedupe=dedupe
        )
        self._ui_cache = None
        return grid

    async def sample(
        self,
        *,
        duration: float = 3.0,
        every: float = 0.0,
        fps: float = 0.0,
        count: float = 0.0,
        mode: str = "frames",
        cols: int = 4,
        width: int = 0,
        per_image: int = 0,
        max_images: int = 0,
        dedupe: bool = True,
        annotate: bool = True,
        region: str = "",
        delay: float = 0.0,
    ) -> SampleResult:
        """按 Agent 给的采样参数连续取帧，返回多张图。"""
        await self.ensure_ready()
        assert self.screen is not None
        result = await self.screen.sample(
            duration=duration,
            every=every,
            fps=fps,
            count=count,
            mode=mode,
            cols=cols,
            width=width,
            per_image=per_image,
            max_images=max_images,
            dedupe=dedupe,
            annotate=annotate,
            region=region,
            delay=delay,
        )
        self._ui_cache = None
        return result

    # ------------------------------------------------------------------ UI
    async def ui_snapshot(self, use_cache: bool = True) -> UiSnapshot:
        await self.ensure_ready()
        assert self.adb is not None
        now = time.time()
        if use_cache and self._ui_cache and now - self._ui_cache[0] < 8.0:
            return self._ui_cache[1]
        xml = await self.adb.dump_ui_xml()
        snapshot = parse_ui_xml(xml)
        self._ui_cache = (time.time(), snapshot)
        return snapshot

    async def ui_summary(
        self,
        only_interactive: bool = True,
        max_items: int = 60,
        text_contains: str = "",
    ) -> dict[str, Any]:
        snapshot = await self.ui_snapshot()
        items = snapshot.summary(max_items=200, only_interactive=only_interactive)
        if text_contains:
            needle = text_contains.lower()
            filtered = []
            for item in items:
                hay = " ".join(
                    str(item.get(k, "")) for k in ("t", "id", "c")
                ).lower()
                if needle in hay:
                    filtered.append(item)
            items = filtered[:max_items]
        else:
            items = items[:max_items]
        size = await self.window_size()
        focus = await self.adb.current_focus() if self.adb else {}
        return {
            "screen": f"{size.get('width')}x{size.get('height')}",
            "rotation": snapshot.rotation,
            "package": (focus or {}).get("package") or snapshot.package,
            "count": len(items),
            "elements": items,
        }

    async def find_node(
        self,
        text: str = "",
        index: int | None = None,
    ) -> tuple[UiNode | None, UiSnapshot]:
        snapshot = await self.ui_snapshot()
        node = snapshot.find(text=text, index=index)
        return node, snapshot

    # ------------------------------------------------------------------ 输入
    async def tap(
        self,
        x: int | None = None,
        y: int | None = None,
        text: str = "",
        index: int | None = None,
        long_press: bool = False,
        duration_ms: int = 800,
    ) -> dict[str, Any]:
        await self.ensure_ready()
        assert self.adb is not None
        target_desc = ""
        if (x is None or y is None) and (text or index is not None):
            node, snapshot = await self.find_node(text=text, index=index)
            if node is None:
                return {
                    "ok": False,
                    "error": f"界面上找不到元素：text={text!r} index={index}",
                    "hint": "先用 mumu_phone_ui 查看可点击元素列表",
                }
            px, py = node.center
            x, y = px, py
            target_desc = node.label() or node.short_cls
            self._ui_cache = None
        if x is None or y is None:
            return {"ok": False, "error": "需要提供 x,y 或 text/index"}
        if long_press:
            await self.adb.long_press(int(x), int(y), duration_ms)
            action = "长按"
        else:
            await self.adb.tap(int(x), int(y))
            action = "点击"
        self._ui_cache = None
        return {
            "ok": True,
            "action": action,
            "at": [int(x), int(y)],
            "target": target_desc,
        }

    async def swipe(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 300,
    ) -> dict[str, Any]:
        await self.ensure_ready()
        assert self.adb is not None
        await self.adb.swipe(int(x1), int(y1), int(x2), int(y2), int(duration_ms))
        self._ui_cache = None
        return {"ok": True, "action": "滑动", "from": [int(x1), int(y1)], "to": [int(x2), int(y2)]}

    async def scroll(self, direction: str, amount: float = 0.6) -> dict[str, Any]:
        await self.ensure_ready()
        assert self.adb is not None
        size = await self.window_size()
        w, h = size.get("width") or 1080, size.get("height") or 1920
        amount = float(util.clamp(amount, 0.05, 0.95))
        cx, cy = w // 2, h // 2
        if direction in ("up", "down"):
            dist = int(h * amount / 2)
            if direction == "up":  # 手指向上 -> 页面向下
                x1, y1, x2, y2 = cx, cy + dist, cx, cy - dist
            else:
                x1, y1, x2, y2 = cx, cy - dist, cx, cy + dist
        else:
            dist = int(w * amount / 2)
            if direction == "left":
                x1, y1, x2, y2 = cx + dist, cy, cx - dist, cy
            else:
                x1, y1, x2, y2 = cx - dist, cy, cx + dist, cy
        await self.adb.swipe(x1, y1, x2, y2, 260)
        self._ui_cache = None
        return {"ok": True, "action": f"滚动({direction})"}

    async def input_text(
        self,
        text: str,
        submit: bool = False,
        clear: bool = False,
    ) -> dict[str, Any]:
        await self.ensure_ready()
        assert self.adb is not None
        if clear:
            await self.adb.clear_text()
            await asyncio.sleep(0.15)
        ascii_only = all(ord(ch) < 128 for ch in text)
        used = "adb_input"
        if not self._ime_ready:
            await self._ensure_ime()
        if self._ime_ready:
            ok = await self.adb.input_text_b64(text, str(self._cfg("text_input_ime", "com.android.adbkeyboard/.AdbIME")))
            used = "ADBKeyboard"
            if not ok:
                await ascii_only_fallback(self.adb, text)
                used = "adb_input(fallback)"
        else:
            await ascii_only_fallback(self.adb, text)
            used = "adb_input(仅ASCII)"
        if submit:
            await asyncio.sleep(0.2)
            await self.adb.key("enter")
        self._ui_cache = None
        return {"ok": True, "action": "输入文本", "text": util.truncate(text, 200), "method": used}

    async def key(self, key: str) -> dict[str, Any]:
        await self.ensure_ready()
        assert self.adb is not None
        await self.adb.key(key)
        self._ui_cache = None
        return {"ok": True, "action": f"按键 {key}"}

    # ------------------------------------------------------------------ 应用
    async def app(self, action: str, target: str = "") -> dict[str, Any]:
        await self.ensure_ready()
        assert self.adb is not None
        action = (action or "").strip()
        target = (target or "").strip()
        if action in ("current", "前台"):
            focus = await self.adb.current_focus()
            return {"ok": True, "action": "current", "focus": focus}
        if action in ("list", "列表"):
            packages = await self.adb.list_packages(third_party_only=True)
            return {"ok": True, "action": "list", "count": len(packages), "packages": packages[:100]}
        if action in ("list_all", "全部"):
            packages = await self.adb.list_packages(third_party_only=False)
            return {"ok": True, "action": "list_all", "count": len(packages), "packages": packages[:150]}
        if action in ("search", "搜索"):
            packages = await self.adb.list_packages(third_party_only=False)
            needle = target.lower()
            hits = [p for p in packages if needle in p.lower()]
            return {"ok": True, "action": "search", "query": target, "hits": hits[:50]}
        if action in ("launch", "打开", "启动"):
            package = target
            if not package:
                return {"ok": False, "error": "launch 需要 target=包名"}
            if not await self.adb.is_package_installed(package):
                packages = await self.adb.list_packages(third_party_only=True)
                hits = [p for p in packages if target.lower() in p.lower()]
                if not hits:
                    return {"ok": False, "error": f"未安装该应用且包名模糊搜索无结果: {target}"}
                package = hits[0]
            out = await self.adb.launch_app(package)
            await asyncio.sleep(1.2)
            focus = await self.adb.current_focus()
            self._ui_cache = None
            return {"ok": True, "action": "launched", "package": package, "focus": focus, "output": util.truncate(out, 200)}
        if action in ("open_url", "openurl", "url", "打开链接"):
            if not target:
                return {"ok": False, "error": "open_url 需要 target=URL"}
            out = await self.adb.open_url(target)
            await asyncio.sleep(1.0)
            return {"ok": True, "action": "open_url", "url": target, "output": util.truncate(out, 200)}
        if action in ("stop", "关闭"):
            if not target:
                return {"ok": False, "error": "stop 需要 target=包名"}
            out = await self.adb.stop_app(target)
            return {"ok": True, "action": "stopped", "package": target, "output": util.truncate(out, 120)}
        if action in ("info", "信息"):
            return {"ok": True, "info": await self.adb.app_info(target)}
        if action in ("install", "安装"):
            if not target:
                return {"ok": False, "error": "install 需要 target=本地 apk 绝对路径"}
            local = Path(target)
            if not local.exists():
                return {"ok": False, "error": f"apk 不存在: {target}"}
            out = await self.adb.install_apk(str(local))
            return {"ok": "Success" in out, "action": "install", "output": util.truncate(out, 300)}
        if action in ("uninstall", "卸载"):
            out = await self.adb.uninstall(target)
            return {"ok": "Success" in out, "action": "uninstall", "output": util.truncate(out, 200)}
        return {"ok": False, "error": f"未知 action: {action}（可用: list/search/launch/open_url/stop/current/info/install/uninstall）"}

    # ------------------------------------------------------------------ 系统
    async def system(self, action: str, value: str = "") -> dict[str, Any]:
        await self.ensure_ready()
        assert self.adb is not None
        action = (action or "info").strip()
        adb = self.adb
        if action in ("info", "状态", ""):
            focus = await adb.current_focus()
            size = await self.window_size()
            battery_raw = await adb.battery()
            level = ""
            for line in battery_raw.splitlines():
                if "level:" in line:
                    level = line.split("level:", 1)[1].strip()
                    break
            return {
                "ok": True,
                "model": await adb.prop("ro.product.model"),
                "android": await adb.prop("ro.build.version.release"),
                "brand": await adb.prop("ro.product.brand"),
                "sdk": await adb.prop("ro.build.version.sdk"),
                "battery": level,
                "ip": await adb.ip_address(),
                "focus": focus,
                "screen": f"{size.get('width')}x{size.get('height')} dpi={size.get('density')}",
                "screen_on": await adb.screen_on(),
            }
        if action in ("wake", "解锁", "unlock"):
            await adb.unlock()
            return {"ok": True, "action": "unlock"}
        if action in ("lock", "锁屏"):
            await adb.lock_screen()
            return {"ok": True, "action": "lock"}
        if action in ("volume", "音量"):
            mapping = {"up": "volume_up", "down": "volume_down", "mute": "mute"}
            key = mapping.get(value.strip().lower(), "volume_up")
            await adb.key(key)
            return {"ok": True, "action": f"volume {key}"}
        if action in ("brightness", "亮度"):
            out = await adb.set_brightness(int(float(value or 128)))
            return {"ok": True, "action": "brightness", "output": util.truncate(out, 120)}
        if action in ("rotate", "旋转"):
            out = await adb.rotate(value or "auto")
            self._size_cache = {}
            return {"ok": True, "action": f"rotate {value or 'auto'}", "output": util.truncate(out, 120)}
        if action in ("dismiss_keyguard", "解除锁屏"):
            out = await adb.dismiss_keyguard()
            return {"ok": True, "output": util.truncate(out, 120)}
        if action in ("storage", "存储"):
            return {"ok": True, "output": util.truncate(await adb.shell("df -h /data", timeout=20), 500)}
        if action in ("network", "网络"):
            return {
                "ok": True,
                "ip": await adb.ip_address(),
                "wifi": util.truncate(await adb.shell("dumpsys wifi | head -n 20", timeout=20), 400),
            }
        return {"ok": False, "error": f"未知 action: {action}"}

    async def notifications(self, limit: int = 15) -> list[dict[str, Any]]:
        await self.ensure_ready()
        assert self.adb is not None
        return await self.adb.notifications(limit=limit)

    async def shell(self, command: str, timeout: float = 60) -> str:
        await self.ensure_ready()
        assert self.adb is not None
        return await self.adb.shell(command, timeout=timeout)

    # ------------------------------------------------------------------ 杂项
    async def focus(self) -> dict[str, str]:
        await self.ensure_ready()
        assert self.adb is not None
        return await self.adb.current_focus()


async def ascii_only_fallback(adb: AdbClient, text: str) -> None:
    """没有 ADBKeyboard 时尽量输入；非 ASCII 字符做近似处理。"""
    filtered = "".join(ch for ch in text if ord(ch) < 128)
    if filtered:
        await adb.input_text_ascii(filtered)
    if filtered != text:
        raise RuntimeError(
            "ADBKeyboard 未就绪，中文无法输入。请检查插件配置 auto_install_adb_keyboard 或手动安装 ADBKeyboard。"
        )
