# -*- coding: utf-8 -*-
"""截图 / 连续采样：把"视频画面"拆成多张带时间戳的图片给 Agent 看。

单帧：adb screencap -p 直接取 PNG。
连续采样：按 Agent 指定的采样频率/时长/帧数取多帧，支持
  - mode=frames：一帧一张图（信息量最大，可一次返回多张图）；
  - mode=sheet ：多帧拼成图版，一帧信息量小但省上下文；
密集采样（间隔 < 0.8s）走 screenrecord + ffmpeg 抽帧，稀疏采样直接连续 screencap；
没有 ffmpeg 时自动退化为连续截图。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import util
from .adb import AdbClient, AdbError

try:  # Pillow 随 AstrBot 提供
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # pragma: no cover
    Image = None  # type: ignore
    ImageDraw = None  # type: ignore
    ImageFont = None  # type: ignore


@dataclass
class Shot:
    png: bytes
    path: Path
    width: int
    height: int
    full_width: int
    full_height: int
    region: tuple[int, int, int, int] | None = None
    ts: float = field(default_factory=time.time)


@dataclass
class Grid:
    png: bytes
    path: Path
    width: int
    height: int
    frames_used: int
    unique_frames: int
    method: str
    duration: float
    notes: list[str] = field(default_factory=list)
    screen_size: tuple[int, int] = (0, 0)


@dataclass
class SampleImage:
    """连续采样结果里的一张图（一帧，或一版拼图）。"""

    png: bytes
    path: Path
    label: str
    frame_start: int
    frame_end: int
    width: int
    height: int
    mime: str = "image/png"


@dataclass
class SampleResult:
    images: list[SampleImage]
    frames_total: int
    sample_times: list[float]
    method: str
    interval: float
    duration: float
    mode: str
    screen_size: tuple[int, int] = (0, 0)
    notes: list[str] = field(default_factory=list)


class ScreenCapture:
    def __init__(self, adb: AdbClient, config: dict, shot_dir: Path, logger=None):
        self.adb = adb
        self.config = config
        self.shot_dir = shot_dir
        self.logger = logger
        self.ffmpeg = util.guess_ffmpeg(str(self._cfg("ffmpeg_path", "") or ""))
        util.ensure_dir(self.shot_dir)

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        if value is None or value == "":
            return default
        return value

    # ------------------------------------------------------------------ 单帧
    async def single(
        self,
        max_width: int | None = None,
        region: str = "",
        delay: float = 0.0,
        annotate: bool = False,
    ) -> Shot:
        if Image is None:
            raise RuntimeError("缺少 Pillow，无法处理截图")
        if delay and delay > 0:
            await asyncio.sleep(min(float(delay), 10.0))
        png = await self.adb.screencap_png()
        img = Image.open(io.BytesIO(png)).convert("RGB")
        full_w, full_h = img.size
        crop: tuple[int, int, int, int] | None = None
        if region:
            crop = self._parse_region(region, full_w, full_h)
            x, y, w, h = crop
            img = img.crop((x, y, x + w, y + h))
        max_w = int(max_width or self._cfg("single_frame_max_width", 1280))
        if max_w > 0 and img.width > max_w:
            ratio = max_w / img.width
            img = img.resize((max_w, max(1, int(img.height * ratio))), Image.LANCZOS)
        if annotate:
            img = self._annotate(img)
        out = io.BytesIO()
        img.save(out, format="PNG", optimize=True)
        data = out.getvalue()
        path = self.shot_dir / f"shot_{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time()*1000)%1000:03d}.png"
        path.write_bytes(data)
        util.cleanup_dir(self.shot_dir, keep=200)
        return Shot(
            png=data,
            path=path,
            width=img.width,
            height=img.height,
            full_width=full_w,
            full_height=full_h,
            region=crop,
        )

    # ------------------------------------------------------------------ 多帧
    async def grid(
        self,
        frames: int = 8,
        fps: int = 10,
        cols: int = 4,
        scale: float = 0.0,
        dedupe: bool = True,
    ) -> Grid:
        if Image is None:
            raise RuntimeError("缺少 Pillow，无法处理截图")
        frames = int(util.clamp(int(frames or 8), 1, int(self._cfg("burst_max_frames", 20))))
        fps = int(util.clamp(int(fps or 10), 1, 30))
        cols = int(util.clamp(int(cols or 4), 1, 8))
        duration = frames / float(fps)
        notes: list[str] = []
        screen_size = (0, 0)

        decoded: list[Image.Image] = []
        method = "screenrecord+ffmpeg"
        video_path: Path | None = None
        try:
            decoded, screen_size = await self._capture_via_screenrecord(
                frames, fps, duration
            )
        except Exception as exc:
            notes.append(f"screenrecord 失败，已用连续截图兜底：{util.truncate(repr(exc), 160)}")
            decoded = []
            method = "screencap-burst"

        if len(decoded) == 0:
            fallback, size2 = await self._capture_via_screenshots(frames, fps)
            if fallback:
                decoded = fallback
                screen_size = size2 or screen_size
                method = "screencap-burst"
                notes = ["录屏没有解出帧，已用连续截图兜底。"]
        if not decoded:
            raise AdbError("没有抓到任何帧")

        # 复制补齐到 frames 个
        if len(decoded) < frames:
            if len(decoded) <= 2:
                notes.append("画面几乎静止，重复帧已合并显示。")
            while len(decoded) < frames:
                decoded.append(decoded[-1].copy())

        # 去重：连续相同帧合并
        groups: list[tuple[Image.Image, int, int]] = []  # (img, start_i, count)
        if dedupe:
            for idx, img in enumerate(decoded):
                if groups and self._similar(groups[-1][0], img):
                    prev = groups[-1]
                    groups[-1] = (prev[0], prev[1], prev[2] + 1)
                else:
                    groups.append((img, idx, 1))
        else:
            groups = [(img, idx, 1) for idx, img in enumerate(decoded)]

        unique = len(groups)
        if unique < frames and "重复" not in "".join(notes):
            notes.append(f"共 {frames} 帧，其中连续相同/近似帧已合并为 {unique} 格。")

        # 尺寸：按配置的最大网格宽度与列数算每格宽度
        grid_max_width = int(self._cfg("grid_max_width", 2000))
        tile_w = int((grid_max_width - (cols + 1) * 6) / cols)
        if scale and 0 < float(scale) <= 1:
            tile_w = int(tile_w * float(scale))
        tile_w = int(util.clamp(tile_w, 220, 720))
        # 保持第一帧比例
        base_w, base_h = groups[0][0].size
        tile_h = max(1, int(tile_w * base_h / base_w))
        label_h = 22 if bool(self._cfg("grid_label_frames", True)) else 0
        effective_cols = max(1, min(cols, unique))
        rows = math.ceil(unique / effective_cols)
        pad = 6
        width = effective_cols * (tile_w + pad) + pad
        height = rows * (tile_h + label_h + pad) + pad
        canvas = Image.new("RGB", (width, height), (18, 18, 18))
        draw = ImageDraw.Draw(canvas)
        font = self._label_font()
        for i, (img, start_i, count) in enumerate(groups):
            x = pad + (i % effective_cols) * (tile_w + pad)
            y = pad + (i // effective_cols) * (tile_h + label_h + pad)
            tile = img.resize((tile_w, tile_h), Image.LANCZOS)
            canvas.paste(tile, (x, y))
            if label_h:
                t_ms = start_i * (1000.0 / fps)
                label = f"#{start_i + 1} {t_ms:.0f}ms"
                if count > 1:
                    label += f" ×{count}"
                draw.text((x + 3, y + tile_h + 3), label, fill=(255, 214, 0), font=font)
        out = io.BytesIO()
        canvas.save(out, format="PNG", optimize=True)
        data = out.getvalue()
        path = self.shot_dir / f"grid_{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time()*1000)%1000:03d}.png"
        path.write_bytes(data)
        util.cleanup_dir(self.shot_dir, keep=200)
        return Grid(
            png=data,
            path=path,
            width=width,
            height=height,
            frames_used=frames,
            unique_frames=unique,
            method=method,
            duration=duration,
            notes=notes,
            screen_size=screen_size,
        )

    # ------------------------------------------------------------------ 内部
    async def _capture_via_screenrecord(
        self, frames: int, fps: int, duration: float
    ) -> tuple[list["Image.Image"], tuple[int, int]]:
        if Image is None:
            return [], (0, 0)
        device_path = "/sdcard/mumu_vm_rec.mp4"
        await self.adb.shell(f"rm -f {device_path}", timeout=10)
        limit = int(util.clamp(math.ceil(duration) + 1, 1, 5))
        await self.adb.screen_record(device_path, time_limit=limit)
        local = self.shot_dir / "_rec.mp4"
        ok = await self.adb.pull(device_path, local)
        if not ok:
            raise AdbError("拉取录屏文件失败")
        frames_img: list[Image.Image] = []
        if self.ffmpeg:
            frames_img = await self._ffmpeg_decode(local, duration, fps)
        if not frames_img:
            # 没有 ffmpeg 或解码失败：用连续截图
            raise AdbError("ffmpeg 抽帧失败或未安装")
        size = frames_img[0].size
        return frames_img, size

    async def _ffmpeg_decode(
        self, video: Path, duration: float, fps: int
    ) -> list["Image.Image"]:
        if Image is None or not self.ffmpeg:
            return []
        frames_dir = self.shot_dir / "_frames"
        util.ensure_dir(frames_dir)
        for old in frames_dir.glob("*.jpg"):
            try:
                old.unlink()
            except Exception:
                pass
        # 先按 fps 抽帧；绝对时长限制到 duration（+0.05s 容差）
        args = [
            self.ffmpeg, "-hide_banner", "-loglevel", "error",
            "-ss", "0", "-i", str(video),
            "-t", f"{max(0.1, duration):.2f}",
            "-vf", f"fps={fps},scale='min(960,iw)':-2",
            "-q:v", "2", "-frames:v", "60",
            str(frames_dir / "f_%03d.jpg"),
        ]
        rc, _, err = await util.run_process(args, timeout=90)
        files = sorted(frames_dir.glob("f_*.jpg"))
        if len(files) < 2:
            # 静态画面/时间戳异常：不按 fps，直接解出所有帧（最多 60）
            for old in frames_dir.glob("*.jpg"):
                try:
                    old.unlink()
                except Exception:
                    pass
            args2 = [
                self.ffmpeg, "-hide_banner", "-loglevel", "error",
                "-i", str(video),
                "-vsync", "0", "-frames:v", "60",
                "-vf", "scale='min(960,iw)':-2",
                "-q:v", "2", str(frames_dir / "f_%03d.jpg"),
            ]
            await util.run_process(args2, timeout=90)
            files = sorted(frames_dir.glob("f_*.jpg"))
        images: list[Image.Image] = []
        for path in files:
            try:
                images.append(Image.open(path).convert("RGB"))
            except Exception:
                continue
        return images

    async def _capture_via_screenshots(
        self, frames: int, fps: int
    ) -> tuple[list["Image.Image"], tuple[int, int]]:
        if Image is None:
            return [], (0, 0)
        images: list[Image.Image] = []
        interval = 1.0 / max(1, fps)
        size = (0, 0)
        for idx in range(frames):
            try:
                png = await self.adb.screencap_png()
            except Exception:
                break
            try:
                img = Image.open(io.BytesIO(png)).convert("RGB")
            except Exception:
                continue
            if idx == 0:
                size = img.size
            if img.size != size:
                img = img.resize(size, Image.LANCZOS)
            images.append(img)
            if idx < frames - 1:
                await asyncio.sleep(interval)
        return images, size

    @staticmethod
    def _similar(a: "Image.Image", b: "Image.Image", threshold: float = 0.012) -> bool:
        """快速判断两帧是否近似相同（缩略图逐像素平均差）。"""
        try:
            if a is b:
                return True
            ta = a.resize((32, 18)).convert("L")
            tb = b.resize((32, 18)).convert("L")
            pa = list(ta.getdata())
            pb = list(tb.getdata())
            diff = sum(abs(x - y) for x, y in zip(pa, pb)) / (len(pa) * 255.0)
            return diff < threshold
        except Exception:
            return False

    # ------------------------------------------------------------------ 连续采样
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
        """按指定采样参数连续取帧，返回多张图片。

        采样方式（优先级从高到低）：count（总帧数）> fps（帧/秒）> every（秒/帧）。
        mode=frames：一帧一张图；mode=sheet：多帧拼成图版（自动按 per_image 分页成多张）。
        """
        if Image is None:
            raise RuntimeError("缺少 Pillow，无法处理截图")
        max_duration = float(self._cfg("frames_max_duration_sec", 20))
        max_count = int(self._cfg("frames_max_count", 24))
        max_images_cfg = int(self._cfg("frames_max_images", 8))
        duration = float(util.clamp(float(duration or 3.0), 0.5, max_duration))
        max_images = int(util.clamp(int(max_images or max_images_cfg), 1, 16))
        cols = int(util.clamp(int(cols or 4), 1, 8))
        mode = (mode or "frames").strip().lower()
        if mode not in ("frames", "sheet", "images", "图", "拼图"):
            mode = "frames"
        mode = "sheet" if mode in ("sheet", "拼图") else "frames"
        interval = 0.0
        count_i = 0
        if count and float(count) > 0:
            count_i = int(util.clamp(int(count), 1, max_count))
            interval = duration / count_i
        elif fps and float(fps) > 0:
            interval = 1.0 / float(util.clamp(float(fps), 0.1, 30.0))
            count_i = int(math.floor(duration / interval)) + 1
        elif every and float(every) > 0:
            interval = float(util.clamp(float(every), 1 / 30.0, 60.0))
            count_i = int(math.floor(duration / interval)) + 1
        else:
            # 默认：3 秒内每 0.5 秒一帧（2fps）
            interval = 0.5
            count_i = int(math.floor(duration / interval)) + 1
        count_i = int(util.clamp(count_i, 1, max_count))
        interval = max(interval, 1 / 30.0)
        if delay and float(delay) > 0:
            await asyncio.sleep(float(util.clamp(float(delay), 0, 10)))

        notes: list[str] = []
        frames: list[tuple["Image.Image", float]] = []
        method = "screencap-sparse"
        try:
            if interval >= 0.8:
                frames, method = await self._sample_screencaps(count_i, interval)
            else:
                frames, method = await self._sample_screenrecord(duration, interval, count_i)
        except Exception as exc:
            notes.append(f"录制抽帧失败，改用连续截图：{util.truncate(repr(exc), 120)}")
            frames, method = await self._sample_screencaps(count_i, max(interval, 0.15))
        if not frames:
            raise AdbError("没有采到任何帧")
        screen_size = frames[0][0].size

        # 裁剪 / 缩放 / 打时间戳标签，同时做连续相同帧合并
        prepared: list[tuple["Image.Image", float, int, int]] = []  # (img, ts, count, start_idx)
        for idx, (img, ts) in enumerate(frames):
            if region:
                try:
                    x, y, w, h = self._parse_region(region, img.width, img.height)
                    img = img.crop((x, y, x + w, y + h))
                except Exception:
                    notes.append(f"region 解析失败，已忽略：{region!r}")
            if prepared and dedupe and self._similar(prepared[-1][0], img, threshold=0.006):
                prev_img, prev_ts, prev_n, prev_start = prepared[-1]
                prepared[-1] = (prev_img, prev_ts, prev_n + 1, prev_start)
                continue
            prepared.append((img, ts, 1, idx + 1))

        total_sampled = len(frames)
        groups = prepared
        if dedupe and len(groups) < total_sampled:
            notes.append(f"连续相同/近似帧已合并：{total_sampled} 帧 → {len(groups)} 帧有效画面。")

        image_width = int(width or (self._cfg("frames_sheet_tile_width", 480) if mode == "sheet" else self._cfg("frames_image_width", 768)))
        image_width = int(util.clamp(image_width, 200, 1600))
        images: list[SampleImage] = []
        if mode == "frames":
            selected = self._pick_evenly(groups, max_images)
            if len(groups) > len(selected):
                notes.append(
                    f"共 {len(groups)} 帧，按 max_images={max_images} 等间隔取了 {len(selected)} 帧；"
                    "想全部看请提高 max_images 或减小 count/duration。"
                )
            for k, (img, ts, rep, start_idx) in enumerate(selected, start=1):
                label = f"#{start_idx} t={ts:.2f}s" + (f" ×{rep}同帧" if rep > 1 else "")
                annotated = self._prepare_sample_frame(
                    img, image_width, label, annotate=bool(annotate)
                )
                png, path, mime = self._save_sample_image(annotated, "frame", k)
                images.append(
                    SampleImage(
                        png=png,
                        path=path,
                        label=label,
                        frame_start=start_idx,
                        frame_end=start_idx,
                        width=annotated.width,
                        height=annotated.height,
                        mime=mime,
                    )
                )
        else:
            per = int(per_image or max(2, cols * 2))
            per = int(util.clamp(per, 1, 32))
            chunks = [groups[i : i + per] for i in range(0, len(groups), per)]
            if len(chunks) > max_images:
                notes.append(
                    f"图版需要 {len(chunks)} 张，超过 max_images={max_images}，只返回前 {max_images} 张；"
                    "可减小 count 或提高 max_images。"
                )
                chunks = chunks[:max_images]
            for k, chunk in enumerate(chunks, start=1):
                sheet = self._compose_sample_sheet(
                    chunk, cols=cols, tile_width=image_width, annotate=bool(annotate)
                )
                start_idx = chunk[0][3]
                end_idx = chunk[-1][3]
                label = (
                    f"图版 {k}：帧 #{start_idx}~#{end_idx}"
                    f"（t={chunk[0][1]:.2f}s~{chunk[-1][1]:.2f}s）"
                )
                png, path, mime = self._save_sample_image(sheet, "sheet", k)
                images.append(
                    SampleImage(
                        png=png,
                        path=path,
                        label=label,
                        frame_start=start_idx,
                        frame_end=end_idx,
                        width=sheet.width,
                        height=sheet.height,
                        mime=mime,
                    )
                )

        sample_times = [ts for _, ts, _, _ in groups]
        return SampleResult(
            images=images,
            frames_total=len(groups),
            sample_times=sample_times,
            method=method,
            interval=interval,
            duration=duration,
            mode=mode,
            screen_size=screen_size,
            notes=notes,
        )

    async def _sample_screencaps(
        self, count: int, interval: float
    ) -> tuple[list[tuple["Image.Image", float]], str]:
        """稀疏采样：直接连续 screencap，时间点准、开销小（不用录视频/解码）。"""
        if Image is None:
            return [], "screencap-sparse"
        frames: list[tuple["Image.Image", float]] = []
        start = time.time()
        for i in range(count):
            try:
                png = await self.adb.screencap_png()
                img = Image.open(io.BytesIO(png)).convert("RGB")
                frames.append((img, i * interval))
            except Exception:
                break
            if i < count - 1:
                next_at = start + (i + 1) * interval
                wait = next_at - time.time()
                if wait > 0:
                    await asyncio.sleep(wait)
        return frames, "screencap-sparse"

    async def _sample_screenrecord(
        self, duration: float, interval: float, count: int
    ) -> tuple[list[tuple["Image.Image", float]], str]:
        """密集采样：screenrecord 录像 -> 拉回 -> ffmpeg 按采样间隔抽帧。"""
        if Image is None:
            return [], "screenrecord+ffmpeg"
        device_path = "/sdcard/mumu_vm_sample.mp4"
        await self.adb.shell(f"rm -f {device_path}", timeout=10)
        limit = int(util.clamp(math.ceil(duration) + 1, 1, 60))
        await self.adb.screen_record(device_path, time_limit=limit)
        local = self.shot_dir / "_sample.mp4"
        if not await self.adb.pull(device_path, local):
            raise AdbError("拉取录屏文件失败")
        frames: list[tuple["Image.Image", float]] = []
        if not self.ffmpeg:
            raise AdbError("没找到 ffmpeg，无法从录屏抽帧")
        frames_dir = self.shot_dir / "_sample_frames"
        util.ensure_dir(frames_dir)
        for old in frames_dir.glob("*.jpg"):
            try:
                old.unlink()
            except Exception:
                pass
        fps_expr = 1.0 / max(interval, 1 / 30.0)
        args = [
            self.ffmpeg, "-hide_banner", "-loglevel", "error",
            "-i", str(local),
            "-t", f"{max(0.2, duration):.2f}",
            "-vf", f"fps={fps_expr:.6f},scale='min(960,iw)':-2",
            "-vsync", "0", "-q:v", "2", "-frames:v", str(count),
            str(frames_dir / "f_%03d.jpg"),
        ]
        await util.run_process(args, timeout=120)
        files = sorted(frames_dir.glob("f_*.jpg"))
        for i, path in enumerate(files[:count]):
            try:
                img = Image.open(path).convert("RGB")
            except Exception:
                continue
            frames.append((img, i * interval))
        if len(frames) < count and frames:
            # 静止画面/帧率不足：补重复帧，保证时间轴数量正确
            last = frames[-1][0].copy()
            start_i = len(frames)
            for i in range(start_i, count):
                frames.append((last.copy(), i * interval))
        return frames, "screenrecord+ffmpeg"

    @staticmethod
    def _pick_evenly(items: list[Any], n: int) -> list[Any]:
        if n <= 0 or len(items) <= n:
            return items
        if n == 1:
            return [items[0]]
        step = (len(items) - 1) / (n - 1)
        return [items[round(i * step)] for i in range(n)]

    def _prepare_sample_frame(
        self, img: "Image.Image", width: int, label: str, annotate: bool = True
    ) -> "Image.Image":
        img = img.copy()
        if width > 0 and img.width > width:
            ratio = width / img.width
            img = img.resize((width, max(1, int(img.height * ratio))), Image.LANCZOS)
        if annotate:
            img = self._draw_label_bar(img, label)
        return img

    def _compose_sample_sheet(
        self,
        chunk: list[tuple["Image.Image", float, int, int]],
        *,
        cols: int,
        tile_width: int,
        annotate: bool,
    ) -> "Image.Image":
        tiles: list[tuple["Image.Image", str]] = []
        for img, ts, rep, start_idx in chunk:
            label = f"#{start_idx} {ts:.2f}s" + (f" ×{rep}同帧" if rep > 1 else "")
            tile = self._prepare_sample_frame(img, tile_width, label, annotate=False)
            tiles.append((tile, label))
        label_h = 22 if annotate else 0
        cols = max(1, min(cols, len(tiles)))
        rows = math.ceil(len(tiles) / cols)
        tile_w = max(t.width for t, _ in tiles)
        tile_h = max(t.height for t, _ in tiles)
        pad = 6
        canvas = Image.new(
            "RGB",
            (cols * (tile_w + pad) + pad, rows * (tile_h + label_h + pad) + pad),
            (18, 18, 18),
        )
        draw = ImageDraw.Draw(canvas)
        font = self._label_font()
        for i, (tile, label) in enumerate(tiles):
            x = pad + (i % cols) * (tile_w + pad)
            y = pad + (i // cols) * (tile_h + label_h + pad)
            canvas.paste(tile, (x, y))
            if label_h:
                draw.text((x + 3, y + tile_h + 3), label, fill=(255, 214, 0), font=font)
        return canvas

    def _draw_label_bar(self, img: "Image.Image", label: str) -> "Image.Image":
        try:
            img = img.copy()
            draw = ImageDraw.Draw(img)
            font = self._label_font()
            w, h = img.size
            draw.rectangle([(0, 0), (w, 24)], fill=(0, 0, 0))
            draw.text((6, 4), label, fill=(255, 235, 0), font=font)
            return img
        except Exception:
            return img

    def _save_sample_image(
        self, img: "Image.Image", kind: str, index: int
    ) -> tuple[bytes, Path, str]:
        """采样图统一用 JPEG（体积小、文字仍清晰），避免一次多图把请求撑爆。"""
        out = io.BytesIO()
        try:
            img.convert("RGB").save(out, format="JPEG", quality=88, optimize=True)
            mime, suffix = "image/jpeg", ".jpg"
        except Exception:
            out = io.BytesIO()
            img.save(out, format="PNG", optimize=True)
            mime, suffix = "image/png", ".png"
        data = out.getvalue()
        path = (
            self.shot_dir
            / f"sample_{kind}_{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time()*1000)%1000:03d}_{index:02d}{suffix}"
        )
        path.write_bytes(data)
        util.cleanup_dir(self.shot_dir, keep=300)
        return data, path, mime

    @staticmethod
    def _fmt_sample_ts(ts: float) -> str:
        m, s = divmod(max(0.0, float(ts)), 60)
        return f"{int(m)}:{s:05.2f}"

    # ------------------------------------------------------------------ 辅助
    def _parse_region(
        self, region: str, full_w: int, full_h: int
    ) -> tuple[int, int, int, int]:
        """region 支持 "x,y,w,h" 或 "x1,y1,x2,y2"。"""
        parts = [p.strip() for p in str(region).replace(" ", ",").split(",") if p.strip()]
        if len(parts) != 4:
            raise ValueError("region 需要 4 个数字：x,y,w,h 或 x1,y1,x2,y2")
        x, y, a, b = (int(float(p)) for p in parts)
        # 若 a/b 看起来是右/下边界（大于 x/y），按边界处理
        if a > x and b > y:
            w, h = a - x, b - y
        else:
            w, h = a, b
        x = int(util.clamp(x, 0, max(0, full_w - 1)))
        y = int(util.clamp(y, 0, max(0, full_h - 1)))
        w = int(util.clamp(w, 1, full_w - x))
        h = int(util.clamp(h, 1, full_h - y))
        return x, y, w, h

    def _label_font(self):
        if ImageFont is None:
            return None
        try:
            return ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 15)
        except Exception:
            try:
                return ImageFont.load_default()
            except Exception:
                return None

    def _annotate(self, img: "Image.Image") -> "Image.Image":
        """在单帧上叠加淡坐标网格，方便 Agent 用坐标点击。"""
        try:
            img = img.copy()
            draw = ImageDraw.Draw(img)
            step = 120
            color = (90, 90, 90)
            font = self._label_font()
            for x in range(step, img.width, step):
                draw.line([(x, 0), (x, img.height)], fill=color, width=1)
                draw.text((x + 2, 2), str(x), fill=(255, 230, 0), font=font)
            for y in range(step, img.height, step):
                draw.line([(0, y), (img.width, y)], fill=color, width=1)
                draw.text((2, y + 1), str(y), fill=(255, 230, 0), font=font)
            return img
        except Exception:
            return img
