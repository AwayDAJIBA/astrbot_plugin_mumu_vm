# -*- coding: utf-8 -*-
"""按画面变化量挑帧（参考 dsh-see-now 的思路，独立实现）。

手机动态画面不能只按固定间隔抽帧：静止时全是重复图、快速变化时又漏帧。
这里对连续截图算一个 0–255 的 HSV 变化分，再做两类筛选：

- 切点：这一帧分数突然跳高，且明显高于前后帧（翻页/切场景）；
- 积分：分数不高但持续变化，累计到阈值后取一帧（视频/滑动/游戏）。

最终只把"真的变了"的帧交给模型，并尽量拼成一张图。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None  # type: ignore


def _bilinear():
    try:
        return Image.Resampling.BILINEAR
    except Exception:  # pragma: no cover - 老 Pillow 兼容
        return Image.BILINEAR


SCAN_W = 64
SCAN_H = 36


def hsv_grid(img: "Image.Image") -> list[tuple[int, int, int]]:
    """缩到 64×36 的 HSV 网格；PIL 的 HSV 三通道都是 0–255。"""
    small = img.resize((SCAN_W, SCAN_H), _bilinear()).convert("HSV")
    return list(small.getdata())


def score_pair(prev: list[tuple[int, int, int]], cur: list[tuple[int, int, int]]) -> float:
    """两帧 HSV 加权绝对差，0–255；色相按环形处理，低饱和时色相降权。"""
    total = 0.0
    for (h1, s1, v1), (h2, s2, v2) in zip(prev, cur):
        dh = abs(h1 - h2)
        hue_diff = min(dh, 255 - dh)
        hue_weight = min(s1, s2) / 255.0
        total += (hue_diff * hue_weight + abs(s1 - s2) + abs(v1 - v2)) / 3.0
    return total / max(1, len(cur))


@dataclass
class MotionPick:
    index: int
    score: float
    reason: str  # start / cut / motion


@dataclass
class MotionSelection:
    picks: list[MotionPick] = field(default_factory=list)
    scores: list[float | None] = field(default_factory=list)
    raw_count: int = 0
    static: bool = False
    cuts: int = 0
    motion_marks: int = 0
    peak: float = 0.0
    weak: bool = False

    @property
    def indices(self) -> list[int]:
        return [item.index for item in self.picks]


def select_motion_frames(
    frames: list[tuple[Any, float]],
    *,
    cut_abs: float = 12.0,
    cut_spike: float = 2.5,
    cut_min_gap: float = 0.4,
    integral_seconds: float = 2.0,
    ref_score: float = 4.0,
    mark_min_gap: float = 0.5,
    dup_min: float = 8.0,
    max_picks: int = 10,
) -> MotionSelection:
    """从带时间戳的连续帧里挑出值得看的帧。"""
    result = MotionSelection(raw_count=len(frames))
    if not frames:
        return result
    if len(frames) == 1:
        result.picks.append(MotionPick(0, 0.0, "start"))
        result.scores = [None]
        return result

    hsvs = [hsv_grid(img) for img, _ in frames]
    scores: list[float | None] = [None]
    for i in range(1, len(frames)):
        scores.append(score_pair(hsvs[i - 1], hsvs[i]))
    result.scores = scores

    times = [float(ts) for _, ts in frames]
    start_t = times[0]
    actual_fps = (len(frames) - 1) / max(1e-6, times[-1] - start_t)
    # 积分阈值：中等变化跑 integral_seconds 才出一个采样点；太快就密集一点，太静就永远攒不够
    need = max(8.0, float(ref_score) * max(1.0, actual_fps) * float(integral_seconds))

    picks: list[MotionPick] = [MotionPick(0, 0.0, "start")]
    last_pick_t = start_t
    last_pick_hsv = hsvs[0]
    acc = 0.0
    cuts = 0
    marks = 0

    for i in range(1, len(frames)):
        score = scores[i] or 0.0
        prev_score = scores[i - 1] if i - 1 >= 0 else None
        next_score = scores[i + 1] if i + 1 < len(scores) else None
        base = max(1.0, prev_score or 0.0, next_score or 0.0)
        spike = score >= cut_abs and score > cut_spike * base
        # 运动中的硬切：前后都在动，spike 未必成立，但分数相对邻居有明显台阶
        jump = score - max(prev_score or 0.0, next_score or 0.0)
        step = score >= cut_abs and jump >= max(10.0, score * 0.35)
        is_cut = spike or step

        gap = times[i] - last_pick_t
        # 和上一张留下来的帧太像：跳过，但保留积分（不能白攒）
        dup = score_pair(last_pick_hsv, hsvs[i])
        if gap >= cut_min_gap and is_cut and len(picks) < max_picks and dup >= dup_min:
            picks.append(MotionPick(i, score, "cut"))
            last_pick_t = times[i]
            last_pick_hsv = hsvs[i]
            acc = 0.0
            cuts += 1
            continue

        acc += score
        if acc >= need and gap >= mark_min_gap and len(picks) < max_picks:
            if dup >= dup_min:
                picks.append(MotionPick(i, score, "motion"))
                last_pick_t = times[i]
                last_pick_hsv = hsvs[i]
                marks += 1
                acc = 0.0
            # dup < dup_min 时不选，但 acc 继续保留

    result.picks = picks
    result.cuts = cuts
    result.motion_marks = marks
    result.peak = max((s or 0.0) for s in scores) if scores else 0.0
    # 只有开头一帧：真正静止时只回一帧；但如果分数虽低却明显非零（慢速动画/
    # 小视频窗口），补一帧末帧，别让模型以为完全没动。
    if len(picks) == 1 and len(frames) >= 3:
        if result.peak < 0.8:
            result.static = True
        elif result.peak < max(3.0, cut_abs * 0.5):
            last = len(frames) - 1
            if last > 0 and len(picks) < max_picks:
                picks.append(MotionPick(last, scores[last] or 0.0, "weak"))
                result.weak = True
    return result
