# -*- coding: utf-8 -*-
"""在后台玩手机时静音 MuMu 的 Windows 音频会话。

为什么不用 hide_window：hide 会销毁 Surface/MediaCodec，视频会停（见 v1.6.2）。
为什么不用 Android 音量：会改模拟器音量状态，而且录音/视频可能依赖音频。
这里只对「MuMu 进程」的 Windows 音频会话 SetMute，用户自己的游戏/BGM/耳机
完全不受影响；窗口仍然在屏幕外正常渲染，视频不会停。

实现用 pycaw + 独立线程（COM 必须在同一个线程里初始化/使用）。
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Callable


class MuMuAudioMuter:
    def __init__(
        self,
        config: dict,
        logger=None,
        diag: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.logger = logger
        self._diag = diag or (lambda _msg: None)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._state = "off"  # off / mute / pause
        self._saved: dict[str, tuple[bool, Any]] = {}
        self._last_error = ""
        self._available: bool | None = None

    # ------------------------------------------------------------------ 基础
    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        if value is None or value == "":
            return default
        return value

    def enabled(self) -> bool:
        return bool(self._cfg("mute_mumu_audio", True))

    def _keywords(self) -> tuple[str, ...]:
        raw = str(
            self._cfg("mute_mumu_audio_keywords", "mumu") or "mumu"
        )
        parts = [part.strip().lower() for part in raw.replace("，", ",").split(",")]
        return tuple(part for part in parts if part) or ("mumu",)

    @staticmethod
    def _import_available() -> bool:
        try:
            import pycaw.pycaw  # noqa: F401
            import comtypes  # noqa: F401

            return True
        except Exception:
            return False

    @staticmethod
    def _mumu_sessions(keywords: tuple[str, ...]) -> dict[str, tuple[Any, str]]:
        """返回 key -> (session, process_name)，只包含 MuMu 进程的音频会话。"""
        from pycaw.pycaw import AudioUtilities

        result: dict[str, tuple[Any, str]] = {}
        for session in AudioUtilities.GetAllSessions():
            try:
                proc = session.Process
                if proc is None:
                    continue
                name = str(proc.name() or "")
                lower = name.lower()
                if not any(keyword in lower for keyword in keywords):
                    continue
                pid = int(getattr(proc, "pid", 0) or 0)
                try:
                    instance = str(session.InstanceIdentifier or "")
                except Exception:
                    instance = ""
                key = f"{pid}:{instance}" if instance else f"{pid}:{name}"
                result[key] = (session, name)
            except Exception:
                continue
        return result

    # ------------------------------------------------------------------ 线程内操作
    def _restore_locked(self) -> None:
        if not self._saved:
            return
        # 先尝试当前枚举到的会话（session 对象可能在重新枚举后失效）
        current: dict[str, tuple[Any, str]] = {}
        try:
            current = self._mumu_sessions(self._keywords())
        except Exception:
            current = {}
        for key, (was_muted, old_session) in list(self._saved.items()):
            session = current.get(key, (None, ""))[0] or old_session
            try:
                volume = getattr(session, "SimpleAudioVolume", None)
                if volume is not None:
                    volume.SetMute(1 if was_muted else 0, None)
            except Exception:
                continue
        self._saved.clear()

    def _apply_locked(self) -> None:
        state = self._state
        if state == "off":
            self._restore_locked()
            return
        if state == "pause":
            self._restore_locked()
            return
        # mute
        try:
            sessions = self._mumu_sessions(self._keywords())
        except Exception as exc:
            message = f"枚举 MuMu 音频会话失败: {exc!r}"
            if message != self._last_error:
                self._last_error = message
                self._diag(message)
            return
        for key, (session, name) in sessions.items():
            try:
                volume = getattr(session, "SimpleAudioVolume", None)
                if volume is None:
                    continue
                if key not in self._saved:
                    try:
                        was_muted = bool(volume.GetMute())
                    except Exception:
                        was_muted = False
                    self._saved[key] = (was_muted, session)
                volume.SetMute(1, None)
            except Exception:
                continue
        self._last_error = ""
        # 会话被系统重建后，旧 key 对应的会话可能已不存在；不必强行处理，
        # 下一轮枚举会按新 key 记录并静音。stop/pause 时能恢复的会尽量恢复。

    def _run(self) -> None:
        com_initialized = False
        try:
            try:
                import comtypes

                comtypes.CoInitialize()
                com_initialized = True
            except Exception:
                pass
            poll = float(self._cfg("mute_mumu_audio_poll_sec", 1.0) or 1.0)
            poll = max(0.2, min(5.0, poll))
            while not self._stop.is_set():
                with self._lock:
                    try:
                        self._apply_locked()
                    except Exception as exc:
                        message = f"静音 MuMu 音频失败: {exc!r}"
                        if message != self._last_error:
                            self._last_error = message
                            self._diag(message)
                self._wake.wait(poll)
                self._wake.clear()
            with self._lock:
                self._state = "off"
                self._restore_locked()
        finally:
            if com_initialized:
                try:
                    import comtypes

                    comtypes.CoUninitialize()
                except Exception:
                    pass
            self._thread = None

    # ------------------------------------------------------------------ 异步接口
    def _set_state(self, state: str) -> None:
        if not self.enabled():
            return
        with self._lock:
            self._state = state
        self._wake.set()

    async def start(self) -> bool:
        """进入手机模式时调用：开始静音 MuMu（不阻塞）。"""
        if not self.enabled():
            return False
        if self._available is None:
            self._available = self._import_available()
        if not self._available:
            self._diag("未安装 pycaw/comtypes，无法静音 MuMu 音频（pip install pycaw）")
            return False
        if self._thread is not None and self._thread.is_alive():
            self._set_state("mute")
            return True
        self._stop.clear()
        self._wake.clear()
        with self._lock:
            self._state = "mute"
            self._saved.clear()
        self._thread = threading.Thread(
            target=self._run, name="mumu-audio-mute", daemon=True
        )
        self._thread.start()
        self._diag("手机模式音频静音：已启动 MuMu 音频会话静音线程")
        return True

    async def pause(self) -> None:
        """录音/需要采集 MuMu 音频时临时解除静音。"""
        if not self.enabled() or self._thread is None:
            return
        self._set_state("pause")
        # 给线程一点时间恢复，避免录音开头几帧是静音
        await asyncio.sleep(0.12)

    async def resume(self) -> None:
        """录音结束后恢复 MuMu 静音。"""
        if not self.enabled() or self._thread is None:
            return
        self._set_state("mute")

    async def stop(self) -> None:
        """退出手机模式/卸载插件时恢复用户原来的静音状态。"""
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        self._wake.set()
        try:
            await asyncio.to_thread(thread.join, 2.5)
        except Exception:
            pass
        self._thread = None
        self._stop.clear()
        self._wake.clear()
        with self._lock:
            self._state = "off"
            self._saved.clear()
        self._diag("手机模式音频静音：已恢复 MuMu 音频会话状态")

    def active(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())
