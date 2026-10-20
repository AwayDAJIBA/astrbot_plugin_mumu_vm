# -*- coding: utf-8 -*-
"""插件持久状态：模式、待处理消息、日记、每日预算、定时任务。"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import util


@dataclass
class PendingMessage:
    id: str
    ts: float
    umo: str
    sender_id: str
    sender_name: str
    text: str
    platform: str = ""
    handled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "umo": self.umo,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "text": self.text,
            "platform": self.platform,
            "handled": self.handled,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingMessage":
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:8]),
            ts=float(data.get("ts") or time.time()),
            umo=str(data.get("umo") or ""),
            sender_id=str(data.get("sender_id") or ""),
            sender_name=str(data.get("sender_name") or ""),
            text=str(data.get("text") or ""),
            platform=str(data.get("platform") or ""),
            handled=bool(data.get("handled")),
        )

    def brief(self) -> str:
        who = self.sender_name or self.sender_id or "某人"
        return f"[#{self.id}] {who}: {util.truncate(self.text, 160)}"


@dataclass
class ScheduledPlay:
    id: str
    run_at: float
    minutes: float
    goal: str
    created_at: float = field(default_factory=time.time)
    done: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_at": self.run_at,
            "minutes": self.minutes,
            "goal": self.goal,
            "created_at": self.created_at,
            "done": self.done,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduledPlay":
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:6]),
            run_at=float(data.get("run_at") or 0),
            minutes=float(data.get("minutes") or 15),
            goal=str(data.get("goal") or ""),
            created_at=float(data.get("created_at") or time.time()),
            done=bool(data.get("done")),
        )


class SessionState:
    """插件全局状态（单实例，线程/协程内用锁保护写操作）。"""

    def __init__(self, data_dir: Path, logger=None):
        self.data_dir = data_dir
        self.path = data_dir / "state.json"
        self.logger = logger
        self._lock = asyncio.Lock()
        data = util.read_json(self.path, {}) or {}
        # 所有者（最近一次与机器人对话的会长/管理员会话）
        self.owner_umo: str = str(data.get("owner_umo") or "")
        self.owner_name: str = str(data.get("owner_name") or "")
        self.owner_sender_id: str = str(data.get("owner_sender_id") or "")
        # 模式
        mode = data.get("mode") or {}
        self.active: bool = bool(mode.get("active"))
        self.goal: str = str(mode.get("goal") or "")
        self.source: str = str(mode.get("source") or "")
        self.started_ts: float = float(mode.get("started_ts") or 0)
        self.deadline_ts: float = float(mode.get("deadline_ts") or 0)
        self.auto_continue: bool = bool(mode.get("auto_continue", True))
        self.last_tool_ts: float = float(mode.get("last_tool_ts") or 0)
        self.last_action: str = str(mode.get("last_action") or "")
        self.autoplay_running: bool = False  # 仅运行时使用，不持久化
        self.autoplay_task: Any = None
        # 消息/日记
        self.pending: list[PendingMessage] = [
            PendingMessage.from_dict(item) for item in (data.get("pending") or [])
        ]
        self.diary: list[dict[str, Any]] = list(data.get("diary") or [])[-80:]
        # 每日统计
        today = time.strftime("%Y-%m-%d")
        daily = data.get("daily") or {}
        if daily.get("date") != today:
            daily = {"date": today, "autoplay_minutes": 0.0, "autoplay_runs": 0}
        self.daily: dict[str, Any] = daily
        # 定时任务
        self.scheduled: list[ScheduledPlay] = [
            ScheduledPlay.from_dict(item) for item in (data.get("scheduled") or [])
        ]
        # 其它
        self.last_user_msg_ts: float = float(data.get("last_user_msg_ts") or 0)
        self.last_autoplay_goal_index: int = int(data.get("last_autoplay_goal_index") or 0)
        self.total_shots: int = int(data.get("total_shots") or 0)
        self.total_actions: int = int(data.get("total_actions") or 0)
        self.last_hint_ts: dict[str, float] = dict(data.get("last_hint_ts") or {})
        # 最近操作（给 mumu_phone_recent 用；最多保留 200 条）
        self.activity: list[dict[str, Any]] = list(data.get("activity") or [])[-200:]
        # 本次会话由 Agent 主动记的"操作教程/经验"（收手时单独写长期记忆）
        self.session_lessons: list[dict[str, Any]] = list(
            data.get("session_lessons") or []
        )[-30:]

    # ------------------------------------------------------------------ 持久化
    def snapshot(self) -> dict[str, Any]:
        return {
            "owner_umo": self.owner_umo,
            "owner_name": self.owner_name,
            "owner_sender_id": self.owner_sender_id,
            "mode": {
                "active": self.active,
                "goal": self.goal,
                "source": self.source,
                "started_ts": self.started_ts,
                "deadline_ts": self.deadline_ts,
                "auto_continue": self.auto_continue,
                "last_tool_ts": self.last_tool_ts,
                "last_action": self.last_action,
            },
            "pending": [item.to_dict() for item in self.pending[-30:]],
            "diary": self.diary[-80:],
            "daily": self.daily,
            "scheduled": [item.to_dict() for item in self.scheduled[-30:]],
            "last_user_msg_ts": self.last_user_msg_ts,
            "last_autoplay_goal_index": self.last_autoplay_goal_index,
            "total_shots": self.total_shots,
            "total_actions": self.total_actions,
            "last_hint_ts": self.last_hint_ts,
            "activity": self.activity[-200:],
            "session_lessons": self.session_lessons[-30:],
        }

    def save(self) -> None:
        util.write_json(self.path, self.snapshot())

    async def asave(self) -> None:
        async with self._lock:
            self.save()

    # ------------------------------------------------------------------ 模式
    def is_active(self) -> bool:
        return self.active

    def begin(self, goal: str, source: str, minutes: float, auto_continue: bool = True) -> None:
        now = time.time()
        self.active = True
        self.goal = goal or ""
        self.source = source
        self.started_ts = now
        self.deadline_ts = now + max(1.0, minutes) * 60.0 if minutes else 0.0
        self.auto_continue = auto_continue
        self.last_tool_ts = now
        self.last_action = ""
        self.session_lessons = []
        self.save()

    def end(self) -> None:
        self.active = False
        self.goal = ""
        self.source = ""
        self.started_ts = 0
        self.deadline_ts = 0
        self.auto_continue = True
        self.autoplay_running = False
        self.save()

    def remaining_sec(self) -> float:
        if not self.deadline_ts:
            return float("inf")
        return max(0.0, self.deadline_ts - time.time())

    def remaining_text(self) -> str:
        remain = self.remaining_sec()
        if remain == float("inf"):
            return "不限时"
        return util.hms(remain)

    def touch_tool(self, action: str = "") -> None:
        self.last_tool_ts = time.time()
        if action:
            self.last_action = action
        self.total_actions += 1

    def add_activity(self, tool: str, detail: str = "") -> None:
        """记录一次手机操作，供「最近做了什么」工具查询。"""
        self.activity.append(
            {
                "ts": time.time(),
                "time": util.now_str("%m-%d %H:%M:%S"),
                "tool": str(tool or ""),
                "detail": util.truncate(str(detail or ""), 200),
            }
        )
        self.activity = self.activity[-200:]
        self.save()

    def recent_activity(self, minutes: float = 60, limit: int = 30) -> list[dict[str, Any]]:
        cutoff = time.time() - max(60.0, float(minutes) * 60.0)
        items = [item for item in self.activity if float(item.get("ts") or 0) >= cutoff]
        return items[-max(1, int(limit)):]

    # ------------------------------------------------------------------ 操作教程/经验
    def add_lesson(self, situation: str, action: str) -> None:
        self.session_lessons.append(
            {
                "ts": time.time(),
                "time": util.now_str("%H:%M"),
                "situation": util.truncate(str(situation or ""), 300),
                "action": util.truncate(str(action or ""), 500),
            }
        )
        self.session_lessons = self.session_lessons[-30:]
        self.save()

    def take_lessons(self) -> list[dict[str, Any]]:
        lessons = list(self.session_lessons)
        self.session_lessons = []
        self.save()
        return lessons

    # ------------------------------------------------------------------ 消息
    def add_pending(
        self,
        *,
        umo: str,
        sender_id: str,
        sender_name: str,
        text: str,
        platform: str = "",
    ) -> PendingMessage:
        item = PendingMessage(
            id=uuid.uuid4().hex[:8],
            ts=time.time(),
            umo=umo,
            sender_id=sender_id,
            sender_name=sender_name,
            text=text,
            platform=platform,
        )
        self.pending.append(item)
        if len(self.pending) > 30:
            self.pending = self.pending[-30:]
        self.save()
        return item

    def unhandled_pending(self) -> list[PendingMessage]:
        # 超过 3 小时的消息自动视为过时，避免下次开手机时突然被一堆旧消息打扰
        cutoff = time.time() - 3 * 3600
        return [m for m in self.pending if not m.handled and m.ts >= cutoff][-10:]

    def mark_handled(self, msg_id: str) -> PendingMessage | None:
        for item in self.pending:
            if item.id == msg_id:
                item.handled = True
                self.save()
                return item
        return None

    def pending_notice_text(self) -> str:
        items = self.unhandled_pending()
        if not items:
            return ""
        lines = [f"📨 手机操作期间有 {len(items)} 条新消息（你自己决定是否回复）："]
        lines += [item.brief() for item in items]
        lines.append("查看/回复：mumu_phone_messages；想回就用 mumu_phone_say(msg_id=..., text=...)。")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 日记
    def add_diary(self, text: str, kind: str = "note") -> None:
        self.diary.append(
            {
                "ts": time.time(),
                "time": util.now_str("%H:%M"),
                "kind": kind,
                "text": str(text or ""),
            }
        )
        self.diary = self.diary[-80:]
        self.save()

    def diary_text(self, limit: int = 6) -> str:
        entries = self.diary[-limit:]
        if not entries:
            return "（暂无）"
        return "\n".join(f"{e.get('time', '')} [{e.get('kind', 'note')}] {e.get('text', '')}" for e in entries)

    # ------------------------------------------------------------------ 每日预算
    def reset_daily_if_needed(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if self.daily.get("date") != today:
            self.daily = {"date": today, "autoplay_minutes": 0.0, "autoplay_runs": 0}
            self.save()

    def daily_minutes(self) -> float:
        self.reset_daily_if_needed()
        return float(self.daily.get("autoplay_minutes") or 0)

    def daily_runs(self) -> int:
        self.reset_daily_if_needed()
        return int(self.daily.get("autoplay_runs") or 0)

    def add_play_minutes(self, minutes: float) -> None:
        self.reset_daily_if_needed()
        self.daily["autoplay_minutes"] = round(
            float(self.daily.get("autoplay_minutes") or 0) + max(0.0, minutes), 2
        )
        self.daily["autoplay_runs"] = int(self.daily.get("autoplay_runs") or 0) + 1
        self.save()

    # ------------------------------------------------------------------ 定时
    def add_scheduled(self, run_at: float, minutes: float, goal: str) -> ScheduledPlay:
        item = ScheduledPlay(
            id=uuid.uuid4().hex[:6], run_at=run_at, minutes=minutes, goal=goal
        )
        self.scheduled.append(item)
        self.scheduled = self.scheduled[-20:]
        self.save()
        return item

    def due_scheduled(self) -> list[ScheduledPlay]:
        now = time.time()
        due = []
        for item in self.scheduled:
            if not item.done and item.run_at <= now and now - item.run_at < 600:
                due.append(item)
        return due

    def next_scheduled_text(self) -> str:
        upcoming = [item for item in self.scheduled if not item.done and item.run_at > time.time()]
        if not upcoming:
            return "无"
        upcoming.sort(key=lambda x: x.run_at)
        item = upcoming[0]
        return f"{time.strftime('%m-%d %H:%M', time.localtime(item.run_at))} 起 {item.minutes:g} 分钟：{util.truncate(item.goal, 60)}"
