# -*- coding: utf-8 -*-
"""adb 客户端：封装 MuMu 模拟器里的安卓控制命令。"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from . import util


class AdbError(RuntimeError):
    pass


# 常用按键
KEYCODE_MAP = {
    "home": 3,
    "back": 4,
    "call": 5,
    "end_call": 6,
    "up": 19,
    "down": 20,
    "left": 21,
    "right": 22,
    "center": 23,
    "volume_up": 24,
    "volume_down": 25,
    "power": 26,
    "camera": 27,
    "clear": 28,
    "tab": 61,
    "space": 62,
    "enter": 66,
    "delete": 67,
    "del": 67,
    "backspace": 67,
    "menu": 82,
    "notification": 83,
    "search": 84,
    "play_pause": 85,
    "media_next": 87,
    "media_previous": 88,
    "media_play": 126,
    "media_pause": 127,
    "mute": 164,
    "wakeup": 224,
    "sleep": 223,
    "page_up": 92,
    "page_down": 93,
    "escape": 111,
    "forward_del": 112,
    "move_home": 122,
    "move_end": 123,
    "switch_app": 187,
    "paste": 279,
    "cut": 277,
    "copy": 278,
    "select_all": 29,
}


class AdbClient:
    """针对单台 MuMu 模拟器的 adb 封装。"""

    def __init__(self, adb_path: str, serial: str, logger=None):
        if not adb_path:
            raise AdbError("adb 路径为空，请检查插件配置 adb_path")
        self.adb_path = adb_path
        self.serial = serial
        self.logger = logger
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ 底层
    def _base(self) -> list[str]:
        return [self.adb_path, "-s", self.serial]

    async def run(
        self,
        args: list[str],
        *,
        timeout: float = 30,
        binary: bool = False,
        locked: bool = True,
    ) -> tuple[int, bytes, bytes]:
        if locked:
            async with self._lock:
                rc, out, err = await util.run_process(
                    [*self._base(), *args], timeout=timeout
                )
        else:
            rc, out, err = await util.run_process(
                [*self._base(), *args], timeout=timeout
            )
        return rc, out, err

    async def shell(self, command: str, timeout: float = 30) -> str:
        """执行 adb shell 命令，返回合并后的文本输出。"""
        if isinstance(command, (list, tuple)):
            args = ["shell", *[str(c) for c in command]]
        else:
            args = ["shell", str(command)]
        rc, out, err = await self.run(args, timeout=timeout)
        text = util.decode(out).strip()
        if not text and err:
            text = util.decode(err).strip()
        if rc != 0 and not text:
            text = f"[adb shell rc={rc}]"
        return text

    async def shell_ok(self, command: str, timeout: float = 30) -> bool:
        rc, _, _ = await self.run(["shell", str(command)], timeout=timeout)
        return rc == 0

    async def exec_out(self, args: list[str], timeout: float = 40) -> bytes:
        rc, out, err = await self.run(["exec-out", *args], timeout=timeout)
        if rc != 0:
            raise AdbError(
                f"adb exec-out {' '.join(args)} 失败(rc={rc}): {util.truncate(util.decode(err), 300)}"
            )
        return out

    async def connect(self) -> bool:
        rc, out, err = await util.run_process(
            [self.adb_path, "connect", self.serial], timeout=20
        )
        text = (util.decode(out) + util.decode(err)).lower()
        return "connected" in text or "already" in text

    async def device_state(self) -> str:
        rc, out, _ = await util.run_process(
            [self.adb_path, "devices"], timeout=10
        )
        for line in util.decode(out).splitlines():
            if line.startswith(self.serial):
                parts = line.split()
                return parts[1] if len(parts) > 1 else "unknown"
        return "offline"

    # ------------------------------------------------------------------ 截屏
    async def screencap_png(self, timeout: float = 30) -> bytes:
        data = await self.exec_out(["screencap", "-p"], timeout=timeout)
        if not data.startswith(b"\x89PNG"):
            raise AdbError("screencap 返回的不是有效 PNG")
        return data

    # ------------------------------------------------------------------ UI / 状态
    async def dump_ui_xml(self, timeout: float = 25) -> str:
        last_err = ""
        for attempt in range(3):
            await self.shell("rm -f /sdcard/mumu_ui.xml", timeout=10)
            rc, out, err = await self.run(
                ["shell", "uiautomator", "dump", "/sdcard/mumu_ui.xml"],
                timeout=timeout,
            )
            text = util.decode(out) + util.decode(err)
            if "dumped" in text or "UI hierchary" in text or rc == 0:
                rc2, data, _ = await self.run(
                    ["exec-out", "cat", "/sdcard/mumu_ui.xml"], timeout=15
                )
                xml = util.decode(data).strip()
                if xml.startswith("<?xml") or "<hierarchy" in xml:
                    return xml
                last_err = util.truncate(xml, 200)
            last_err = util.truncate(text, 300) or last_err
            await asyncio.sleep(0.4 + attempt * 0.4)
        raise AdbError(f"uiautomator dump 失败: {last_err}")

    async def current_focus(self) -> dict[str, str]:
        """返回 {package, activity, window}。"""
        out = await self.shell("dumpsys window", timeout=30)
        package, activity, window = "", "", ""
        for line in out.splitlines():
            line = line.strip()
            if "mCurrentFocus=" in line:
                window = line.split("mCurrentFocus=", 1)[1].strip()
            elif "mFocusedApp=" in line and not activity:
                activity = line.split("mFocusedApp=", 1)[1].strip()
        if window:
            match = re.search(r"([\w.]+)/([\w.$]+)", window)
            if match:
                package, activity = match.group(1), match.group(2)
        if not package and activity:
            match = re.search(r"([\w.]+)/([\w.$]+)", activity)
            if match:
                package, activity = match.group(1), match.group(2)
        return {"package": package, "activity": activity, "window": window}

    async def window_size(self) -> dict[str, int]:
        out = await self.shell("wm size", timeout=15)
        match = re.search(r"(\d+)x(\d+)", out)
        width, height = (int(match.group(1)), int(match.group(2))) if match else (0, 0)
        density_out = await self.shell("wm density", timeout=15)
        dmatch = re.search(r"(\d+)", density_out)
        density = int(dmatch.group(1)) if dmatch else 0
        return {"width": width, "height": height, "density": density}

    async def screen_on(self) -> bool:
        out = await self.shell("dumpsys power", timeout=20)
        return ("mWakefulness=Awake" in out) or ("Display Power: state=ON" in out)

    async def screen_off(self) -> bool:
        out = await self.shell("dumpsys power", timeout=20)
        return "mWakefulness=Asleep" in out or "mWakefulness=Dozing" in out

    async def wake_up(self) -> None:
        if not await self.screen_on():
            await self.key("wakeup")
            await asyncio.sleep(0.3)
        await self.shell("wm dismiss-keyguard", timeout=10)

    async def unlock(self) -> None:
        await self.wake_up()
        for _ in range(2):
            await self.key("wakeup")
            await asyncio.sleep(0.2)
        await self.swipe(540, 1600, 540, 600, 180)
        await asyncio.sleep(0.4)

    # ------------------------------------------------------------------ 输入
    async def tap(self, x: int, y: int) -> None:
        await self.shell(f"input tap {int(x)} {int(y)}", timeout=15)

    async def long_press(self, x: int, y: int, duration_ms: int = 800) -> None:
        duration_ms = int(util.clamp(duration_ms, 300, 5000))
        await self.shell(
            f"input swipe {int(x)} {int(y)} {int(x)} {int(y)} {duration_ms}",
            timeout=20,
        )

    async def swipe(
        self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300
    ) -> None:
        duration_ms = int(util.clamp(duration_ms, 30, 5000))
        await self.shell(
            f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {duration_ms}",
            timeout=20,
        )

    async def key(self, name_or_code: str | int) -> None:
        if isinstance(name_or_code, int) or str(name_or_code).isdigit():
            code = int(name_or_code)
        else:
            code = KEYCODE_MAP.get(str(name_or_code).lower().strip())
            if code is None:
                raise AdbError(
                    f"未知按键: {name_or_code}（可用: {', '.join(sorted(KEYCODE_MAP))}）"
                )
        await self.shell(f"input keyevent {code}", timeout=15)

    async def input_text_ascii(self, text: str) -> None:
        escaped = util.escape_adb_input_text(text)
        await self.shell(f"input text '{escaped}'", timeout=20)

    async def input_text_b64(self, text: str, ime_id: str) -> bool:
        """通过 ADBKeyboard 广播输入任意 Unicode 文本。"""
        enabled = await self.is_package_installed(ime_id.split("/")[0])
        if not enabled:
            return False
        import base64 as _b64

        payload = _b64.b64encode(text.encode("utf-8")).decode("ascii")
        out = await self.shell(
            f"am broadcast -a ADB_INPUT_B64 --es msg {payload}", timeout=20
        )
        return "Broadcast completed" in out or "result=0" in out or out == ""

    async def clear_text(self) -> None:
        await self.shell("am broadcast -a ADB_CLEAR_TEXT", timeout=15)

    async def set_ime(self, ime_id: str) -> bool:
        """启用并切换输入法；返回是否成功。"""
        try:
            await self.shell(f"ime enable {ime_id}", timeout=15)
            await self.shell(f"ime set {ime_id}", timeout=15)
            out = await self.shell("ime list -s", timeout=15)
            return ime_id in out
        except Exception:
            return False

    async def current_ime(self) -> str:
        try:
            out = await self.shell("settings get secure default_input_method", timeout=10)
            return out.strip()
        except Exception:
            return ""

    # ------------------------------------------------------------------ 应用
    async def is_package_installed(self, package: str) -> bool:
        out = await self.shell(f"pm list packages {package}", timeout=15)
        return any(line.strip() == f"package:{package}" for line in out.splitlines())

    async def list_packages(self, third_party_only: bool = True) -> list[str]:
        flag = "-3" if third_party_only else ""
        out = await self.shell(f"pm list packages {flag}", timeout=25)
        packages = []
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("package:"):
                packages.append(line[len("package:") :])
        return sorted(set(packages))

    async def install_apk(self, local_path: str, timeout: float = 180) -> str:
        rc, out, err = await self.run(
            ["install", "-r", str(local_path)], timeout=timeout
        )
        return (util.decode(out) + "\n" + util.decode(err)).strip()

    async def uninstall(self, package: str) -> str:
        return await self.shell(f"pm uninstall {package}", timeout=60)

    async def launch_app(self, package: str) -> str:
        out = await self.shell(
            f"monkey -p {package} -c android.intent.category.LAUNCHER 1", timeout=30
        )
        if "No activities found" in out or "monkey aborted" in out:
            out2 = await self.shell(
                f"am start -a android.intent.action.MAIN -c android.intent.category.LAUNCHER {package}",
                timeout=30,
            )
            return out2
        return out

    async def stop_app(self, package: str) -> str:
        return await self.shell(f"am force-stop {package}", timeout=20)

    async def open_url(self, url: str) -> str:
        safe = url.replace("'", "%27")
        return await self.shell(
            f"am start -a android.intent.action.VIEW -d '{safe}'", timeout=30
        )

    async def app_info(self, package: str) -> dict[str, Any]:
        info: dict[str, Any] = {"package": package, "installed": False}
        out = await self.shell(f"dumpsys package {package}", timeout=40)
        info["installed"] = "Package [" in out or "versionName" in out
        match = re.search(r"versionName=([^\s]+)", out)
        if match:
            info["versionName"] = match.group(1)
        match = re.search(r"firstInstallTime=([^\n]+)", out)
        if match:
            info["firstInstallTime"] = match.group(1).strip()
        match = re.search(r"lastUpdateTime=([^\n]+)", out)
        if match:
            info["lastUpdateTime"] = match.group(1).strip()
        return info

    async def current_app_name(self) -> str:
        focus = await self.current_focus()
        return focus.get("package", "")

    # ------------------------------------------------------------------ 系统
    async def battery(self) -> str:
        return await self.shell("dumpsys battery", timeout=20)

    async def screen_info(self) -> str:
        return await self.shell(
            "dumpsys display | head -n 40", timeout=20
        )

    async def media_volume(self) -> str:
        return await self.shell("cmd media_session volume --show 2>/dev/null || dumpsys audio | grep -i 'stream volume' | head -n 5", timeout=20)

    async def set_brightness(self, value: int) -> str:
        value = int(util.clamp(value, 0, 255))
        return await self.shell(
            f"settings put system screen_brightness {value}", timeout=15
        )

    async def set_volume(self, stream: str, index: int) -> str:
        stream = {"media": "3", "ring": "2", "alarm": "4", "call": "0"}.get(
            stream, "3"
        )
        index = int(util.clamp(index, 0, 15))
        return await self.shell(f"media volume --stream {stream} --set {index}", timeout=20)

    async def rotate(self, orientation: str = "auto") -> str:
        if orientation in ("auto", "unlock", ""):
            return await self.shell(
                "settings put system accelerometer_rotation 1", timeout=15
            )
        mapping = {"portrait": 0, "landscape": 1, "reverse_portrait": 2, "reverse_landscape": 3}
        rot = mapping.get(orientation, 0)
        await self.shell("settings put system accelerometer_rotation 0", timeout=15)
        return await self.shell(f"settings put system user_rotation {rot}", timeout=15)

    async def dismiss_keyguard(self) -> str:
        return await self.shell("wm dismiss-keyguard", timeout=15)

    async def lock_screen(self) -> str:
        return await self.shell("input keyevent 26", timeout=15)

    async def ip_address(self) -> str:
        out = await self.shell("ip -f inet addr show wlan0 2>/dev/null", timeout=15)
        match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        if match:
            return match.group(1)
        out = await self.shell("ip -f inet addr show eth0 2>/dev/null", timeout=15)
        match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        return match.group(1) if match else ""

    async def prop(self, name: str) -> str:
        return (await self.shell(f"getprop {name}", timeout=15)).strip()

    async def screen_record(
        self, device_path: str, time_limit: int = 2, bit_rate: int = 8000000
    ) -> None:
        # 录屏是长命令，绕过全局 adb 锁，允许同时做 input 操作（录到动作过程）
        await self.run(
            [
                "shell",
                "screenrecord",
                "--time-limit",
                str(int(time_limit)),
                "--bit-rate",
                str(int(bit_rate)),
                device_path,
            ],
            timeout=max(30, int(time_limit) + 25),
            locked=False,
        )

    async def pull(self, device_path: str, local_path: Path) -> bool:
        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            rc, out, err = await self.run(
                ["pull", device_path, str(local_path)], timeout=90
            )
            return rc == 0 and local_path.exists()
        except Exception:
            return False

    async def push(self, local_path: str, device_path: str) -> bool:
        rc, _, _ = await self.run(
            ["push", str(local_path), device_path], timeout=120
        )
        return rc == 0

    async def notifications(self, limit: int = 15) -> list[dict[str, Any]]:
        out = await self.shell("dumpsys notification --noredact", timeout=40)
        items: list[dict[str, Any]] = []
        current: dict[str, Any] = {}
        for line in out.splitlines():
            stripped = line.strip()
            match = re.search(r"NotificationRecord\(.*?pkg=([\w.]+)", stripped)
            if match:
                if current.get("package") or current.get("title") or current.get("text"):
                    items.append(current)
                current = {"package": match.group(1)}
            for field, key in (("android.title=", "title"), ("android.text=", "text"), ("android.bigText=", "bigText")):
                if field in stripped:
                    value = stripped.split(field, 1)[1].strip()
                    if value.startswith("String (") and value.endswith(")"):
                        value = value[len("String (") : -1]
                    if value and value != "null" and key not in current:
                        current[key] = util.truncate(value, 300)
            if len(items) >= limit:
                break
        if current.get("package") or current.get("title") or current.get("text"):
            items.append(current)
        return items[:limit]
