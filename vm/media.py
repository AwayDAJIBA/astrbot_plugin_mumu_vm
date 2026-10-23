# -*- coding: utf-8 -*-
"""媒体辅助：录屏/录音 + STT / 音频多模态 / 视觉(全模态)转述。

设计目标：让没有语音/视频模态的主模型也能"看视频、听声音"。

- 录屏：`adb shell screenrecord` 在后台跑（不占 adb 全局锁），录制期间仍可
  正常点击/滑动/截图；结束后抽帧交给支持图片的模型转述。
- 录音：优先 `soundcard`（Windows WASAPI loopback，抓默认扬声器输出，不需要
  虚拟声卡）；也可用 `ffmpeg -f dshow` 抓指定设备。录制期间同样可操作手机。
- 转述（三者可独立开关、同时启用）：
  - STT：走 AstrBot 已配置的 speech_to_text Provider（provider.get_text）；
  - 音频模型：把 wav 作为 audio_urls 交给支持音频的 chat Provider；
  - 视觉/全模态：录屏抽帧后交给支持图片的 chat Provider 描述。

所有媒体文件默认只用于本次转述，处理完即删（`media_keep_files=true` 可保留）。
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import util

try:  # 可选依赖：音频 loopback 录制
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore

try:
    import soundcard as _sc
except Exception:  # pragma: no cover
    _sc = None  # type: ignore


@dataclass
class MediaStart:
    ok: bool
    text: str
    mode: str = ""
    info: dict[str, Any] = field(default_factory=dict)


class PhoneMedia:
    """录屏/录音与多模态转述的运行时。"""

    AUDIO_SAMPLE_RATE = 48000
    AUDIO_CHANNELS = 2

    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self.config = plugin.config
        self.logger = plugin.logger
        self.dir = Path(plugin.data_dir) / "media"
        util.ensure_dir(self.dir)
        # 录屏
        self._video_task: asyncio.Task | None = None
        self._video_device = ""
        self._video_started = 0.0
        self._video_path: Path | None = None
        self._video_error = ""
        # 录音
        self._audio_task: asyncio.Task | None = None
        self._audio_stop: threading.Event | None = None
        self._audio_path: Path | None = None
        self._audio_started = 0.0
        self._audio_error = ""
        self._audio_proc = None
        self._heartbeat_task: asyncio.Task | None = None
        self._processing = False

    # ------------------------------------------------------------------ 配置
    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        if value is None or value == "":
            return default
        return value

    def _diag(self, message: str) -> None:
        try:
            self.plugin._diag(message)
        except Exception:
            pass

    # ------------------------------------------------------------------ 心跳
    # 录制/转述期间更新 last_tool_ts，避免手机模式的"空闲自动收手"把正在录制的会话掐掉
    async def _heartbeat_loop(self) -> None:
        try:
            while self.video_active() or self.audio_active() or self._processing:
                try:
                    self.plugin.state.last_tool_ts = time.time()
                except Exception:
                    pass
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise
        finally:
            self._heartbeat_task = None

    def _ensure_heartbeat(self) -> None:
        if self._heartbeat_task is None or self._heartbeat_task.done():
            try:
                self._heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(), name="mumu-media-heartbeat"
                )
            except Exception:
                self._heartbeat_task = None

    def _maybe_stop_heartbeat(self) -> None:
        if self.video_active() or self.audio_active() or self._processing:
            return
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is not None and not task.done():
            task.cancel()

    def begin_processing(self) -> None:
        self._processing = True
        self._ensure_heartbeat()

    def end_processing(self) -> None:
        self._processing = False
        self._maybe_stop_heartbeat()

    def assist_flags(self) -> dict[str, bool]:
        return {
            "stt": bool(self._cfg("stt_assist_enable", False)),
            "audio_llm": bool(self._cfg("audio_llm_assist_enable", False)),
            "omni": bool(self._cfg("omni_assist_enable", False)),
        }

    # ------------------------------------------------------------------ 录屏
    def video_active(self) -> bool:
        return self._video_task is not None and not self._video_task.done()

    async def _ensure_phone(self):
        phone = self.plugin.phone
        await phone.ensure_started()
        if not phone.adb:
            raise RuntimeError("adb 未连接")
        return phone

    async def _record_video_task(self, phone, device: str, max_sec: int) -> None:
        try:
            await phone.adb.screen_record(
                device,
                time_limit=max_sec,
                bit_rate=int(self._cfg("media_video_bit_rate", 8000000)),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - 设备差异
            self._video_error = repr(exc)
            self._diag(f"media 录屏异常: {util.truncate(repr(exc), 200)}")

    async def start_video(self) -> MediaStart:
        if not bool(self._cfg("media_assist_enable", True)):
            return MediaStart(False, "媒体辅助已关闭（media_assist_enable=false）")
        if self.video_active():
            return MediaStart(True, "已经在录屏了", "video", {"started_ago": round(time.time() - self._video_started, 1)})
        try:
            phone = await self._ensure_phone()
        except Exception as exc:
            return MediaStart(False, f"启动模拟器失败：{util.truncate(str(exc), 200)}")
        max_sec = int(util.clamp(int(self._cfg("media_record_max_sec", 120)), 5, 180))
        device = f"/sdcard/mumu_media_{int(time.time())}.mp4"
        self._video_device = device
        self._video_started = time.time()
        self._video_path = None
        self._video_error = ""
        self._video_task = asyncio.create_task(
            self._record_video_task(phone, device, max_sec), name="mumu-media-video"
        )
        self._ensure_heartbeat()
        return MediaStart(
            True,
            f"已开始录屏（最多 {max_sec}s），期间可以继续操作手机/截图；结束时调用 mumu_phone_record_stop。",
            "video",
            {"max_seconds": max_sec},
        )

    async def stop_video(self) -> MediaStart:
        if not self._video_task:
            return MediaStart(False, "当前没有在录屏")
        task = self._video_task
        phone = self.plugin.phone
        dev = self._video_device
        if phone.adb and dev:
            try:
                await asyncio.wait_for(
                    phone.adb.shell(
                        "pkill -INT screenrecord 2>/dev/null; sleep 0.3; true",
                        timeout=10,
                    ),
                    timeout=12,
                )
            except Exception:
                pass
        try:
            await asyncio.wait_for(task, timeout=20)
        except Exception:
            task.cancel()
            try:
                await task
            except Exception:
                pass
        local = self.dir / f"video_{int(self._video_started)}.mp4"
        pulled = False
        if phone.adb and dev:
            try:
                pulled = await phone.adb.pull(dev, local)
            except Exception:
                pulled = False
            try:
                await phone.adb.shell(f"rm -f {dev}", timeout=10)
            except Exception:
                pass
        duration = await self._probe_duration(local) if pulled else 0.0
        self._video_task = None
        self._video_device = ""
        self._video_path = local if pulled else None
        self._maybe_stop_heartbeat()
        info = {
            "path": str(local) if pulled else "",
            "duration": round(duration, 2),
            "error": self._video_error,
        }
        if not pulled:
            return MediaStart(False, f"录屏结束但文件拉取失败：{util.truncate(self._video_error, 160)}", "video", info)
        return MediaStart(True, f"录屏完成：约 {duration:.1f}s，文件 {local.name}", "video", info)

    # ------------------------------------------------------------------ 录音
    def _find_loopback(self):
        if _sc is None:
            return None
        try:
            speaker = _sc.default_speaker()
            for mic in _sc.all_microphones(include_loopback=True):
                if mic.name == speaker.name:
                    return mic
            mics = _sc.all_microphones(include_loopback=True)
            return mics[0] if mics else None
        except Exception:
            return None

    def audio_backend_info(self) -> tuple[bool, str]:
        backend = str(self._cfg("media_audio_backend", "soundcard") or "soundcard").lower()
        if backend in ("off", "none", "false"):
            return False, "media_audio_backend=off（未启用录音）"
        if backend == "soundcard":
            if _sc is None or np is None:
                return False, "未安装可选依赖 soundcard/numpy（pip install soundcard）"
            mic = self._find_loopback()
            if not mic:
                return False, "找不到默认扬声器的 loopback 录制设备"
            return True, f"soundcard loopback：{mic.name}"
        if backend == "ffmpeg":
            ff = util.guess_ffmpeg(str(self._cfg("ffmpeg_path", "")))
            device = str(self._cfg("media_audio_device", "") or "").strip()
            if not ff:
                return False, "没有找到 ffmpeg"
            if not device:
                return False, "media_audio_device 为空（ffmpeg 模式下需要填 dshow 音频设备名）"
            return True, f"ffmpeg dshow：{device}"
        return False, f"未知的 media_audio_backend={backend}"

    def audio_active(self) -> bool:
        return self._audio_task is not None and not self._audio_task.done()

    def _record_audio_sync(self, path: Path, max_sec: int, stop_event: threading.Event) -> None:
        try:
            mic = self._find_loopback()
            if not mic:
                raise RuntimeError("找不到 loopback 设备")
            with wave.open(str(path), "wb") as wav, mic.recorder(
                samplerate=self.AUDIO_SAMPLE_RATE, channels=self.AUDIO_CHANNELS
            ) as rec:
                wav.setnchannels(self.AUDIO_CHANNELS)
                wav.setsampwidth(2)
                wav.setframerate(self.AUDIO_SAMPLE_RATE)
                start = time.time()
                while not stop_event.is_set() and time.time() - start < max_sec:
                    data = rec.record(numframes=4800)
                    pcm = np.clip(data, -1.0, 1.0)
                    wav.writeframes((pcm * 32767).astype("int16").tobytes())
        except Exception as exc:  # pragma: no cover - 设备差异
            self._audio_error = repr(exc)
            self._diag(f"media 录音异常: {util.truncate(repr(exc), 200)}")

    async def start_audio(self) -> MediaStart:
        if not bool(self._cfg("media_assist_enable", True)):
            return MediaStart(False, "媒体辅助已关闭（media_assist_enable=false）")
        if self.audio_active():
            return MediaStart(True, "已经在录音了", "audio", {"started_ago": round(time.time() - self._audio_started, 1)})
        ok, why = self.audio_backend_info()
        if not ok:
            return MediaStart(False, why)
        max_sec = int(util.clamp(int(self._cfg("media_record_max_sec", 120)), 5, 180))
        path = self.dir / f"audio_{int(time.time())}.wav"
        self._audio_path = path
        self._audio_error = ""
        self._audio_started = time.time()
        backend = str(self._cfg("media_audio_backend", "soundcard") or "soundcard").lower()
        if backend == "soundcard":
            self._audio_stop = threading.Event()
            self._audio_task = asyncio.create_task(
                asyncio.to_thread(self._record_audio_sync, path, max_sec, self._audio_stop),
                name="mumu-media-audio",
            )
        else:
            ff = util.guess_ffmpeg(str(self._cfg("ffmpeg_path", "")))
            device = str(self._cfg("media_audio_device", "") or "").strip()
            self._audio_stop = None
            self._audio_proc = await asyncio.create_subprocess_exec(
                ff,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "dshow",
                "-i",
                f"audio={device}",
                "-t",
                str(max_sec),
                "-ac",
                str(self.AUDIO_CHANNELS),
                "-ar",
                str(self.AUDIO_SAMPLE_RATE),
                str(path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            self._audio_task = asyncio.create_task(self._audio_proc.wait(), name="mumu-media-audio")
        self._ensure_heartbeat()
        return MediaStart(
            True,
            f"已开始录音（最多 {max_sec}s，后端：{why}），期间可以继续操作手机；结束时调用 mumu_phone_hear_stop。",
            "audio",
            {"max_seconds": max_sec, "backend": backend},
        )

    async def stop_audio(self) -> MediaStart:
        if not self._audio_task:
            return MediaStart(False, "当前没有在录音")
        task = self._audio_task
        if self._audio_stop is not None:
            self._audio_stop.set()
        if self._audio_proc is not None:
            try:
                self._audio_proc.terminate()
            except Exception:
                pass
        try:
            await asyncio.wait_for(task, timeout=15)
        except Exception:
            task.cancel()
            try:
                await task
            except Exception:
                pass
        self._audio_proc = None
        self._audio_stop = None
        self._audio_task = None
        path = self._audio_path
        self._audio_path = None
        self._maybe_stop_heartbeat()
        if not path or not path.exists() or path.stat().st_size <= 44:
            return MediaStart(False, f"录音失败：{util.truncate(self._audio_error, 160) or '没有录到音频'}", "audio")
        duration = self._wav_duration(path)
        return MediaStart(
            True,
            f"录音完成：约 {duration:.1f}s，文件 {path.name}",
            "audio",
            {"path": str(path), "duration": round(duration, 2)},
        )

    @staticmethod
    def _wav_duration(path: Path) -> float:
        try:
            with wave.open(str(path), "rb") as wav:
                return wav.getnframes() / float(wav.getframerate() or 1)
        except Exception:
            return 0.0

    # ------------------------------------------------------------------ 抽帧 / 时长
    async def _probe_duration(self, path: Path) -> float:
        ff = util.guess_ffmpeg(str(self._cfg("ffmpeg_path", "")))
        if not ff:
            return 0.0
        ffprobe = Path(ff).with_name("ffprobe.exe")
        try:
            if ffprobe.exists():
                rc, out, err = await util.run_process(
                    [
                        str(ffprobe),
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "default=noprint_wrappers=1:nokey=1",
                        str(path),
                    ],
                    timeout=30,
                )
                text = util.decode(out).strip()
                if text:
                    return float(text)
        except Exception:
            pass
        try:
            rc, out, err = await util.run_process(
                [ff, "-hide_banner", "-i", str(path)], timeout=30
            )
            text = util.decode(out) + util.decode(err)
            match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", text)
            if match:
                return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
        except Exception:
            pass
        return 0.0

    async def extract_frames(self, video: Path, count: int = 0, width: int = 0) -> list[Path]:
        ff = util.guess_ffmpeg(str(self._cfg("ffmpeg_path", "")))
        if not ff:
            return []
        count = int(util.clamp(int(count or self._cfg("media_video_frames", 6)), 1, 12))
        width = int(util.clamp(int(width or self._cfg("media_video_frame_width", 768)), 200, 1280))
        duration = await self._probe_duration(video)
        duration = max(duration, 0.6)
        fps = max(0.05, min(10.0, count / duration))
        out_dir = self.dir / "frames"
        util.ensure_dir(out_dir)
        for old in out_dir.glob("f_*.jpg"):
            try:
                old.unlink()
            except Exception:
                pass
        try:
            await util.run_process(
                [
                    ff,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(video),
                    "-vf",
                    f"fps={fps:.4f},scale='min({width},iw)':-2",
                    "-frames:v",
                    str(count),
                    str(out_dir / "f_%02d.jpg"),
                ],
                timeout=120,
            )
        except Exception as exc:
            self._diag(f"media 抽帧失败: {util.truncate(repr(exc), 200)}")
            return []
        return sorted(out_dir.glob("f_*.jpg"))

    # ------------------------------------------------------------------ Provider 选择
    def _manager(self):
        return getattr(self.plugin.context, "provider_manager", None)

    def _provider_by_id(self, provider_id: str):
        manager = self._manager()
        if not manager or not provider_id:
            return None
        try:
            return getattr(manager, "inst_map", {}).get(provider_id)
        except Exception:
            return None

    @staticmethod
    def _modalities(provider) -> list[str]:
        try:
            cfg = getattr(provider, "provider_config", {}) or {}
            mods = cfg.get("modalities") or []
            return [str(item).lower() for item in mods]
        except Exception:
            return []

    def _current_chat_provider(self):
        try:
            return self.plugin.context.get_using_provider(
                self.plugin.state.owner_umo or None
            )
        except Exception:
            return None

    def _pick_chat_provider(self, need: str, configured: str = ""):
        provider = self._provider_by_id(configured) if configured else None
        if provider is not None:
            return provider
        manager = self._manager()
        if manager:
            for item in getattr(manager, "provider_insts", []) or []:
                if need in self._modalities(item):
                    return item
        current = self._current_chat_provider()
        if current is not None:
            mods = self._modalities(current)
            if not mods or need in mods:
                return current
        return None

    @staticmethod
    def _provider_label(provider) -> str:
        try:
            meta = provider.meta()
            return str(getattr(meta, "id", "") or getattr(meta, "model", "") or type(provider).__name__)
        except Exception:
            return type(provider).__name__

    def _stt_provider(self):
        manager = self._manager()
        if not manager:
            return None
        provider = getattr(manager, "curr_stt_provider_inst", None)
        if provider is not None:
            return provider
        insts = getattr(manager, "stt_provider_insts", []) or []
        if insts:
            return insts[0]
        return None

    # ------------------------------------------------------------------ 转述
    async def transcribe_audio(self, audio: Path) -> tuple[bool, str]:
        provider = self._stt_provider()
        if provider is None:
            return False, "未配置可用的 STT Provider（在 AstrBot 设置→语音转文字 中启用）"
        label = self._provider_label(provider)
        self._diag(f"media STT 使用 Provider={label}")
        try:
            text = await provider.get_text(audio_url=str(audio))
            text = str(text or "").strip()
            if not text:
                return False, "STT 没有返回文字（可能没有语音）"
            return True, text
        except Exception as exc:
            return False, f"STT 失败（{label}）：{util.truncate(repr(exc), 200)}"

    async def describe_audio(self, audio: Path, question: str = "") -> tuple[bool, str]:
        provider = self._pick_chat_provider(
            "audio", str(self._cfg("audio_llm_provider_id", "") or "")
        )
        if provider is None:
            return False, "没有可用的音频多模态模型（配置 audio_llm_provider_id，例如 MiniMax/MiMo 音频模型）"
        label = self._provider_label(provider)
        self._diag(
            f"media 音频 Provider={label} modalities={self._modalities(provider)}"
        )
        prompt = (
            "你是一个音频转述助手。请用简洁中文描述这段安卓手机录到的音频："
            "听到的语音内容、语气情绪、声音事件、音乐/环境音等。"
            "只输出你听到的事实，不要猜测画面。"
        )
        if question.strip():
            prompt += f"\n用户想重点关注：{question.strip()}"
        try:
            response = await provider.text_chat(prompt=prompt, audio_urls=[str(audio)])
            text = str(getattr(response, "completion_text", "") or "").strip()
            if not text:
                return False, f"音频模型没有返回内容（{label}）"
            return True, text
        except Exception as exc:
            return False, f"音频模型转述失败（{label}）：{util.truncate(repr(exc), 240)}"

    async def describe_video(self, video: Path, question: str = "") -> tuple[bool, str, list[Path]]:
        frames = await self.extract_frames(video)
        if not frames:
            return False, "录屏抽帧失败（检查 ffmpeg 是否可用）", []
        provider = self._pick_chat_provider(
            "image", str(self._cfg("omni_provider_id", "") or "")
        )
        if provider is None:
            return False, "没有可用的视觉/全模态模型（配置 omni_provider_id，需支持图片输入）", frames
        label = self._provider_label(provider)
        self._diag(
            f"media 视觉 Provider={label} modalities={self._modalities(provider)}"
        )
        duration = await self._probe_duration(video)
        prompt = (
            f"你是一个视觉转述助手。下面是安卓手机录屏按时间顺序截取的 {len(frames)} 张画面"
            f"（录屏约 {duration:.0f} 秒）。请用简洁中文描述："
            "当前是什么应用/界面、屏幕上有什么文字或内容、人物/动画/视频里发生了什么、"
            "画面随时间有什么变化。只输出看到的事实，不要猜测。"
        )
        if question.strip():
            prompt += f"\n用户想重点关注：{question.strip()}"
        try:
            response = await provider.text_chat(
                prompt=prompt, image_urls=[str(frame) for frame in frames]
            )
            text = str(getattr(response, "completion_text", "") or "").strip()
            if not text:
                return False, f"视觉/全模态模型没有返回内容（{label}）", frames
            return True, text, frames
        except Exception as exc:
            return False, f"视觉/全模态转述失败（{label}）：{util.truncate(repr(exc), 240)}", frames

    # ------------------------------------------------------------------ 状态/清理
    def status_text(self) -> str:
        flags = self.assist_flags()
        audio_ok, audio_why = self.audio_backend_info()
        lines = [
            f"媒体辅助：STT={'开' if flags['stt'] else '关'}，"
            f"音频模型={'开' if flags['audio_llm'] else '关'}，"
            f"视觉/全模态={'开' if flags['omni'] else '关'}",
            f"录音后端：{'可用（' + audio_why + '）' if audio_ok else '不可用（' + audio_why + '）'}",
            f"录屏状态：{'录制中' if self.video_active() else '未录制'}；"
            f"录音状态：{'录制中' if self.audio_active() else '未录制'}",
        ]
        return "\n".join(lines)

    async def abort_all(self) -> None:
        """收手/卸载时停止所有录制并清理临时文件。"""
        self._processing = False
        try:
            if self.audio_active():
                await self.stop_audio()
        except Exception:
            pass
        try:
            if self.video_active():
                await self.stop_video()
        except Exception:
            pass
        self._maybe_stop_heartbeat()
        await self.cleanup_outputs()

    async def cleanup_outputs(self) -> None:
        if bool(self._cfg("media_keep_files", False)):
            return
        try:
            for pattern in ("video_*.mp4", "audio_*.wav", "frames/f_*.jpg"):
                for path in self.dir.glob(pattern):
                    try:
                        path.unlink()
                    except Exception:
                        pass
        except Exception:
            pass
