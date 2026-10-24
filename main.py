# -*- coding: utf-8 -*-
"""MuMu 安卓虚拟机插件（AstrBot 4.x）

让 AstrBot 的 Agent 独占操控 MuMu 模拟器里的安卓手机：
- 模拟器窗口隐藏，桌面画面只给 Agent 看；
- 单帧截图 / 10fps 多帧拼图、UI 树点击、中文输入、应用与系统控制；
- "玩手机"模式下自动精简工具与上下文，提速；
- 被消息打扰时只回"正在玩手机"，把消息交给 Agent 决定是否回复；
- 支持空闲/定时自主玩手机、随时收手、每日预算。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from astrbot.api.event import filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.agent.tool import ToolSet
try:
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover
    TextPart = None  # type: ignore
from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.message.components import Image, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from .vm.autoplay import AutoplayEngine
from .vm.media import PhoneMedia
from .vm.phone import Phone
from .vm.state import SessionState
from .vm import util

PLUGIN_NAME = "astrbot_plugin_mumu_vm"
AUTHOR = "AwayDAJIBA"
DESC = (
    "让 Agent 独占操控 MuMu 模拟器里的安卓手机（窗口隐藏、单帧截图/连续采样多帧看图、"
    "UI 点击、中文输入、应用/系统控制、玩手机模式精简提速、被打扰时只回“正在玩手机”、"
    "空闲/定时自主玩手机与随时收手）。"
)
VERSION = "v1.5.0"

# 精简模式（普通聊天）下暴露的工具：让 Agent 知道有手机可用，又不占太多上下文
COMPACT_TOOLS = {
    "mumu_phone_status",
    "mumu_phone_play",
    "mumu_phone_autoplay",
    "mumu_phone_recent",
}
# 只有进入手机模式才注入的工具
FULL_ONLY_TOOLS = {
    "mumu_phone_screen",
    "mumu_phone_frames",
    "mumu_phone_ui",
    "mumu_phone_tap",
    "mumu_phone_swipe",
    "mumu_phone_text",
    "mumu_phone_key",
    "mumu_phone_app",
    "mumu_phone_system",
    "mumu_phone_notifications",
    "mumu_phone_shell",
    "mumu_phone_messages",
    "mumu_phone_say",
    "mumu_phone_diary",
    "mumu_phone_lesson",
    "mumu_phone_exit",
    "mumu_phone_media_status",
    "mumu_phone_record_start",
    "mumu_phone_record_stop",
    "mumu_phone_hear_start",
    "mumu_phone_hear_stop",
}
FULL_TOOL_NAMES = COMPACT_TOOLS | FULL_ONLY_TOOLS
# 媒体辅助工具（受 media_assist_enable 总开关控制）
MEDIA_TOOL_NAMES = {
    "mumu_phone_media_status",
    "mumu_phone_record_start",
    "mumu_phone_record_stop",
    "mumu_phone_hear_start",
    "mumu_phone_hear_stop",
}
# 自主玩手机时给的手机工具（含 mumu_phone_exit：Agent 想收手时必须能退出来）
AUTOPLAY_TOOL_NAMES = FULL_TOOL_NAMES - {"mumu_phone_play", "mumu_phone_autoplay"}


class MumuVmPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config: dict = config or {}
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.shot_dir = Path(get_astrbot_temp_path()) / "mumu_vm" / "shots"
        self.state = SessionState(self.data_dir, logger=self.logger)
        self.phone = Phone(
            config=self.config,
            data_dir=self.data_dir,
            shot_dir=self.shot_dir,
            logger=self.logger,
            assets_dir=Path(__file__).parent / "assets",
        )
        self.autoplay = AutoplayEngine(self)
        self.media = PhoneMedia(self)
        self._bg_task: asyncio.Task | None = None
        # 当前正在跑的普通 Agent 请求（umo -> ProviderRequest）。
        # click 进入/退出手机模式时动态换工具组，让同一轮回答可以继续做别的事。
        self._active_reqs: dict[str, ProviderRequest] = {}
        self._req_snapshots: dict[str, dict[str, Any]] = {}
        self._startup_ts = time.time()
        self._entry_lock = asyncio.Lock()
        self._agent_busy = False
        self._kick_task: asyncio.Task | None = None
        self._iris_ctx_cache: tuple[float, str] = (0.0, "")
        self._iris_ctx_lock = asyncio.Lock()
        self._last_skip_diag = 0.0

    # ------------------------------------------------------------------ 生命周期
    def _is_current(self) -> bool:
        """是否是当前生效的插件实例。

        AstrBot 在重载/启动过程中可能创建过多个插件实例，旧实例的后台循环如果
        没被及时清掉，会继续处理消息并把旧的内存取证写回 state.json（表现为
        "测试状态总被覆盖"）。这里用 Context 上的共享标记保证只有最新实例干活。
        """
        try:
            return getattr(self.context, "_mumu_vm_active_instance", None) is self
        except Exception:
            return True

    async def initialize(self) -> None:
        try:
            setattr(self.context, "_mumu_vm_active_instance", self)
        except Exception:
            pass
        self._diag(
            f"initialize 插件启动 version={VERSION} instance={id(self)} "
            f"manager={self.phone.mumu.manager_path} adb={self.phone.mumu.adb_path} "
            f"ffmpeg={util.guess_ffmpeg(str(self._cfg('ffmpeg_path', '')))} "
            f"owner={self.state.owner_umo}"
        )
        self.logger.info(
            f"MuMu 安卓虚拟机插件启动；管理员={self.state.owner_name or '未知'} "
            f"Manager={self.phone.mumu.manager_path} ADB={self.phone.mumu.adb_path} "
            f"FFmpeg={util.guess_ffmpeg(str(self._cfg('ffmpeg_path', ''))) or '未找到'}"
        )
        # 上次异常退出时残留的模式，直接清掉并关掉模拟器，避免幽灵状态
        if self.state.active:
            self.logger.warning("检测到上次未正常退出的手机模式，正在清理并关闭模拟器…")
            try:
                await self.phone.stop()
            except Exception:
                pass
            self.state.end()
        # 上次异常退出可能残留"玩手机时收到的消息"，开机时清掉，避免下一局看到旧消息
        if self.state.pending:
            self._diag(f"清理上次残留的待处理消息 {len(self.state.pending)} 条")
            self.state.pending.clear()
            self.state.last_hint_ts.clear()
            self.state.save()
        if self.state.session_lessons:
            self._diag(f"清理上次残留的操作经验 {len(self.state.session_lessons)} 条")
            self.state.session_lessons.clear()
            self.state.save()
        self._bg_task = asyncio.create_task(self._background_loop(), name="mumu-vm-scheduler")

    async def terminate(self) -> None:
        try:
            if self._bg_task:
                self._bg_task.cancel()
                try:
                    await self._bg_task
                except (asyncio.CancelledError, Exception):
                    pass
            await self.autoplay.stop(reason="插件卸载", notify=False)
            try:
                await self.media.abort_all()
            except Exception:
                pass
            if self.state.active and bool(self._cfg("shutdown_on_exit", True)):
                try:
                    await self.phone.stop()
                except Exception:
                    pass
            self.state.autoplay_running = False
            # 状态在每次变更时已持久化；卸载时不再写盘，避免把内存里的旧快照覆盖回磁盘
            try:
                if getattr(self.context, "_mumu_vm_active_instance", None) is self:
                    setattr(self.context, "_mumu_vm_active_instance", None)
            except Exception:
                pass
        except Exception as exc:
            self.logger.warning(f"插件退出清理异常: {exc!r}")

    # ------------------------------------------------------------------ 配置
    def _diag(self, message: str) -> None:
        """写一行诊断日志到插件数据目录（AstrBot 控制台日志不可见时排障用）。"""
        try:
            path = self.data_dir / "diag.log"
            with path.open("a", encoding="utf-8") as f:
                f.write(f"{util.now_str()} {message}\n")
            if path.stat().st_size > 512 * 1024:
                path.write_text(path.read_text(encoding="utf-8")[-100000:], encoding="utf-8")
        except Exception:
            pass

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        if value is None or value == "":
            return default
        return value

    @property
    def _enabled(self) -> bool:
        return bool(self._cfg("enabled", True))

    def _keywords(self, key: str, default: str) -> list[str]:
        raw = str(self._cfg(key, default) or "")
        return [item.strip() for item in raw.replace("，", ",").split(",") if item.strip()]

    # ------------------------------------------------------------------ 后台调度
    async def _background_loop(self) -> None:
        await asyncio.sleep(8)
        while True:
            if not self._is_current():
                self._diag("检测到自己是旧实例，后台调度退出")
                return
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.error(f"后台调度异常: {exc!r}", exc_info=True)
            await asyncio.sleep(20)

    async def _tick(self) -> None:
        if not self._enabled or not self._is_current():
            return
        self.state.reset_daily_if_needed()
        now = time.time()
        # 1) 模式超时自动收手
        if self.state.active:
            if self.state.deadline_ts and now > self.state.deadline_ts + 15:
                self.logger.info("手机模式到时间了，自动收手。")
                await self.finish_play_session(reason="时间到（自动）", notify=True)
                return
            if not self.autoplay.running:
                idle = now - max(self.state.last_tool_ts, self.state.last_user_msg_ts)
                idle_limit = float(self._cfg("mode_idle_timeout_sec", 180))
                if idle > idle_limit:
                    self.logger.info(f"手机模式空闲 {int(idle)}s，自动收手。")
                    await self.finish_play_session(reason="空闲自动收手", notify=True)
                    return
            return
        # 2) 定时任务
        for item in self.state.due_scheduled():
            if self.state.active or self.autoplay.running:
                break
            item.done = True
            self.state.save()
            self.logger.info(f"定时玩手机任务触发: {item.goal!r} {item.minutes}min")
            await self.start_play_session(
                goal=item.goal or self._pick_goal(),
                minutes=item.minutes,
                source="schedule",
                notify=bool(self._cfg("autoplay_notify_on_start", False)),
            )
            return
        # 3) 空闲自动玩手机
        if not bool(self._cfg("autoplay_enable", True)):
            return
        session_minutes = float(self._cfg("autoplay_session_minutes", 20))
        daily_budget = float(self._cfg("autoplay_daily_budget_minutes", 60))
        runs_limit = int(self._cfg("autoplay_runs_per_day", 3))
        if self.state.daily_minutes() >= daily_budget or self.state.daily_runs() >= runs_limit:
            return
        idle_minutes = float(self._cfg("autoplay_idle_minutes", 45))
        last_active = max(self._last_user_msg_ts(), self._startup_ts)
        if now - last_active < idle_minutes * 60:
            return
        if not self._in_active_hours():
            return
        minutes = min(session_minutes, max(5.0, daily_budget - self.state.daily_minutes()))
        goal = self._pick_goal()
        self.logger.info(f"空闲 {idle_minutes:g} 分钟，开始自主玩手机: {goal!r} {minutes:g}min")
        await self.start_play_session(
            goal=goal,
            minutes=minutes,
            source="idle",
            notify=bool(self._cfg("autoplay_notify_on_start", False)),
        )

    def _last_user_msg_ts(self) -> float:
        return max(self.state.last_user_msg_ts, self._startup_ts)

    def _in_active_hours(self) -> bool:
        raw = str(self._cfg("autoplay_active_hours", "09:00-23:30") or "").strip()
        if not raw or "-" not in raw:
            return True
        try:
            start_s, end_s = [part.strip() for part in raw.split("-", 1)]
            now_min = time.localtime().tm_hour * 60 + time.localtime().tm_min
            sh, sm = [int(x) for x in start_s.split(":")]
            eh, em = [int(x) for x in end_s.split(":")]
            start, end = sh * 60 + sm, eh * 60 + em
            if start <= end:
                return start <= now_min <= end
            return now_min >= start or now_min <= end
        except Exception:
            return True

    def _pick_goal(self) -> str:
        raw = str(self._cfg("autoplay_goals", "") or "")
        goals = [g.strip() for g in raw.replace("，", ",").split(",") if g.strip()]
        if not goals:
            goals = ["随便刷一会儿手机，看看有什么新鲜事", "逛逛应用商店/短视频，玩一会儿游戏"]
        idx = self.state.last_autoplay_goal_index % len(goals)
        self.state.last_autoplay_goal_index = (idx + 1) % len(goals)
        self.state.save()
        return goals[idx]

    # ------------------------------------------------------------------ 模式进出
    async def begin_play_mode(
        self,
        goal: str,
        minutes: float,
        source: str = "chat",
        auto_continue: bool = True,
        prestart: bool = True,
    ) -> None:
        minutes = float(util.clamp(float(minutes or 20), 1, 240))
        self.state.begin(goal=goal, source=source, minutes=minutes, auto_continue=auto_continue)
        self.logger.info(
            f"进入手机模式: goal={goal!r} minutes={minutes:g} source={source}"
        )
        if prestart and bool(self._cfg("prestart_vm_on_enter", True)):
            # 在请求 LLM 之前先把模拟器开机，避免第一次工具调用等太久触发超时
            try:
                info = await self.phone.ensure_started()
                self._diag(f"预启动模拟器完成: {info}")
            except Exception as exc:
                self._diag(f"预启动模拟器失败: {exc!r}")

    async def _prestart_vm_bg(self) -> None:
        try:
            if not self.phone.is_started:
                info = await self.phone.ensure_started()
                self._diag(f"后台预启动模拟器完成: {info}")
        except Exception as exc:
            self._diag(f"后台预启动模拟器失败: {exc!r}")

    async def start_play_session(
        self,
        goal: str,
        minutes: float,
        source: str = "manual",
        notify: bool = True,
    ) -> dict[str, Any]:
        minutes = float(util.clamp(float(minutes or 20), 1, 240))
        if not self.state.active:
            await self.begin_play_mode(goal=goal, minutes=minutes, source=source)
        result = await self.autoplay.start(goal=goal, minutes=minutes, source=source, notify=notify)
        return result

    async def _stop_vm_checked(self, reason: str = "") -> None:
        """关闭模拟器并确认真的关掉了（失败重试一次，写诊断日志）。"""
        try:
            await self.phone.stop()
        except Exception as exc:
            self._diag(f"关闭模拟器异常({reason}): {exc!r}")
        await asyncio.sleep(2)
        try:
            if await self.phone.mumu.is_running():
                self._diag(f"模拟器仍在运行({reason})，重试关闭")
                await self.phone.stop()
                await asyncio.sleep(2)
            running = await self.phone.mumu.is_running()
            self._diag(f"关闭模拟器完成({reason}) running={running}")
        except Exception as exc:
            self._diag(f"确认模拟器状态失败({reason}): {exc!r}")

    async def finish_play_session(
        self,
        reason: str = "",
        notify: bool = True,
        skip_autoplay_stop: bool = False,
        stop_vm_override: bool | None = None,
    ) -> None:
        current = asyncio.current_task()
        if not skip_autoplay_stop:
            if self.autoplay.task is not None and self.autoplay.task is not current:
                if self.autoplay.running:
                    await self.autoplay.stop(reason=reason, notify=False)
        minutes_played = 0.0
        if self.state.started_ts:
            minutes_played = max(0.0, (time.time() - self.state.started_ts) / 60.0)
        session_source = self.state.source
        # 自动会话结束后重置空闲计时：下一局要等 idle_minutes，避免半夜连续开好几局
        if session_source in ("idle", "schedule", "continue", "wake-resume"):
            self.state.last_user_msg_ts = time.time()
        if minutes_played > 0:
            self.state.add_play_minutes(minutes_played)
        self.state.add_diary(f"收手：{reason}", "end")
        summary_text = self._compose_session_summary(reason, minutes_played)
        # 操作总结不再发给用户，直接写进长期记忆（默认 iris L2，默认不截断）
        try:
            await self._persist_session_summary(summary_text)
        except Exception as exc:
            self._diag(f"写入收尾总结失败: {exc!r}")
        # 操作教程总结（Agent 可选：只有它主动用 mumu_phone_lesson 记过经验才写）
        try:
            lessons = self.state.take_lessons()
            if lessons:
                lesson_text = self._compose_lesson_summary(lessons)
                await self._persist_memory_text(
                    lesson_text,
                    tags=self._keywords(
                        "lesson_memory_tags", "玩手机,教程,经验,操作技巧"
                    ),
                    file_name="lessons.md",
                    label="操作教程总结",
                    sink_cfg="lesson_memory_sink",
                )
            else:
                self._diag("操作教程总结：本次 Agent 没有主动记录经验，跳过")
        except Exception as exc:
            self._diag(f"写入操作教程总结失败: {exc!r}")
        # 收手前停止可能存在的录屏/录音，并清理临时媒体文件
        try:
            await self.media.abort_all()
        except Exception as exc:
            self._diag(f"收尾清理媒体录制失败: {exc!r}")
        pending = self.state.unhandled_pending()
        stop_vm = bool(self._cfg("shutdown_on_exit", True))
        if stop_vm_override is not None:
            stop_vm = bool(stop_vm_override)
        if stop_vm:
            try:
                await self._stop_vm_checked("finish")
            except Exception as exc:
                self._diag(f"收尾关模拟器失败: {exc!r}")
        self.state.end()
        # 如果 Agent 是在某一轮回答中间收手，把这一轮的工具组换回普通工具，
        # 让它能继续做别的任务（不主动停掉当前回答）。
        try:
            self._restore_all_active_requests(hint=True)
        except Exception as exc:
            self._diag(f"收手后恢复普通工具组失败: {exc!r}")
        # 会话结束，清掉没处理的排队消息/提示节流记录（属于上一局，不该留到下一局）
        try:
            if self.state.pending:
                self._diag(f"清理未处理消息 {len(self.state.pending)} 条（会话结束）")
            self.state.pending.clear()
            self.state.last_hint_ts.clear()
            self.state.save()
        except Exception:
            pass
        notify_finish = notify and bool(self._cfg("notify_on_finish", False))
        # 定时/空闲自动玩默认不往聊天里发"手机收起来了"，免得半夜刷屏
        if session_source in ("idle", "schedule") and not bool(
            self._cfg("autoplay_notify_on_finish", False)
        ):
            notify_finish = False
        if notify_finish:
            short = str(self._cfg("finish_notify_text", "手机收起来了。") or "").strip()
            if short:
                await self.notify_owner(short)
        self._diag(
            f"finish_play_session reason={reason!r} minutes={minutes_played:.2f} "
            f"source={session_source or '—'} notify_finish={notify_finish} "
            f"summary_chars={len(summary_text)}"
        )

    def _compose_session_summary(self, reason: str, minutes_played: float) -> str:
        """把这次玩手机的过程整理成一段可存长期记忆的总结（不发聊天，默认不截断）。"""
        session_start = self.state.started_ts or 0
        entries = [
            e
            for e in self.state.diary
            if e.get("kind") in ("round", "agent", "exit")
            and float(e.get("ts") or 0) >= session_start
        ]
        tail = entries[-8:]
        lines = [
            f"[手机操作] {util.now_str('%Y-%m-%d %H:%M')} 结束（{reason or '结束'}），"
            f"本次约 {minutes_played:.1f} 分钟。目标：{self.state.goal or '随便玩玩'}。"
        ]
        if tail:
            lines.append("过程要点：")
            for item in tail:
                lines.append(f"- {str(item.get('text') or '')}")
        pending = self.state.unhandled_pending()
        if pending:
            lines.append(f"（还有 {len(pending)} 条消息未回）")
        text = "\n".join(lines)
        max_chars = int(self._cfg("summary_max_chars", 0) or 0)
        if max_chars > 0:
            text = util.truncate(text, max_chars)
        return text

    def _compose_lesson_summary(self, lessons: list[dict[str, Any]]) -> str:
        """把 Agent 主动记的操作经验整理成"操作教程总结"（单独一条长期记忆）。"""
        lines = [
            f"[手机教程] {util.now_str('%Y-%m-%d %H:%M')} 本次玩手机总结的操作经验，"
            f"共 {len(lessons)} 条（遇到什么情况 → 该怎么做）："
        ]
        for idx, item in enumerate(lessons, start=1):
            situation = str(item.get("situation") or "").strip()
            action = str(item.get("action") or "").strip()
            lines.append(f"{idx}. 遇到：{situation} → 做法：{action}")
        return "\n".join(lines)

    async def _persist_memory_text(
        self,
        content: str,
        *,
        tags: list[str],
        file_name: str,
        label: str,
        sink_cfg: str = "summary_memory_sink",
    ) -> str:
        """把一段文本写长期记忆（默认 iris L2），同时本地留档。返回简短结果。"""
        if not content.strip():
            return ""
        if not bool(self._cfg("summary_to_memory", True)):
            return "summary_to_memory=false，已跳过"
        sink = str(self._cfg(sink_cfg, "iris_l2") or "iris_l2").strip().lower()
        # 本地留档（永远写，方便排查）
        try:
            mem_dir = self.data_dir / "memory"
            util.ensure_dir(mem_dir)
            with (mem_dir / file_name).open("a", encoding="utf-8") as f:
                f.write(f"\n## {util.now_str()}\n{content}\n")
        except Exception:
            pass
        if sink in ("none", "off", "false", "local"):
            self._diag(f"{label}只写本地（sink={sink}）chars={len(content)}")
            return f"只写本地 chars={len(content)}"
        tool_name = str(self._cfg("iris_memory_tool_name", "save_memory") or "save_memory")
        iris_result = ""
        try:
            from astrbot.core.agent.run_context import ContextWrapper
            from astrbot.core.astr_agent_context import AstrAgentContext

            manager = self.context.get_llm_tool_manager()
            tool = None
            try:
                tool = manager.get_full_tool_set().get_tool(tool_name)
            except Exception:
                tool = None
            if tool is None:
                tool = manager.get_func(tool_name)
            if tool is None:
                iris_result = f"未找到 {tool_name}（iris 记忆插件未安装/未启用），已只写本地"
            else:
                event = self.build_synthetic_event(f"[{label}]")
                self._patch_event_identity_for_memory(event)
                run_context = ContextWrapper(
                    context=AstrAgentContext(context=self.context, event=event)
                )
                result = await tool.call(
                    run_context,
                    content=content,
                    confidence=0.85,
                    tags=tags,
                )
                iris_result = util.truncate(str(result or ""), 160)
        except Exception as exc:
            iris_result = f"写入 iris L2 异常: {util.truncate(repr(exc), 160)}"
        self._diag(f"{label}写入长期记忆 sink={sink} chars={len(content)} result={iris_result}")
        return iris_result

    async def _persist_session_summary(self, summary: str) -> None:
        """收尾总结写长期记忆：默认调用 iris 插件的 save_memory 工具写 L2。"""
        if not summary.strip():
            return
        await self._persist_memory_text(
            summary,
            tags=["玩手机", "手机操作", "自动记录"],
            file_name="sessions.md",
            label="收尾总结",
        )

    def _patch_event_identity_for_memory(self, event: AstrMessageEvent) -> None:
        """让合成事件带上真实用户身份，iris 写 L2 时 user/group 元数据才对。"""
        try:
            from astrbot.core.platform.message_session import MessageSession

            session = MessageSession.from_str(self.state.owner_umo)
            sender = event.message_obj.sender
            if sender is not None:
                sender.user_id = self.state.owner_sender_id or session.session_id
                if self.state.owner_name:
                    sender.nickname = self.state.owner_name
            if session.message_type.value == "GroupMessage":
                try:
                    event.message_obj.group_id = session.session_id
                    event.message_obj.group = type("GroupStub", (), {"group_id": session.session_id})()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            event.role = "admin"
        except Exception:
            pass

    async def notify_owner(self, text: str) -> bool:
        umo = self.state.owner_umo
        if not umo:
            self.logger.warning("还没有记录到可主动发送消息的会话（owner_umo 为空）")
            return False
        try:
            ok = await self.context.send_message(umo, MessageChain([Plain(text)]))
            return bool(ok)
        except Exception as exc:
            self.logger.warning(f"主动发送消息失败: {exc!r}")
            return False

    async def _safe_reply(self, event: AstrMessageEvent, text: str) -> None:
        try:
            await event.send(MessageChain([Plain(text)]))
        except Exception as exc:
            self.logger.warning(f"直接回复失败: {exc!r}")

    # ------------------------------------------------------------------ 消息拦截
    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_any_message(self, event: AstrMessageEvent) -> None:
        if not self._enabled or not self._is_current():
            return
        text = (event.message_str or "").strip()
        if not text:
            return
        # 忽略机器人自己
        try:
            if event.get_sender_id() == event.get_self_id():
                return
        except Exception:
            pass
        now = time.time()
        try:
            is_wake_message = bool(event.is_at_or_wake_command)
        except Exception:
            is_wake_message = False
        if is_wake_message:
            self.state.last_user_msg_ts = now
        if not self.state.owner_umo:
            self._remember_owner(event)
        elif is_wake_message and (event.is_admin() or event.is_private_chat()):
            self._remember_owner(event)
        # 命令行优先放行
        wake_prefix = str(self._cfg("command_wake_prefix", "/") or "/")
        is_command = bool(wake_prefix) and text.startswith(wake_prefix)
        if self.state.active:
            if is_command or not self._should_interrupt(event):
                # 不是 @/触发词：放过，不提示、不入队、不打断手机模式
                now_diag = time.time()
                if now_diag - float(getattr(self, "_last_skip_diag", 0.0)) > 10.0:
                    self._last_skip_diag = now_diag
                    self._diag(
                        f"忽略非唤醒消息 sender={event.get_sender_name() or event.get_sender_id()!r} "
                        f"text={util.truncate(text, 30)!r} "
                        f"is_at_or_wake={bool(getattr(event, 'is_at_or_wake_command', False))} "
                        f"is_wake={bool(getattr(event, 'is_wake', False))}"
                    )
                return
            self._diag(f"on_any_message 模式中收到@{event.get_sender_name()}: {util.truncate(text, 80)!r}")
            if self._is_stop_intent(text):
                self.logger.info(f"用户喊停手机模式: {text!r}")
                await self.finish_play_session(reason="用户喊停", notify=False)
                await self._safe_reply(event, str(self._cfg("stop_reply_text", "好，手机收起来了。")))
                event.stop_event()
                return
            if is_command:
                return
            if not self._should_interrupt(event):
                return
            # 打扰：入队 + 自动提示，不回复正文
            self.state.add_pending(
                umo=event.unified_msg_origin,
                sender_id=str(event.get_sender_id() or ""),
                sender_name=str(event.get_sender_name() or ""),
                text=text,
                platform=event.get_platform_name() or "",
            )
            cooldown = float(self._cfg("interrupt_hint_cooldown_sec", 60))
            last_hint = float(self.state.last_hint_ts.get(event.unified_msg_origin) or 0)
            hint_text = str(self._cfg("interrupt_hint_text", "正在玩手机，稍等～"))
            if now - last_hint >= cooldown:
                self.state.last_hint_ts[event.unified_msg_origin] = now
                self.state.save()
                await self._safe_reply(
                    event,
                    f"{hint_text}\n（消息已转给我了，我决定要不要回你）",
                )
            # 如果命令手机模式开着但后台没在玩（例如卡在 play(auto_play=false)），
            # 用这条 @ 消息把自主玩拉起来，让 Agent 真的能处理这条消息
            self._maybe_kick_autoplay("wake-message")
            event.stop_event()
            return
        # 未激活：判断是否是"去玩手机"
        if not is_command and self._is_enter_intent(text) and not self.autoplay.running:
            if not self._entry_allowed(event):
                return
            if self._entry_lock.locked():
                return
            async with self._entry_lock:
                minutes = float(self._cfg("mode_default_minutes", 30))
                await self.begin_play_mode(goal=text, minutes=minutes, source="chat", auto_continue=True)
            self.logger.info(f"检测到玩手机意图，进入手机模式: {text!r}")
            self._diag(f"进入手机模式: {text!r} umo={event.unified_msg_origin}")

    def _entry_allowed(self, event: AstrMessageEvent) -> bool:
        """是否允许这条消息触发"去玩手机"（只允许 @/唤醒前缀，且管理员/私聊）。"""
        try:
            # 注意：不能用 event.is_wake —— AstrBot 的 is_wake 会被"注册了全量消息监听
            # 的插件"（包括本插件自己的 on_any_message）在 WakingStage 里置为 True，
            # 群聊普通消息也会变成 is_wake=True。真正可靠的判断是 is_at_or_wake_command。
            if not bool(getattr(event, "is_at_or_wake_command", False)):
                return False
        except Exception:
            return False
        if not bool(self._cfg("enter_require_admin", True)):
            return True
        try:
            return bool(event.is_admin() or event.is_private_chat())
        except Exception:
            return False

    def _should_interrupt(self, event: AstrMessageEvent) -> bool:
        """玩手机时，哪些消息需要拦截并回"正在玩手机"。

        按用户要求：只有 **@ 机器人 或带唤醒前缀（触发词）** 的消息才会递给 Agent；
        普通群聊消息直接放过，不拦、不提示、不入队。
        """
        try:
            # 只用 is_at_or_wake_command（@ / 唤醒词 / 私聊 / 引用回复）。
            # event.is_wake 不可靠：本插件注册了全量消息监听，WakingStage 会因此
            # 把每条群聊消息的 is_wake 都置成 True。
            if not bool(getattr(event, "is_at_or_wake_command", False)):
                return False
        except Exception:
            return False
        if bool(self._cfg("interrupt_all_sessions", False)):
            return True
        try:
            if event.unified_msg_origin == self.state.owner_umo:
                return True
            return bool(event.is_admin() or event.is_private_chat())
        except Exception:
            return False

    def _remember_owner(self, event: AstrMessageEvent) -> None:
        try:
            umo = event.unified_msg_origin
            if not umo:
                return
            self.state.owner_umo = umo
            self.state.owner_name = str(event.get_sender_name() or "")
            self.state.owner_sender_id = str(event.get_sender_id() or "")
            self.state.save()
        except Exception:
            pass

    def _is_enter_intent(self, text: str) -> bool:
        if not (bool(self._cfg("natural_language_entry", True))):
            return False
        if len(text) > 120:
            return False
        keywords = self._keywords(
            "enter_keywords",
            "玩手机,操作手机,用手机,打开手机,看看手机,看下手机,看手机,手机看看,"
            "打开模拟器,操作模拟器,用模拟器,安卓虚拟机, MuMu,去刷手机,帮我玩手机",
        )
        lowered = text.lower()
        return any(item.lower() in lowered for item in keywords)

    def _is_stop_intent(self, text: str) -> bool:
        keywords = self._keywords(
            "stop_keywords",
            "别玩了,不玩了,退出玩手机,停止玩手机,停止操作,手机收起来,收手,回来吧,别玩手机了,结束玩手机",
        )
        lowered = text.lower()
        if any(item.lower() in lowered for item in keywords):
            return True
        # 很短的"停/停一下/够了"
        return text.strip() in {"停", "停一下", "够了", "回来", "别玩了"}

    # ------------------------------------------------------------------ LLM 请求注入
    # 用很低的 priority 保证手机模式的提示词注入在最后执行：
    # 不会被 iris / openviking / token_controller 等插件的 system_prompt 追加/裁剪再污染。
    @filter.on_llm_request(priority=-1000)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if not self._enabled or not self._is_current():
            return
        try:
            names: list[str] = []
            umo = getattr(event, "unified_msg_origin", "") or ""
            if umo:
                self._active_reqs[umo] = req
                # 记录进入手机模式前的普通请求状态，供 Agent 中途收手后恢复
                self._snapshot_request(umo, req)
            if req.func_tool and getattr(req.func_tool, "tools", None):
                names = [t.name for t in req.func_tool.tools]
            active_before = self.state.active
            if self.state.active:
                await self._slim_for_vm_mode(req)
            else:
                self._remove_tools(req, FULL_ONLY_TOOLS)
                # 记下普通模式工具组（可能已被 lazy-tools 等插件过滤），收手后原样恢复
                self._remember_normal_toolset(umo, req)
            names_after = [t.name for t in req.func_tool.tools] if req.func_tool else []
            full_names: list[str] = []
            try:
                full_set = self.context.get_llm_tool_manager().get_full_tool_set()
                full_names = [t.name for t in full_set.tools]
            except Exception as exc:
                full_names = [f"<error {exc!r}>"]
            self._diag(
                f"on_llm_request active={active_before} tools_before={len(names)} "
                f"tools_after={len(names_after)} "
                f"mumu_after={len([n for n in names_after if n.startswith('mumu_')])} "
                f"mumu_in_mgr={len([n for n in full_names if n.startswith('mumu_')])}"
            )
        except Exception as exc:
            self._diag(f"on_llm_request 异常: {exc!r}")

    async def _slim_for_vm_mode(self, req: ProviderRequest) -> None:
        """手机模式：只留手机工具 + 必要的发消息工具，并裁掉历史上下文。

        注意：AstrBot 在组装请求时可能已经按 persona / 其他插件改写过工具集，
        所以这里不依赖 req 里正好有手机工具，而是从 ToolManager 里重新取一份。
        """
        iris_names = self._iris_tool_names()
        allowed = set(FULL_TOOL_NAMES) | iris_names
        if not bool(self._cfg("lesson_note_enable", True)):
            allowed.discard("mumu_phone_lesson")
        if not bool(self._cfg("media_assist_enable", True)):
            allowed -= MEDIA_TOOL_NAMES
        for name in self._keywords("vm_mode_extra_tools", "send_message_to_user"):
            allowed.add(name)
        # 从工具管理器重建工具集（手机工具一定在管理器里注册过）
        vm_tools = []
        try:
            full_set = self.context.get_llm_tool_manager().get_full_tool_set()
            vm_tools = [tool for tool in full_set.tools if tool.name in allowed]
        except Exception as exc:
            self._diag(f"_slim_for_vm_mode 取工具失败: {exc!r}")
        new_set = ToolSet()
        for tool in vm_tools:
            new_set.add_tool(tool)
        # 保留请求里本来就有的"额外白名单"工具（如 send_message_to_user）
        if req.func_tool and getattr(req.func_tool, "tools", None):
            for tool in req.func_tool.tools:
                if tool.name in allowed and new_set.get_tool(tool.name) is None:
                    new_set.add_tool(tool)
        req.func_tool = new_set if new_set.tools else None
        # 缓存友好模式：不裁剪历史/附加内容、不改写系统提示词前缀，只追加稳定的手机提示块，
        # 让模型的前缀缓存/缓存命中尽可能保留（代价是 token 更多）。
        cache_mode = bool(self._cfg("vm_mode_cache_friendly", False))
        if (not cache_mode) and bool(self._cfg("slim_trim_contexts", True)):
            keep = int(util.clamp(int(self._cfg("slim_context_keep_messages", 8)), 0, 40))
            if keep <= 0:
                req.contexts = []
            elif req.contexts and len(req.contexts) > keep:
                req.contexts = req.contexts[-keep:]
        if (not cache_mode) and bool(self._cfg("slim_extra_content_parts", True)):
            req.extra_user_content_parts = []
        if bool(self._cfg("slim_system_prompt", True)):
            block = self._vm_system_block(cache_friendly=cache_mode)
            persona_source, persona_prompt = await self.resolve_phone_persona()
            iris_ctx = await self.get_iris_phone_context()
            replace_mode = (
                bool(self._cfg("vm_mode_replace_system_prompt", True)) and not cache_mode
            )
            before_len = len(req.system_prompt or "")
            if replace_mode:
                # 手机模式用自己的"单独人格提示词 + 手机操作规则"整体替换系统提示词，
                # 不再带上 AstrBot 那一长串通用系统提示（技能/工具说明/插件提醒等）。
                parts: list[str] = []
                if persona_prompt:
                    parts.append("# Persona Instructions\n\n" + persona_prompt)
                if iris_ctx:
                    parts.append(iris_ctx)
                parts.append(block)
                req.system_prompt = "\n\n".join(parts)
            else:
                extra = iris_ctx
                req.system_prompt = (
                    (req.system_prompt or "").rstrip()
                    + (("\n\n" + extra) if extra else "")
                    + "\n\n"
                    + block
                )
            max_chars = int(self._cfg("slim_system_prompt_max_chars", 0))
            if max_chars > 0 and len(req.system_prompt) > max_chars:
                req.system_prompt = req.system_prompt[:max_chars] + "\n…（系统提示已精简）"
            self._diag(
                f"注入手机模式提示词 prompt_mode={'cache' if cache_mode else 'slim'} "
                f"mode={'replace' if replace_mode else 'append'} "
                f"block={len(block)}字 iris={len(iris_ctx)}字 "
                f"iris_tools={sorted(iris_names)} "
                f"system_prompt {before_len}->{len(req.system_prompt)}字 "
                f"persona_source={persona_source or '—'} persona_chars={len(persona_prompt)} "
                f"persona_in_prompt={('# Persona Instructions' in (req.system_prompt or ''))} "
                f"contexts={len(req.contexts)} tools={len(req.func_tool.tools) if req.func_tool else 0}"
            )

    async def resolve_phone_persona(self) -> tuple[str, str]:
        """解析手机模式/自主玩使用的"单独人格提示词"。

        优先级：
        1. 配置 `phone_persona_prompt`（推荐，单独设定，内容自己控制）；
        2. `phone_persona_fallback_astrbot=true` 时回退到当前会话人格提示词；
        3. 都没有则返回空（只用手机操作规则）。
        返回 (来源标识, 人格提示词)。
        """
        custom = str(self._cfg("phone_persona_prompt", "") or "").strip()
        if custom:
            return "custom", custom
        if bool(self._cfg("phone_persona_fallback_astrbot", True)):
            persona_id, prompt = await self.resolve_persona_prompt()
            if prompt:
                return f"astrbot:{persona_id or 'default'}", prompt
        return "", ""

    # ------------------------------------------------------------------ iris 少量背景
    async def get_iris_phone_context(self, force: bool = False) -> str:
        """取一小段 iris 记忆/画像，供 Agent 评论时知道用户是谁、最近在干嘛。

        只取少量（默认画像+3 条记忆、总长 ≤600 字），不把 iris 的大段注入搬进来；
        iris 未安装/不可用时返回空字符串，不影响手机模式。
        """
        if not bool(self._cfg("phone_iris_context_enable", True)):
            return ""
        if not self.state.owner_umo:
            return ""
        ttl = float(util.clamp(float(self._cfg("phone_iris_context_ttl_sec", 600)), 30, 3600))
        now = time.time()
        if not force and self._iris_ctx_cache[1] and now - self._iris_ctx_cache[0] < ttl:
            return self._iris_ctx_cache[1]
        async with self._iris_ctx_lock:
            if (
                not force
                and self._iris_ctx_cache[1]
                and time.time() - self._iris_ctx_cache[0] < ttl
            ):
                return self._iris_ctx_cache[1]
            text = await self._build_iris_phone_context()
            self._iris_ctx_cache = (time.time(), text)
            return text

    def _iris_tool_names(self) -> set[str]:
        """手机模式/自主玩额外暴露的记忆插件工具（默认 iris 的查询/画像/保存）。"""
        if not bool(self._cfg("phone_iris_tools_enable", True)):
            return set()
        raw = str(
            self._cfg(
                "phone_iris_tool_names",
                "search_memory,get_profile,save_memory",
            )
            or ""
        )
        names = {item.strip() for item in raw.replace("，", ",").split(",") if item.strip()}
        try:
            available = {
                tool.name
                for tool in self.context.get_llm_tool_manager().get_full_tool_set().tools
            }
        except Exception:
            available = set()
        return names & available

    def _iris_inject_query(self, owner: str) -> str:
        """按配置拼出注入 iris 时用的检索语句（模板 + 关键词）。"""
        template = str(
            self._cfg(
                "phone_iris_inject_query",
                "{owner} 最近在做什么 近况 项目 喜好",
            )
            or ""
        ).strip()
        try:
            query = template.format(owner=owner, goal=self.state.goal or "")
        except Exception:
            query = template.replace("{owner}", owner).replace(
                "{goal}", self.state.goal or ""
            )
        raw_keywords = str(
            self._cfg("phone_iris_inject_keywords", "近况,项目,喜好,视频,评论") or ""
        )
        keywords = [
            item.strip()
            for item in raw_keywords.replace("，", ",").split(",")
            if item.strip()
        ]
        extra = [kw for kw in keywords if kw not in query]
        if extra:
            query = (query + " " + " ".join(extra)).strip()
        return query or owner

    async def _build_iris_phone_context(self) -> str:
        owner = self.state.owner_name or "用户"
        max_chars = int(util.clamp(int(self._cfg("phone_iris_context_max_chars", 600)), 100, 2000))
        event = self.build_synthetic_event("[手机] 评论参考")
        self._patch_event_identity_for_memory(event)
        parts: list[str] = []
        if bool(self._cfg("phone_iris_profile_enable", True)):
            profile_text = await self._call_phone_plugin_tool(
                "get_profile", {"target_type": "user"}, event
            )
            cleaned = self._clean_iris_text(profile_text, 260)
            if cleaned:
                parts.append(f"{owner} 的画像摘要：{cleaned}")
        if bool(self._cfg("phone_iris_memory_enable", True)):
            top_k = int(util.clamp(int(self._cfg("phone_iris_memory_top_k", 3)), 1, 8))
            query = self._iris_inject_query(owner)
            self._diag(f"iris 检索 query={util.truncate(query, 100)!r} top_k={top_k}")
            memory_text = await self._call_phone_plugin_tool(
                "search_memory",
                {"query": query, "top_k": top_k},
                event,
            )
            cleaned = self._clean_iris_text(memory_text, 320)
            if cleaned:
                parts.append(f"最近相关记忆：{cleaned}")
        if not parts:
            return ""
        text = (
            "# 少量背景（只在评论/聊天确实相关时自然用；禁止外泄隐私，不要提“记忆/资料/我是AI”）\n"
            + "\n".join(f"- {p}" for p in parts)
        )
        text = util.truncate(text, max_chars)
        self._diag(
            f"iris 少量背景注入 {len(text)}字 "
            f"profile={bool(parts and '画像' in parts[0])} parts={len(parts)}"
        )
        return text

    async def _call_phone_plugin_tool(self, name: str, args: dict, event) -> str:
        """调用其它插件注册的 LLM 工具（这里是 iris 的 get_profile / search_memory）。"""
        try:
            from astrbot.core.agent.run_context import ContextWrapper
            from astrbot.core.astr_agent_context import AstrAgentContext

            manager = self.context.get_llm_tool_manager()
            tool = None
            try:
                tool = manager.get_full_tool_set().get_tool(name)
            except Exception:
                tool = None
            if tool is None:
                tool = manager.get_func(name)
            if tool is None:
                self._diag(f"iris 工具 {name} 不存在（iris 未安装/未启用）")
                return ""
            run_context = ContextWrapper(
                context=AstrAgentContext(context=self.context, event=event)
            )
            result = await tool.call(run_context, **args)
            return str(result or "")
        except Exception as exc:
            self._diag(f"调用 {name} 失败: {util.truncate(repr(exc), 160)}")
            return ""

    @staticmethod
    def _clean_iris_text(text: str, limit: int) -> str:
        """把 iris 工具的长输出压成适合塞进提示词的一小段。"""
        if not text:
            return ""
        t = str(text).strip()
        if not t or t.startswith("未找到") or "不可用" in t[:30] or "失败" in t[:30]:
            return ""
        lines: list[str] = []
        for raw in t.splitlines():
            s = raw.strip()
            if not s:
                continue
            if s.startswith("找到 ") or "相似度" in s or "画像系统未启用" in s:
                continue
            if s.startswith("1.") or s[:1].isdigit():
                # "1. [id] 内容" -> 内容
                for sep in ("] ", " "):
                    if sep in s:
                        s = s.split(sep, 1)[1].strip()
                        break
            lines.append(s)
            if len("；".join(lines)) >= limit:
                break
        return util.truncate("；".join(lines), limit)

    def _vm_system_block(self, cache_friendly: bool = False) -> str:
        pending = "" if cache_friendly else self.state.pending_notice_text()
        if cache_friendly:
            goal_line = (
                f"目标：{self.state.goal or '随便玩玩'}。"
                "（剩余时间/待处理消息用 mumu_phone_status / mumu_phone_messages 查，避免系统提示词频繁变化影响缓存）"
            )
        else:
            goal_line = (
                f"目标：{self.state.goal or '随便玩玩'}；剩余时间：{self.state.remaining_text()}。"
            )
        lines = [
            "# 📱 手机模式（已激活，模拟器窗口隐藏，只有你能看到屏幕）",
            goal_line,
            "观察：`mumu_phone_screen` 看单帧（静态细节/点按前确认）；`mumu_phone_frames` 连续采样一次返回多张带时间戳的图（看视频/动画/滑动/游戏过程）——参数由你定：duration 采样秒数、count 帧数、fps 每秒帧数、every 每几秒一帧、mode=frames 一帧一张图 / sheet 拼图版、max_images 最多几张图、region 裁剪、delay 等待。看视频时先小段粗看（如 duration=4, fps=2），需要细节再加 fps/count 或裁剪 region 分段看。",
            "操作：优先 `mumu_phone_tap`（可传 text/index 直接点元素）；坐标与截图同尺度；`mumu_phone_text` 支持中文；`mumu_phone_app` 打开 App；`mumu_phone_key` 返回键/主页。",
            "消息：只有 @你 或带触发词的消息会被转给你（对方只收到「正在玩手机」）；消息列在下方，想回用 `mumu_phone_say`，不想回就忽略。",
            "回顾：`mumu_phone_recent` 查看你最近在手机上做过什么（操作记录/日记/统计）。",
        ]
        iris_names = self._iris_tool_names()
        if iris_names:
            lines.append(
                "记忆：可调用 "
                + "、".join(f"`{name}`" for name in sorted(iris_names))
                + " 查用户画像/相关记忆（评论或回复前可查一次，别反复查）；需要补充记忆可用 save_memory。"
            )
        if bool(self._cfg("lesson_note_enable", True)):
            lines.append(
                "经验：遇到踩坑或好用的操作技巧时，可用 `mumu_phone_lesson(situation, action)` 记一条；"
                "收手时会整理成《操作教程总结》单独写入长期记忆（可选，不记也行）。"
            )
        if self._media_tools_enabled():
            flags = self.media.assist_flags()
            lines.append(
                "媒体辅助（录制期间仍可正常操作/截图）："
                "`mumu_phone_record_start`/`mumu_phone_record_stop` 后台录屏，停止时可让视觉/全模态模型转述画面；"
                "`mumu_phone_hear_start`/`mumu_phone_hear_stop` 后台录音（只录 MuMu 进程自己的声音，不录 Windows 其它应用），停止时可 STT 转写/让音频模型描述声音；"
                f"当前开关：STT={'开' if flags.get('stt') else '关'}、"
                f"音频模型={'开' if flags.get('audio_llm') else '关'}、"
                f"视觉转述={'开' if flags.get('omni') else '关'}；"
                "详细状态用 `mumu_phone_media_status`。"
            )
        lines.extend(
            [
                "",
                "## 收手规则（重要）",
                "1. 想结束操作时**必须调用工具 `mumu_phone_exit`**（参数 {} 即可），不要只在文字里说结束——不调用工具就退不出去。",
                "2. 操作总结会自动写入长期记忆（iris L2），**不用在聊天里复述整段过程**；用户问起时再用一两句话概括即可。",
                "3. 到时间/空闲超时系统会自动收手并关闭模拟器。",
            ]
        )
        if pending:
            lines.append("")
            lines.append(pending)
        if bool(self._cfg("phone_comment_guard", True)):
            lines.extend(
                [
                    "",
                    "## 评论/发言规则（重要）",
                    "1. 在视频/动态下评论时，**只评论当前内容本身**；不要写与当前视频无关的个人近况、聊天内容、隐私或无关信息。",
                    "2. 下面若有“少量背景”，只在确实和当前内容相关时自然带过，不要大段复述、不要暴露是资料/记忆。",
                    "3. 不要提自己是 AI/机器人/插件，也不要提这些背景资料的来源；不确定是否合适就不发评论。",
                ]
            )
        return "\n".join(lines)

    @staticmethod
    def _filter_tool_set(tool_set, keep: set[str]) -> Any:
        if not tool_set or not getattr(tool_set, "tools", None):
            return tool_set
        tool_set.tools = [tool for tool in tool_set.tools if tool.name in keep]
        return tool_set

    def _keep_tools(self, req: ProviderRequest, keep: set[str]) -> None:
        req.func_tool = self._filter_tool_set(req.func_tool, keep)

    def _remove_tools(self, req: ProviderRequest, remove: set[str]) -> None:
        if req.func_tool and getattr(req.func_tool, "tools", None):
            req.func_tool.tools = [
                tool for tool in req.func_tool.tools if tool.name not in remove
            ]
        # 普通聊天里补上"轻量工具"，让 Agent 知道手机可用
        if bool(self._cfg("expose_compact_tools_in_normal_mode", True)):
            try:
                if not req.func_tool:
                    req.func_tool = ToolSet()
                full_set = self.context.get_llm_tool_manager().get_full_tool_set()
                for tool in full_set.tools:
                    if tool.name in COMPACT_TOOLS and req.func_tool.get_tool(tool.name) is None:
                        req.func_tool.add_tool(tool)
            except Exception as exc:
                self._diag(f"_remove_tools 补轻量工具失败: {exc!r}")
        if req.func_tool is not None and not req.func_tool.tools:
            req.func_tool = None

    # --------------------------------------------------------- 同一轮请求内换工具组
    def _build_normal_tool_set(self):
        """普通聊天工具组：全量工具里去掉重型手机工具（保留 other plugins 的工具）。"""
        try:
            full_set = self.context.get_llm_tool_manager().get_full_tool_set()
        except Exception as exc:
            self._diag(f"_build_normal_tool_set 取工具失败: {exc!r}")
            return None
        expose_compact = bool(self._cfg("expose_compact_tools_in_normal_mode", True))
        normal = ToolSet()
        for tool in full_set.tools:
            if tool.name in FULL_ONLY_TOOLS:
                continue
            if not expose_compact and tool.name in COMPACT_TOOLS:
                continue
            normal.add_tool(tool)
        return normal if normal.tools else None

    def _snapshot_request(self, umo: str, req: ProviderRequest) -> None:
        """记录进入/退出手机模式前的普通请求状态，供收手后恢复。"""
        if not umo or umo in self._req_snapshots:
            return
        try:
            self._req_snapshots[umo] = {
                "system_prompt": req.system_prompt or "",
                "contexts": list(req.contexts or []),
                "extra_user_content_parts": list(
                    getattr(req, "extra_user_content_parts", None) or []
                ),
            }
        except Exception as exc:
            self._diag(f"_snapshot_request 失败: {exc!r}")

    def _remember_normal_toolset(self, umo: str, req: ProviderRequest) -> None:
        """记录当前请求的普通工具组，收手后原样换回去（兼容 lazy-tools 等插件过滤）。"""
        if not umo or req is None:
            return
        try:
            snapshot = self._req_snapshots.setdefault(umo, {})
            snapshot["normal_tools"] = list(
                getattr(req.func_tool, "tools", None) or []
            )
        except Exception as exc:
            self._diag(f"_remember_normal_toolset 失败: {exc!r}")

    def _append_request_hint(self, req: ProviderRequest, text: str) -> None:
        if TextPart is None or not text:
            return
        try:
            parts = list(getattr(req, "extra_user_content_parts", None) or [])
            parts.append(TextPart(text=text))
            req.extra_user_content_parts = parts
        except Exception as exc:
            self._diag(f"_append_request_hint 失败: {exc!r}")

    async def _apply_phone_toolset_to_request(self, event: AstrMessageEvent) -> bool:
        """Agent 在普通聊天里调用 mumu_phone_play 后，把当前请求工具组切成手机工具。"""
        umo = getattr(event, "unified_msg_origin", "") or ""
        req = self._active_reqs.get(umo)
        if req is None:
            return False
        self._snapshot_request(umo, req)
        self._remember_normal_toolset(umo, req)
        try:
            await self._slim_for_vm_mode(req)
        except Exception as exc:
            self._diag(f"_apply_phone_toolset_to_request 失败: {exc!r}")
            return False
        self._append_request_hint(
            req,
            "[手机模式已开启（同一轮回答）] 工具组已切换为完整手机工具，可以直接截图/点击/滑动/输入/看多帧/录屏。"
            "操作完必须调用 `mumu_phone_exit` 收手；退出后普通工具会自动恢复，你可以在同一轮回答里继续完成用户原本的任务，不要提前结束回答。",
        )
        self._diag(
            f"同一轮回答内切换手机工具组 umo={umo} "
            f"tools={len(req.func_tool.tools) if req.func_tool else 0}"
        )
        return True

    def _restore_normal_toolset(self, event=None, umo: str = "", hint: bool = True) -> bool:
        """收手后把当前请求工具组换回普通工具，并恢复进入前的系统提示/上下文。"""
        umo = umo or (getattr(event, "unified_msg_origin", "") if event else "") or ""
        req = self._active_reqs.get(umo)
        snapshot = self._req_snapshots.pop(umo, None)
        if req is None:
            return False
        try:
            normal_tools = snapshot.get("normal_tools") if snapshot else None
            if normal_tools is not None:
                normal_set = ToolSet()
                for tool in normal_tools:
                    normal_set.add_tool(tool)
                req.func_tool = normal_set if normal_set.tools else None
            else:
                req.func_tool = self._build_normal_tool_set()
            if snapshot:
                req.system_prompt = snapshot.get("system_prompt") or ""
                req.contexts = list(snapshot.get("contexts") or [])
                req.extra_user_content_parts = list(
                    snapshot.get("extra_user_content_parts") or []
                )
            if hint:
                self._append_request_hint(
                    req,
                    "[手机已收起] 已退出手机模式，普通工具已恢复。请在同一轮回答里继续完成用户原本的请求，不要因为收手而停止回答。",
                )
            self._diag(
                f"同一轮回答内恢复普通工具组 umo={umo} "
                f"tools={len(req.func_tool.tools) if req.func_tool else 0}"
            )
            return True
        except Exception as exc:
            self._diag(f"_restore_normal_toolset 失败: {exc!r}")
            return False

    def _restore_all_active_requests(self, hint: bool = True) -> None:
        for umo in list(self._active_reqs.keys()):
            try:
                self._restore_normal_toolset(umo=umo, hint=hint)
            except Exception as exc:
                self._diag(f"_restore_all_active_requests 失败 umo={umo}: {exc!r}")

    # ------------------------------------------------------------------ Agent 回合开始/结束
    @filter.on_agent_begin()
    async def on_agent_begin(self, event: AstrMessageEvent, run_context=None) -> None:
        if not self._is_current():
            return
        self._agent_busy = True

    @filter.on_agent_done()
    async def on_agent_done(self, event: AstrMessageEvent, run_context, response) -> None:
        if not self._is_current():
            return
        umo = getattr(event, "unified_msg_origin", "") or ""
        if umo:
            self._active_reqs.pop(umo, None)
            self._req_snapshots.pop(umo, None)
        self._agent_busy = False
        if not self.state.active:
            return
        if self.state.owner_umo and event.unified_msg_origin != self.state.owner_umo:
            return
        if not self.state.auto_continue:
            return
        if not bool(self._cfg("continue_after_turn", True)):
            return
        if self.autoplay.running:
            return
        self.state.auto_continue = False
        asyncio.create_task(self._continue_after_turn(), name="mumu-vm-continue")
        if self.state.unhandled_pending():
            self._maybe_kick_autoplay("agent-done-pending")

    def _maybe_kick_autoplay(self, why: str = "wake-message") -> None:
        """手机模式开着但后台没在自主玩时（例如卡在 play(auto_play=false)），
        用一次 @ 消息把自主玩重新拉起来，让 Agent 真的能看到消息并决定回不回。"""
        if self.autoplay.running or self._agent_busy or not self.state.active:
            return
        if self._kick_task is not None and not self._kick_task.done():
            return
        self._kick_task = asyncio.create_task(
            self._kick_autoplay_for_pending(why), name="mumu-vm-kick"
        )

    async def _kick_autoplay_for_pending(self, why: str) -> None:
        try:
            await asyncio.sleep(2.0)
            if not self._is_current():
                return
            if not self.state.active or self.autoplay.running or self._agent_busy:
                return
            remain_min = 5.0
            if self.state.deadline_ts:
                remain_min = max(1.5, self.state.remaining_sec() / 60.0)
            goal = self.state.goal or self._pick_goal()
            self._diag(f"唤醒消息触发恢复自主玩手机 why={why} goal={util.truncate(goal, 60)!r}")
            await self.autoplay.start(
                goal=goal, minutes=remain_min, source="wake-resume", notify=False
            )
        except Exception as exc:
            self._diag(f"恢复自主玩手机失败: {exc!r}")

    async def _continue_after_turn(self) -> None:
        try:
            await asyncio.sleep(1.5)
            if not self._is_current():
                return
            if not self.state.active or self.autoplay.running:
                return
            remain_min = 15.0
            if self.state.deadline_ts:
                remain_min = max(1.0, self.state.remaining_sec() / 60.0)
            goal = self.state.goal or self._pick_goal()
            self.logger.info("Agent 回合结束，自动继续玩手机（后台自主模式）。")
            await self.autoplay.start(
                goal=goal, minutes=remain_min, source="continue", notify=False
            )
        except Exception as exc:
            self.logger.warning(f"自动继续玩手机失败: {exc!r}")

    # ------------------------------------------------------------------ 人格 / Provider
    async def resolve_persona_prompt(self) -> tuple[str, str]:
        """取当前 owner 会话生效的人格提示词，返回 (persona_id, prompt)。

        主 Agent 的手机模式请求里人格由 AstrBot 原生注入，不需要重复拼；
        这里主要给后台自主玩手机（tool_loop_agent 自己传 system_prompt）用。
        """
        umo = self.state.owner_umo or ""
        if not umo:
            return "", ""
        platform_name = ""
        try:
            from astrbot.core.platform.message_session import MessageSession

            platform_name = MessageSession.from_str(umo).platform_id
        except Exception:
            pass
        provider_settings = None
        try:
            cfg = self.context.get_config(umo) or {}
            if isinstance(cfg, dict):
                provider_settings = cfg.get("provider_settings")
        except Exception:
            pass
        persona = None
        persona_id = ""
        try:
            persona_id, persona, _, _ = await self.context.persona_manager.resolve_selected_persona(
                umo=umo,
                conversation_persona_id=None,
                platform_name=platform_name,
                provider_settings=provider_settings,
            )
        except Exception as exc:
            self._diag(f"解析人格失败: {exc!r}")
        prompt = str((persona or {}).get("prompt") or "").strip()
        if not prompt:
            try:
                default_persona = await self.context.persona_manager.get_default_persona_v3(umo)
                if default_persona:
                    persona_id = persona_id or str(default_persona.get("name") or "")
                    prompt = str(default_persona.get("prompt") or "").strip()
            except Exception:
                pass
        return str(persona_id or ""), prompt

    async def resolve_autoplay_provider_id(self) -> str | None:
        configured = str(self._cfg("autoplay_provider_id", "") or "").strip()
        if configured:
            return configured
        umo = self.state.owner_umo or ""
        if umo:
            try:
                return await self.context.get_current_chat_provider_id(umo)
            except Exception:
                pass
        try:
            provider = self.context.get_using_provider(umo or None)
            if provider:
                return provider.meta().id
        except Exception:
            pass
        try:
            manager = self.context.provider_manager
            providers = manager.get_all_providers() if hasattr(manager, "get_all_providers") else []
            for provider in providers:
                if provider.meta().type in ("chat_completion", None):
                    return provider.meta().id
        except Exception:
            pass
        return None

    def build_autoplay_toolset(self) -> ToolSet:
        tool_set = ToolSet()
        try:
            full = self.context.get_llm_tool_manager().get_full_tool_set()
        except Exception:
            return tool_set
        iris_names = self._iris_tool_names()
        allowed = set(AUTOPLAY_TOOL_NAMES) | iris_names
        if not bool(self._cfg("lesson_note_enable", True)):
            allowed.discard("mumu_phone_lesson")
        if not bool(self._cfg("media_assist_enable", True)):
            allowed -= MEDIA_TOOL_NAMES
        for tool in full.tools:
            if tool.name in allowed:
                tool_set.add_tool(tool)
        return tool_set

    def build_synthetic_event(self, prompt: str) -> CronMessageEvent:
        umo = self.state.owner_umo
        from astrbot.core.platform.message_type import MessageType

        try:
            session = MessageSession.from_str(umo)
        except Exception:
            session = MessageSession(
                "aiocqhttp", MessageType.FRIEND_MESSAGE, "mumu_vm"
            )
        event = CronMessageEvent(
            context=self.context,
            session=session,
            message=prompt,
            sender_id="mumu-vm",
            sender_name="手机",
            message_type=session.message_type,
        )
        self._ensure_event_admin(event)
        return event

    @staticmethod
    def _ensure_event_admin(event) -> None:
        """合成事件/记忆事件按 owner 管理员处理（供需要权限的工具使用）。"""
        try:
            event.role = "admin"
        except Exception:
            pass

    # ------------------------------------------------------------------ 工具辅助
    def _mode_guard(self) -> str | None:
        if not self.state.active:
            return (
                "手机模式未激活。先让用户说“去玩手机/看看手机”，或调用 mumu_phone_play；"
                "手机工具只有在手机模式下可用。"
            )
        return None

    def _touch(self, action: str, detail: str = "") -> None:
        self.state.touch_tool(action)
        try:
            self.state.add_activity(action, detail)
        except Exception:
            pass

    def _pending_suffix(self) -> str:
        notice = self.state.pending_notice_text()
        return f"\n\n{notice}" if notice else ""

    def _tail(self) -> str:
        return (
            f"\n手机剩余时间：{self.state.remaining_text()}；"
            f"如需收手：mumu_phone_exit。"
            + self._pending_suffix()
        )

    @staticmethod
    def _json(data: Any) -> str:
        return json.dumps(data, ensure_ascii=False)

    def _call_result(self, text: str, png: bytes | None = None):
        """构造带图片的 CallToolResult（图片会被 AstrBot 缓存后交给模型看）。"""
        return self._call_result_images(text, [png] if png else [])

    def _call_result_images(
        self,
        text: str,
        images: list[bytes],
        mime_type: str = "image/png",
        mimes: list[str] | None = None,
    ):
        """构造带多张图片的 CallToolResult：一次工具调用可以返回 N 张图给模型看。"""
        import base64

        import mcp

        content: list[Any] = [mcp.types.TextContent(type="text", text=text)]
        for idx, png in enumerate(images):
            mime = mime_type
            if mimes and idx < len(mimes) and mimes[idx]:
                mime = mimes[idx]
            content.append(
                mcp.types.ImageContent(
                    type="image",
                    data=base64.b64encode(png).decode("ascii"),
                    mimeType=mime,
                )
            )
        return mcp.types.CallToolResult(content=content)

    # ================================================================== 工具
    @filter.llm_tool(name="mumu_phone_recent")
    async def mumu_phone_recent(
        self,
        event: AstrMessageEvent,
        minutes: float = 60,
        limit: float = 30,
        include_diary: bool = True,
    ) -> str:
        """查看 Agent 最近在手机上做了什么（操作记录 + 日记 + 统计）。手机模式外也能用。

        Args:
            minutes(number): 看最近多少分钟内的记录，默认 60
            limit(number): 最多返回多少条，默认 30
            include_diary(boolean): 是否包含日记/回合小结，默认 true
        """
        minutes = float(util.clamp(float(minutes or 60), 1, 24 * 60))
        limit_i = int(util.clamp(int(limit or 30), 1, 100))
        lines: list[str] = []
        if self.state.active:
            lines.append(
                f"📱 手机模式开启中：目标={util.truncate(self.state.goal, 80) or '—'}；"
                f"已进行 {util.hms(max(0.0, time.time() - self.state.started_ts))}；"
                f"剩余 {self.state.remaining_text()}；自主玩={'进行中' if self.autoplay.running else '未运行'}"
            )
        else:
            lines.append("📱 手机模式：未激活（下面是历史记录）")
        items = self.state.recent_activity(minutes, limit_i)
        if items:
            lines.append(f"最近 {minutes:g} 分钟的操作记录（{len(items)} 条）：")
            for item in items:
                detail = str(item.get("detail") or "")
                lines.append(
                    f"  {item.get('time') or ''} {item.get('tool') or ''}"
                    + (f"：{util.truncate(detail, 120)}" if detail else "")
                )
        else:
            lines.append(f"最近 {minutes:g} 分钟没有操作记录")
        if include_diary:
            cutoff = time.time() - minutes * 60
            diary = [
                entry
                for entry in self.state.diary
                if float(entry.get("ts") or 0) >= cutoff
            ][-limit_i:]
            if diary:
                lines.append("最近的日记/回合小结：")
                for entry in diary:
                    lines.append(
                        f"  {entry.get('time') or ''} [{entry.get('kind') or 'note'}] "
                        f"{util.truncate(str(entry.get('text') or ''), 160)}"
                    )
        lines.append(
            f"累计：操作 {self.state.total_actions} 次，截图/采样 {self.state.total_shots} 张；"
            f"今日自主玩 {self.state.daily_minutes():.0f} 分钟"
        )
        return "\n".join(lines)

    @filter.llm_tool(name="mumu_phone_status")
    async def mumu_phone_status(
        self, event: AstrMessageEvent, show_prompt: bool = False
    ) -> str:
        """查看 MuMu 安卓虚拟机与"玩手机"模式的当前状态。

        Args:
            show_prompt(boolean): 是否预览手机模式实际会用的系统提示词（人格来源/长度/开头），排障用
        """
        lines = []
        if self.state.active:
            lines.append(
                f"📱 手机模式：开启中（来源：{self.state.source or '—'}，"
                f"目标：{util.truncate(self.state.goal, 80) or '—'}，剩余：{self.state.remaining_text()}）"
            )
            lines.append(f"自主玩手机：{'进行中' if self.autoplay.running else '未运行'}")
            if self.state.unhandled_pending():
                lines.append(self.state.pending_notice_text())
        else:
            lines.append("📱 手机模式：未激活")
        lines.append(await self.phone.status_text())
        try:
            procs = await self.phone.mumu.list_processes()
            svc = await self.phone.mumu.service_state("MuMuRemoteService")
            lines.append(
                f"MuMu 后台：进程={'无' if not procs else ', '.join(procs)}；"
                f"MuMuRemoteService={svc}"
            )
        except Exception:
            pass
        if self._media_tools_enabled():
            try:
                lines.append(self.media.status_text())
            except Exception:
                pass
        try:
            persona_source, phone_persona = await self.resolve_phone_persona()
            replace_mode = bool(self._cfg("vm_mode_replace_system_prompt", True))
            if phone_persona:
                lines.append(
                    f"手机模式人格：{persona_source or '默认'}"
                    f"（{len(phone_persona)} 字，{'替换系统提示词' if replace_mode else '追加在原系统提示词后'}）"
                )
            else:
                lines.append("手机模式人格：无（只用手机操作规则）")
        except Exception as exc:
            lines.append(f"手机模式人格：解析失败（{util.truncate(str(exc), 80)}）")
        lines.append(f"今日自主玩手机：{self.state.daily_minutes():.0f} 分钟 / {self.state.daily_runs()} 次")
        lines.append(f"下次定时：{self.state.next_scheduled_text()}")
        if show_prompt:
            try:
                probe = ProviderRequest()
                await self._slim_for_vm_mode(probe)
                prompt = probe.system_prompt or ""
                replace_mode = bool(self._cfg("vm_mode_replace_system_prompt", True))
                src, persona = await self.resolve_phone_persona()
                lines.append(
                    f"手机模式系统提示词预览：mode={'replace' if replace_mode else 'append'}，"
                    f"人格来源={src or '无'}（{len(persona)}字），总长 {len(prompt)} 字，tools="
                    f"{len(probe.func_tool.tools) if probe.func_tool else 0}，contexts={len(probe.contexts)}"
                )
                iris_names = self._iris_tool_names()
                lines.append(
                    "手机模式额外记忆工具："
                    + (", ".join(sorted(iris_names)) if iris_names else "无（phone_iris_tools_enable=false 或未安装 iris）")
                )
                cache_mode = bool(self._cfg("vm_mode_cache_friendly", False))
                lines.append(
                    "提示词缓存友好模式："
                    + (
                        "开（追加注入、不裁剪历史/附加内容、手机提示块保持稳定）"
                        if cache_mode
                        else "关（精简替换，token 更省但会降低前缀缓存命中）"
                    )
                )
                lines.append("开头预览：\n" + util.truncate(prompt, 400).replace("\n", " / "))
            except Exception as exc:
                lines.append(f"提示词预览失败：{util.truncate(repr(exc), 160)}")
        self._touch("status")
        return "\n".join(lines)

    @filter.llm_tool(name="mumu_phone_play")
    async def mumu_phone_play(
        self,
        event: AstrMessageEvent,
        goal: str = "",
        minutes: float = 0,
        auto_play: bool = False,
        notify: bool = True,
    ) -> str:
        """由你自行决定进入"玩手机"模式（不再依赖用户关键词自动触发）。

        当你判断用户需要你操作手机（例如"去玩手机 / 打开某 App / 看看某页面 / 刷视频"）时调用。
        默认 auto_play=false：进入后**当前这一轮的工具组会立刻切换成完整手机工具**，
        你可以继续截图/点击/滑动/输入/看多帧，在同一轮回答里完成手机任务；
        操作完调用 mumu_phone_exit，收手后普通工具会自动恢复，你可以在同一轮回答里继续做其它任务。

        Args:
            goal(string): 这次玩手机的目标/任务，例如"刷一会儿B站""打开微信看看消息"
            minutes(number): 计划玩多少分钟，0 表示用配置的默认值
            auto_play(boolean): true=交给后台自主玩模式（本轮不亲自操作手机）；默认 false=本轮自己操作
            notify(boolean): auto_play=true 时是否给用户发一条"我去玩手机了"
        """
        if not self._enabled:
            return "插件已禁用。"
        if self.state.active:
            return (
                f"手机模式已经是开启状态（目标：{util.truncate(self.state.goal, 80)}，"
                f"剩余：{self.state.remaining_text()}）。直接开始操作即可。"
            )
        minutes = float(minutes or self._cfg("mode_default_minutes", 30))
        goal = goal or "随便玩玩手机"
        if auto_play:
            await self.begin_play_mode(
                goal=goal, minutes=minutes, source="chat", auto_continue=True, prestart=False
            )
            if not self.phone.is_started:
                asyncio.create_task(self._prestart_vm_bg(), name="mumu-vm-prestart")
            self._diag(
                f"mumu_phone_play goal={util.truncate(goal, 60)!r} minutes={minutes:g} auto_play=True"
            )
            try:
                await self.autoplay.start(
                    goal=goal, minutes=minutes, source="chat", notify=bool(notify)
                )
            except Exception as exc:
                self._diag(f"mumu_phone_play 启动自主玩失败: {exc!r}")
                await self.finish_play_session(
                    reason="自主玩启动失败", notify=False, stop_vm_override=True
                )
                return (
                    f"进入手机模式失败：{util.truncate(str(exc), 200)}\n"
                    "已自动收起手机，请检查 Provider（需支持图片+工具调用）或配置。"
                )
            return (
                f"已进入手机模式并交给后台自主玩：目标 {goal!r}，{minutes:g} 分钟。\n"
                "本轮不占用你的工具组，你可以先继续回答/做其它任务；"
                "期间 @我 的消息会转给后台 Agent 决定是否回复。"
                + self._pending_suffix()
            )
        await self.begin_play_mode(
            goal=goal, minutes=minutes, source="chat", auto_continue=False, prestart=False
        )
        if not self.phone.is_started:
            asyncio.create_task(self._prestart_vm_bg(), name="mumu-vm-prestart")
        swapped = await self._apply_phone_toolset_to_request(event)
        self._diag(
            f"mumu_phone_play goal={util.truncate(goal, 60)!r} minutes={minutes:g} "
            f"auto_play=False swapped={swapped}"
        )
        if swapped:
            return (
                f"已进入手机模式：目标 {goal!r}，{minutes:g} 分钟。\n"
                "当前这一轮的工具组已切换为完整手机工具（截图/点击/滑动/输入/多帧/录屏等），"
                "你可以直接开始操作手机，不需要等下一轮。\n"
                "操作完成后调用 `mumu_phone_exit` 收手；收手后普通工具会自动恢复，"
                "你可以在同一轮回答里继续完成用户原本的其它任务（不要提前结束回答）。"
            )
        return (
            f"已进入手机模式：目标 {goal!r}，{minutes:g} 分钟。\n"
            "当前请求不是普通聊天上下文，无法就地切换工具组；请先结束本回合，"
            "下一轮会自动出现完整手机工具。"
        )

    @filter.llm_tool(name="mumu_phone_autoplay")
    async def mumu_phone_autoplay(
        self,
        event: AstrMessageEvent,
        action: str = "status",
        goal: str = "",
        minutes: float = 0,
        start_at: str = "",
    ) -> str:
        """管理"自主玩手机"：开始/停止/查看/定时。

        Args:
            action(string): start=立即开始, stop=停止, status=状态, schedule=定时, list=定时列表, cancel=取消定时
            goal(string): 玩手机目标（start/schedule 用）
            minutes(number): 玩多少分钟（start/schedule 用，0=默认）
            start_at(string): 定时开始时间，格式 "HH:MM" 或 "YYYY-MM-DD HH:MM"（schedule 用）
        """
        action = (action or "status").strip().lower()
        if action in ("status", "状态"):
            return (
                f"自主玩手机：{'进行中' if self.autoplay.running else '未运行'}\n"
                f"手机模式：{'开启' if self.state.active else '关闭'}\n"
                f"今日已玩：{self.state.daily_minutes():.0f} 分钟 / {self.state.daily_runs()} 次\n"
                f"下次定时：{self.state.next_scheduled_text()}"
            )
        if action in ("stop", "停止"):
            if self.state.active:
                await self.finish_play_session(reason="手动停止", notify=True)
                return "已停止玩手机并收起手机。"
            return "当前没有在玩手机。"
        if action in ("schedule", "定时"):
            if not start_at:
                return "schedule 需要 start_at，例如 \"21:30\" 或 \"2026-10-19 21:30\""
            run_at = self._parse_when(start_at)
            if not run_at:
                return f"无法解析时间：{start_at!r}，请用 HH:MM 或 YYYY-MM-DD HH:MM"
            item = self.state.add_scheduled(
                run_at=run_at,
                minutes=float(minutes or self._cfg("autoplay_session_minutes", 20)),
                goal=goal or self._pick_goal(),
            )
            return f"已定时：{time.strftime('%m-%d %H:%M', time.localtime(item.run_at))} 开始，{item.minutes:g} 分钟，目标：{item.goal}"
        if action in ("list", "列表"):
            upcoming = [s for s in self.state.scheduled if not s.done]
            if not upcoming:
                return "没有定时任务。"
            return "\n".join(
                f"- {s.id} {time.strftime('%m-%d %H:%M', time.localtime(s.run_at))} "
                f"{s.minutes:g}min {util.truncate(s.goal, 60)}"
                for s in sorted(upcoming, key=lambda x: x.run_at)
            )
        if action in ("cancel", "取消"):
            for item in self.state.scheduled:
                if not item.done:
                    item.done = True
            self.state.save()
            return "已取消所有未执行的定时玩手机。"
        return f"未知 action: {action}"

    @filter.llm_tool(name="mumu_phone_screen")
    async def mumu_phone_screen(
        self,
        event: AstrMessageEvent,
        region: str = "",
        max_width: float = 0,
        delay: float = 0,
        annotate: bool = False,
    ) -> Any:
        """截取当前手机屏幕（单帧）。返回图片 + 屏幕信息，适合看静态页面。

        动态界面（视频/动画/游戏/滑动过程）请用 mumu_phone_frames 看多帧。

        Args:
            region(string): 可选裁剪区域，格式 "x,y,w,h" 或 "x1,y1,x2,y2"
            max_width(number): 图片最大宽度，0=默认（1600）
            delay(number): 截图前等待秒数（等页面加载/动画），0~10
            annotate(boolean): 是否叠加上淡坐标网格，方便找坐标
        """
        guard = self._mode_guard()
        if guard:
            self._diag("mumu_phone_screen 被模式守卫拦截")
            return guard
        self._diag(f"mumu_phone_screen 开始 active={self.state.active} phone_started={self.phone.is_started}")
        try:
            shot = await self.phone.screenshot(
                max_width=int(max_width or 0) or None,
                region=region,
                delay=float(delay or 0),
                annotate=bool(annotate),
            )
        except Exception as exc:
            self._diag(f"mumu_phone_screen 失败: {exc!r}")
            return f"截图失败：{util.truncate(str(exc), 300)}"
        self._touch("screen", f"{shot.width}x{shot.height} path={shot.path.name}")
        self.state.total_shots += 1
        self._diag(f"mumu_phone_screen 成功 {shot.width}x{shot.height} bytes={len(shot.png)}")
        focus = await self._safe_focus()
        size = await self._safe_size()
        lines = [
            f"已截取单帧：{shot.width}x{shot.height}（整屏 {shot.full_width}x{shot.full_height}）",
            f"前台应用：{focus or '未知'}；屏幕：{size}",
            f"图片路径：{shot.path}",
        ]
        if shot.region:
            lines.append(f"裁剪区域：{shot.region}")
        lines.append(f"如需点击，坐标直接用这张图的像素坐标。" + self._tail())
        return self._call_result("\n".join(lines), shot.png)

    @filter.llm_tool(name="mumu_phone_frames")
    async def mumu_phone_frames(
        self,
        event: AstrMessageEvent,
        duration: float = 3,
        fps: float = 0,
        every: float = 0,
        count: float = 0,
        mode: str = "frames",
        max_images: float = 0,
        per_image: float = 0,
        cols: float = 4,
        width: float = 0,
        dedupe: bool = True,
        annotate: bool = True,
        region: str = "",
        delay: float = 0,
    ) -> Any:
        """连续采样屏幕多帧，按时间顺序返回**多张图片**（每帧带时间戳/序号标签），用来"看视频/动画/滑动过程"。

        采样频率三选一（优先级 count > fps > every）：
          - count：总共采多少帧（按 duration 均分，例如 duration=6, count=12 → 每 0.5s 一帧）；
          - fps：每秒采几帧（例如 fps=2 → 每 0.5s 一帧）；
          - every：每几秒采一帧（例如 every=2 → 2s 一帧）。
        mode=frames：一帧一张图（默认，画面最清楚、信息量最大）；
        mode=sheet ：多帧拼成图版，按 per_image 自动分成多张图版（省上下文，但每格小、别认小字）。
        返回张数受 max_images 限制；帧太多时按等间隔挑选并注明。看静态页面用 mumu_phone_screen。

        Args:
            duration(number): 采样总时长（秒），默认 3，上限由插件配置（默认 20）
            fps(number): 采样频率：每秒多少帧（如 2=每 0.5 秒一帧；密集画面用 2~10）
            every(number): 采样频率：每多少秒一帧（如 2=每 2 秒一帧；长视频粗看用）
            count(number): 总共采多少帧（给了它则按 duration/count 均分，优先于 fps/every）
            mode(string): frames=一帧一张图（默认）；sheet=多帧拼成图版（自动分成多张）
            max_images(number): 最多返回几张图（0=用配置默认，通常 8；上限 16）
            per_image(number): sheet 模式每张图最多放几帧（0=cols*2）
            cols(number): sheet 模式每行几格，默认 4
            width(number): 每帧/每格宽度，0=自动（frames 默认 768，sheet 默认 480）
            dedupe(boolean): 是否合并连续相同帧（静态画面省 token），默认 true
            annotate(boolean): 是否在每帧顶部标注时间戳/帧序号，默认 true
            region(string): 可选裁剪区域 "x,y,w,h"
            delay(number): 采样前等待秒数（等页面加载/动画），0~10
        """
        guard = self._mode_guard()
        if guard:
            self._diag("mumu_phone_frames 被模式守卫拦截")
            return guard
        self._diag(
            f"mumu_phone_frames 开始 duration={duration:g} fps={fps:g} every={every:g} "
            f"count={count:g} mode={mode} max_images={max_images:g}"
        )
        try:
            force_screencap = bool(
                self.media.video_active() or self.media.audio_active()
            )
        except Exception:
            force_screencap = False
        try:
            result = await self.phone.sample(
                duration=float(duration or 3),
                fps=float(fps or 0),
                every=float(every or 0),
                count=float(count or 0),
                mode=str(mode or "frames"),
                max_images=int(max_images or 0),
                per_image=int(per_image or 0),
                cols=int(cols or 4),
                width=int(width or 0),
                dedupe=bool(dedupe),
                annotate=bool(annotate),
                region=str(region or ""),
                delay=float(delay or 0),
                force_screencap=force_screencap,
            )
        except Exception as exc:
            self._diag(f"mumu_phone_frames 失败: {exc!r}")
            return f"连续采样失败：{util.truncate(str(exc), 300)}"
        self._touch(
            "frames",
            f"{result.frames_total}帧/{len(result.images)}图 间隔{result.interval:.2f}s {result.mode}",
        )
        self.state.total_shots += result.frames_total
        focus = await self._safe_focus()
        lines = [
            f"连续采样完成：共 {result.frames_total} 帧有效画面，采样间隔≈{result.interval:.2f}s，"
            f"时长 {result.duration:.1f}s，方式 {result.method}，模式 {result.mode}，"
            f"返回 {len(result.images)} 张图（按时间顺序，图片按顺序交给模型看）。",
            f"前台应用：{focus or '未知'}；时间轴："
            + ", ".join(self._fmt_sample_times(result.sample_times)),
        ]
        if force_screencap:
            lines.append("（录屏/录音进行中：多帧改用连续截图采集，避免和后台录屏抢 screenrecord）")
        for i, img in enumerate(result.images, start=1):
            lines.append(f"  image {i} = {img.label}（{img.width}x{img.height}）路径：{img.path}")
        if result.notes:
            lines.append("说明：" + "；".join(result.notes))
        lines.append("看静态细节改用 mumu_phone_screen。" + self._tail())
        self._diag(
            f"mumu_phone_frames 完成 frames={result.frames_total} images={len(result.images)} "
            f"interval={result.interval:.2f} method={result.method} mode={result.mode}"
        )
        return self._call_result_images(
            "\n".join(lines),
            [img.png for img in result.images],
            mimes=[img.mime for img in result.images],
        )

    @staticmethod
    def _fmt_sample_times(times: list[float], limit: int = 24) -> list[str]:
        if not times:
            return ["—"]
        out = [f"{t:.1f}s" for t in times[:limit]]
        if len(times) > limit:
            out.append(f"…共{len(times)}帧")
        return out

    # ------------------------------------------------------------------ 媒体辅助
    def _media_tools_enabled(self) -> bool:
        return bool(self._cfg("media_assist_enable", True))

    @filter.llm_tool(name="mumu_phone_media_status")
    async def mumu_phone_media_status(self, event: AstrMessageEvent) -> str:
        """查看媒体辅助（录屏/录音 + STT/音频模型/视觉转述）的可用状态。无参数。"""
        guard = self._mode_guard()
        if guard:
            return guard
        self._touch("media_status")
        return self.media.status_text() + self._tail()

    @filter.llm_tool(name="mumu_phone_record_start")
    async def mumu_phone_record_start(self, event: AstrMessageEvent) -> str:
        """开始后台录屏（录制期间可以继续操作手机、截图/多帧采样）。

        录完后调用 mumu_phone_record_stop 停止，并可选让视觉/全模态模型转述画面。
        只有配置里开启 media_assist_enable 才可用；建议先看 mumu_phone_media_status。
        """
        guard = self._mode_guard()
        if guard:
            return guard
        if not self._media_tools_enabled():
            return "媒体辅助已关闭（media_assist_enable=false），请在插件配置里开启。"
        result = await self.media.start_video()
        self._diag(f"mumu_phone_record_start ok={result.ok} {util.truncate(result.text, 120)}")
        if result.ok:
            self._touch("record_start")
        return result.text + self._tail()

    @filter.llm_tool(name="mumu_phone_record_stop")
    async def mumu_phone_record_stop(
        self,
        event: AstrMessageEvent,
        question: str = "",
        describe: bool = True,
        return_frames: bool = False,
    ) -> Any:
        """停止录屏，可选让视觉/全模态模型把画面转述成文字返回给主模型。

        Args:
            question(string): 你希望转述模型重点关注什么（如"视频里在说什么/页面上的文字"）
            describe(boolean): 是否调用视觉/全模态模型转述画面，默认 true（需 omni_assist_enable）
            return_frames(boolean): 是否把抽出的关键帧也作为图片返回（主模型有 image 模态时用）
        """
        guard = self._mode_guard()
        if guard:
            return guard
        if not self._media_tools_enabled():
            return "媒体辅助已关闭（media_assist_enable=false），请在插件配置里开启。"
        result = await self.media.stop_video()
        if not result.ok:
            return result.text + self._tail()
        self.media.begin_processing()
        flags = self.media.assist_flags()
        video_path = Path(str(result.info.get("path") or ""))
        parts = [result.text]
        frames: list[Path] = []
        if describe and flags.get("omni"):
            if video_path.exists():
                ok, description, frames = await self.media.describe_video(video_path, question)
                parts.append(("视觉/全模态转述：\n" + description) if ok else description)
            else:
                parts.append("视觉转述失败：录屏文件不存在")
        elif describe:
            parts.append("（omni_assist_enable 未开启，跳过画面转述；需要时可在配置开启）")
        if return_frames and not frames and video_path.exists():
            frames = await self.media.extract_frames(video_path)
        if return_frames and frames:
            payload = [path.read_bytes() for path in frames if path.exists()]
            self._touch("record_stop", util.truncate(question, 60))
            await self.media.cleanup_outputs()
            self.media.end_processing()
            return self._call_result_images(
                "\n\n".join(parts) + f"\n\n附 {len(payload)} 张关键帧。" + self._tail(),
                payload,
                mimes=["image/jpeg"] * len(payload),
            )
        self._touch("record_stop", util.truncate(question, 60))
        await self.media.cleanup_outputs()
        self.media.end_processing()
        return "\n\n".join(parts) + self._tail()

    @filter.llm_tool(name="mumu_phone_hear_start")
    async def mumu_phone_hear_start(self, event: AstrMessageEvent) -> str:
        """开始后台录音（抓 Windows 默认扬声器输出，录制期间可继续操作手机）。

        录完调用 mumu_phone_hear_stop 停止，可做 STT 转写和/或交给音频多模态模型描述。
        只有配置里开启 media_assist_enable 才可用。
        """
        guard = self._mode_guard()
        if guard:
            return guard
        if not self._media_tools_enabled():
            return "媒体辅助已关闭（media_assist_enable=false），请在插件配置里开启。"
        result = await self.media.start_audio()
        self._diag(f"mumu_phone_hear_start ok={result.ok} {util.truncate(result.text, 120)}")
        if result.ok:
            self._touch("hear_start")
        return result.text + self._tail()

    @filter.llm_tool(name="mumu_phone_hear_stop")
    async def mumu_phone_hear_stop(
        self,
        event: AstrMessageEvent,
        transcribe: bool = True,
        describe: bool = False,
        question: str = "",
    ) -> str:
        """停止录音，可选 STT 转写语音/让音频多模态模型描述听到的声音。

        Args:
            transcribe(boolean): 是否做 STT 转写，默认 true（需 stt_assist_enable）
            describe(boolean): 是否让音频多模态模型描述声音，默认 false（需 audio_llm_assist_enable）
            question(string): 你希望音频转述模型重点关注什么
        """
        guard = self._mode_guard()
        if guard:
            return guard
        if not self._media_tools_enabled():
            return "媒体辅助已关闭（media_assist_enable=false），请在插件配置里开启。"
        result = await self.media.stop_audio()
        if not result.ok:
            return result.text + self._tail()
        self.media.begin_processing()
        flags = self.media.assist_flags()
        audio_path = Path(str(result.info.get("path") or ""))
        parts = [result.text]
        did = False
        if transcribe and flags.get("stt"):
            ok, text = await self.media.transcribe_audio(audio_path)
            parts.append(("STT 转写：\n" + text) if ok else text)
            did = True
        elif transcribe:
            parts.append("（stt_assist_enable 未开启，跳过 STT 转写）")
        if describe and flags.get("audio_llm"):
            ok, text = await self.media.describe_audio(audio_path, question)
            parts.append(("音频模型描述：\n" + text) if ok else text)
            did = True
        elif describe:
            parts.append("（audio_llm_assist_enable 未开启，跳过音频模型描述）")
        if not did:
            parts.append("未执行任何转述；如需 STT/音频模型请先在插件配置里开启对应开关。")
        self._touch("hear_stop", util.truncate(question, 60))
        await self.media.cleanup_outputs()
        self.media.end_processing()
        return "\n\n".join(parts) + self._tail()

    @filter.llm_tool(name="mumu_phone_ui")
    async def mumu_phone_ui(
        self,
        event: AstrMessageEvent,
        text_contains: str = "",
        only_interactive: bool = True,
        max_items: float = 60,
    ) -> str:
        """读取当前界面的 UI 元素树（可点击/可输入/有文字的元素，含中心坐标）。

        比截图更精确地找到按钮位置；拿到 i/编号后可以直接用 mumu_phone_tap(index=...) 点击。

        Args:
            text_contains(string): 只显示文字/ID 含该关键词的元素
            only_interactive(boolean): 只显示可交互元素，默认 true
            max_items(number): 最多返回多少个元素，默认 60
        """
        guard = self._mode_guard()
        if guard:
            return guard
        try:
            data = await self.phone.ui_summary(
                only_interactive=bool(only_interactive),
                max_items=int(max_items or 60),
                text_contains=text_contains,
            )
        except Exception as exc:
            return f"读取 UI 失败：{util.truncate(str(exc), 300)}"
        self._touch("ui")
        return self._json(data) + self._tail()

    @filter.llm_tool(name="mumu_phone_tap")
    async def mumu_phone_tap(
        self,
        event: AstrMessageEvent,
        x: float = -1,
        y: float = -1,
        text: str = "",
        index: float = -1,
        long_press: bool = False,
        duration_ms: float = 800,
        observe: bool = False,
    ) -> Any:
        """点击屏幕。可以直接给坐标，也可以给文字/UI 编号（会先在 UI 树里找到它再点）。

        Args:
            x(number): 横坐标（截图同尺度），-1 表示不用坐标
            y(number): 纵坐标，-1 表示不用坐标
            text(string): 要点击的元素文字（精确/包含匹配）
            index(number): mumu_phone_ui 返回的元素编号 i，-1 表示不用
            long_press(boolean): 是否长按
            duration_ms(number): 长按时长毫秒，默认 800
            observe(boolean): 点击后是否立刻截一张图返回（默认 false，需要再调 mumu_phone_screen）
        """
        guard = self._mode_guard()
        if guard:
            return guard
        px = None if (x is None or float(x) < 0) else int(x)
        py = None if (y is None or float(y) < 0) else int(y)
        idx = None if (index is None or float(index) < 0) else int(index)
        try:
            result = await self.phone.tap(
                x=px,
                y=py,
                text=text,
                index=idx,
                long_press=bool(long_press),
                duration_ms=int(duration_ms or 800),
            )
        except Exception as exc:
            return f"点击失败：{util.truncate(str(exc), 300)}"
        self._touch(
            "tap",
            f"x={x:g} y={y:g} text={util.truncate(text, 30)!r} index={index:g} 长按={bool(long_press)}",
        )
        await asyncio.sleep(0.35)
        focus = await self._safe_focus()
        text_out = self._json(result) + f"\n前台应用：{focus or '未知'}" + self._tail()
        if observe and result.get("ok"):
            try:
                shot = await self.phone.screenshot()
                return self._call_result(text_out + f"\n图片路径：{shot.path}", shot.png)
            except Exception:
                pass
        return text_out

    @filter.llm_tool(name="mumu_phone_swipe")
    async def mumu_phone_swipe(
        self,
        event: AstrMessageEvent,
        direction: str = "",
        amount: float = 0.6,
        x1: float = -1,
        y1: float = -1,
        x2: float = -1,
        y2: float = -1,
        duration_ms: float = 300,
    ) -> str:
        """滑动屏幕。给 direction（up/down/left/right）就按屏幕比例滚，给四点坐标就精确滑。

        Args:
            direction(string): up=页面上滑/内容向下, down, left, right；留空则用坐标
            amount(number): direction 模式下滑动幅度，0.1~0.9，默认 0.6
            x1(number): 起点 x（坐标模式）
            y1(number): 起点 y（坐标模式）
            x2(number): 终点 x（坐标模式）
            y2(number): 终点 y（坐标模式）
            duration_ms(number): 滑动时长毫秒，默认 300
        """
        guard = self._mode_guard()
        if guard:
            return guard
        try:
            if direction:
                result = await self.phone.scroll(direction, amount=float(amount or 0.6))
            else:
                result = await self.phone.swipe(
                    int(x1), int(y1), int(x2), int(y2), int(duration_ms or 300)
                )
        except Exception as exc:
            return f"滑动失败：{util.truncate(str(exc), 300)}"
        self._touch(
            "swipe",
            f"({x1:g},{y1:g})->({x2:g},{y2:g}) {int(duration_ms or 300)}ms",
        )
        await asyncio.sleep(0.35)
        focus = await self._safe_focus()
        return self._json(result) + f"\n前台应用：{focus or '未知'}" + self._tail()

    @filter.llm_tool(name="mumu_phone_text")
    async def mumu_phone_text(
        self,
        event: AstrMessageEvent,
        text: str = "",
        submit: bool = False,
        clear: bool = False,
    ) -> str:
        """往当前输入框输入文字（支持中文/emoji，经 ADBKeyboard 注入）。

        Args:
            text(string): 要输入的文字
            submit(boolean): 输入后是否按回车提交（搜索/发送），默认 false
            clear(boolean): 输入前是否清空输入框，默认 false
        """
        guard = self._mode_guard()
        if guard:
            return guard
        if not text:
            return "text 不能为空"
        try:
            result = await self.phone.input_text(text, submit=bool(submit), clear=bool(clear))
        except Exception as exc:
            return f"输入失败：{util.truncate(str(exc), 300)}"
        self._touch("text", f"text={util.truncate(text, 40)!r} submit={bool(submit)}")
        await asyncio.sleep(0.25)
        return self._json(result) + self._tail()

    @filter.llm_tool(name="mumu_phone_key")
    async def mumu_phone_key(self, event: AstrMessageEvent, key: str = "back") -> str:
        """按安卓按键：home/back/recent/enter/delete/volume_up/volume_down/power/wakeup/menu/tab/escape 等。

        Args:
            key(string): 按键名，常用 home, back, recent(任务键), enter, delete, volume_up, volume_down, power, wakeup
        """
        guard = self._mode_guard()
        if guard:
            return guard
        key_map = {"recent": "switch_app", "任务": "switch_app", "返回": "back", "主页": "home", "回车": "enter"}
        key = key_map.get((key or "").strip(), (key or "back").strip())
        try:
            result = await self.phone.key(key)
        except Exception as exc:
            return f"按键失败：{util.truncate(str(exc), 300)}"
        self._touch("key", key)
        await asyncio.sleep(0.35)
        return self._json(result) + self._tail()

    @filter.llm_tool(name="mumu_phone_app")
    async def mumu_phone_app(
        self,
        event: AstrMessageEvent,
        action: str = "current",
        target: str = "",
    ) -> str:
        """管理手机 App：查看前台/列表/搜索/启动/打开链接/停止/安装/卸载/信息。

        Args:
            action(string): current=前台应用, list=第三方已安装, list_all=全部已安装, search=搜索包名, launch=打开App, open_url=用浏览器打开链接, stop=强停, info=应用信息, install=安装apk, uninstall=卸载
            target(string): 包名/包名关键词/URL/本地apk路径，视 action 而定
        """
        guard = self._mode_guard()
        if guard:
            return guard
        try:
            result = await self.phone.app(action, target)
        except Exception as exc:
            return f"应用操作失败：{util.truncate(str(exc), 300)}"
        self._touch("app", f"{action} {util.truncate(str(target or ''), 60)}")
        return self._json(result) + self._tail()

    @filter.llm_tool(name="mumu_phone_system")
    async def mumu_phone_system(
        self,
        event: AstrMessageEvent,
        action: str = "info",
        value: str = "",
    ) -> str:
        """手机系统操作：信息/电量/网络/IP/解锁/锁屏/音量/亮度/旋转/存储。

        Args:
            action(string): info=系统概况, wake=解锁亮屏, lock=锁屏, volume=音量(up/down/mute), brightness=亮度0-255, rotate=旋转(auto/portrait/landscape), storage=存储, network=网络
            value(string): 参数值，如 brightness 的 128、volume 的 up
        """
        guard = self._mode_guard()
        if guard:
            return guard
        try:
            result = await self.phone.system(action, value)
        except Exception as exc:
            return f"系统操作失败：{util.truncate(str(exc), 300)}"
        self._touch("system")
        return self._json(result) + self._tail()

    @filter.llm_tool(name="mumu_phone_notifications")
    async def mumu_phone_notifications(
        self, event: AstrMessageEvent, limit: float = 12
    ) -> str:
        """读取手机通知栏里的通知（谁发了什么消息、推送等）。

        Args:
            limit(number): 最多返回几条，默认 12
        """
        guard = self._mode_guard()
        if guard:
            return guard
        try:
            items = await self.phone.notifications(limit=int(limit or 12))
        except Exception as exc:
            return f"读取通知失败：{util.truncate(str(exc), 300)}"
        self._touch("notifications")
        return self._json({"count": len(items), "notifications": items}) + self._tail()

    @filter.llm_tool(name="mumu_phone_shell")
    async def mumu_phone_shell(
        self,
        event: AstrMessageEvent,
        command: str = "",
        timeout: float = 60,
    ) -> str:
        """在安卓里执行 shell 命令（高级功能，adb shell 能做的都能做）。

        Args:
            command(string): 要执行的 shell 命令，例如 "dumpsys battery"、"pm list packages -3"
            timeout(number): 超时秒数，默认 60
        """
        guard = self._mode_guard()
        if guard:
            return guard
        if not bool(self._cfg("enable_shell_tool", True)):
            return "shell 工具已被配置禁用。"
        # 安全：adb shell 权限较大，默认只允许管理员/私聊/所有者（可配置放开）
        if bool(self._cfg("shell_tool_require_admin", True)):
            try:
                allowed = bool(event.is_admin() or event.is_private_chat())
                if not allowed and self.state.owner_sender_id:
                    allowed = str(event.get_sender_id()) == str(self.state.owner_sender_id)
            except Exception:
                allowed = False
            if not allowed:
                return (
                    "mumu_phone_shell 默认仅管理员/私聊可用；"
                    "如需放开请把 shell_tool_require_admin 设为 false。"
                )
        if not command.strip():
            return "command 不能为空"
        try:
            out = await self.phone.shell(command, timeout=float(util.clamp(float(timeout or 60), 5, 180)))
        except Exception as exc:
            return f"shell 执行失败：{util.truncate(str(exc), 300)}"
        self._touch("shell")
        return util.truncate(out or "（无输出）", int(self._cfg("shell_output_max_chars", 6000))) + self._tail()

    @filter.llm_tool(name="mumu_phone_messages")
    async def mumu_phone_messages(
        self, event: AstrMessageEvent, mark_handled: bool = False
    ) -> str:
        """查看玩手机期间被拦下的消息（有人找你，机器人只回了"正在玩手机"）。

        Args:
            mark_handled(boolean): 查看后是否把它们全部标记为已读/不回复
        """
        items = self.state.unhandled_pending()
        if not items:
            return "没有待处理消息。"
        lines = [f"共 {len(items)} 条待处理消息（可以用 mumu_phone_say(msg_id=..., text=...) 回复）："]
        lines += [item.brief() for item in items]
        if mark_handled:
            for item in items:
                self.state.mark_handled(item.id)
            lines.append("已全部标记为不回复。")
        self._touch("messages")
        return "\n".join(lines) + self._tail()

    @filter.llm_tool(name="mumu_phone_say")
    async def mumu_phone_say(
        self,
        event: AstrMessageEvent,
        text: str = "",
        msg_id: str = "",
    ) -> str:
        """给用户发一条消息（玩手机时回复被拦下的消息，或主动说话）。

        Args:
            text(string): 要发送的文字
            msg_id(string): 要回复哪条待处理消息的编号（如 "a1b2c3d4"）；留空=直接发到最近的用户会话
        """
        if not text.strip():
            return "text 不能为空"
        target_umo = self.state.owner_umo
        if msg_id:
            item = self.state.mark_handled(msg_id.strip())
            if item is None:
                return f"找不到编号为 {msg_id!r} 的待处理消息（用 mumu_phone_messages 查看编号）"
            target_umo = item.umo or target_umo
        if not target_umo:
            return "还没有可发送消息的会话（用户还没和机器人说过话）。"
        try:
            ok = await self.context.send_message(target_umo, MessageChain([Plain(text)]))
        except Exception as exc:
            return f"发送失败：{util.truncate(str(exc), 300)}"
        self._touch("say", util.truncate(text, 120))
        if not ok:
            return "发送失败：平台没有找到对应会话。"
        return f"已发送给用户{'（回复 #' + msg_id + '）' if msg_id else ''}：{util.truncate(text, 200)}"

    @filter.llm_tool(name="mumu_phone_diary")
    async def mumu_phone_diary(
        self,
        event: AstrMessageEvent,
        action: str = "get",
        text: str = "",
    ) -> str:
        """玩手机日记：记录/查看自己玩到哪了，方便跨回合、跨轮次保持连续。

        Args:
            action(string): get=查看最近记录, add=追加一条记录
            text(string): add 时要记录的内容
        """
        if action in ("add", "记录", "append"):
            if not text.strip():
                return "text 不能为空"
            if self.state.active:
                self.state.add_diary(text.strip(), "agent")
            else:
                self.state.add_diary(text.strip(), "note")
            return "已记录。"
        return "最近记录：\n" + self.state.diary_text(limit=10) + self._tail()

    @filter.llm_tool(name="mumu_phone_lesson")
    async def mumu_phone_lesson(
        self,
        event: AstrMessageEvent,
        situation: str,
        action: str,
    ) -> str:
        """记录一条操作手机的经验/教程（可选）：遇到什么情况该怎么做。

        收手时会把这些经验整理成《操作教程总结》，作为单独一条写进长期记忆；
        不调用这个工具就不会写，完全可选。遇到踩坑/有效技巧时可以记一条。

        Args:
            situation(string): 遇到的情况/场景，例如"B站评论区输入框被键盘挡住"
            action(string): 该怎么做，例如"先按返回键收起键盘，再点输入框"
        """
        guard = self._mode_guard()
        if guard:
            return guard
        if not situation.strip() or not action.strip():
            return "situation 和 action 都不能为空。"
        self.state.add_lesson(situation.strip(), action.strip())
        self._touch("lesson", util.truncate(situation.strip(), 60))
        count = len(self.state.session_lessons)
        return (
            f"已记录操作经验 #{count}：遇到「{util.truncate(situation, 80)}」→ "
            f"{util.truncate(action, 120)}。收手时会作为《操作教程总结》写入长期记忆（可选）。"
            + self._tail()
        )

    @filter.llm_tool(name="mumu_phone_exit")
    async def mumu_phone_exit(
        self,
        event: AstrMessageEvent,
        stop_vm: bool = True,
        summary: str = "",
    ) -> str:
        """收起手机、退出玩手机模式（默认顺手关掉模拟器）。

        收手后普通工具会自动恢复，你可以**在同一轮回答里继续完成用户原本的其它任务**，
        不需要停掉当前回答或等下一轮。

        Args:
            stop_vm(boolean): 是否关闭 MuMu 模拟器，默认 true
            summary(string): 收尾说明（会写进长期记忆，不会直接发给用户）
        """
        if not self.state.active:
            return "手机模式当前没有开启（可能已经自动收手了，无需再调用）。"
        in_autoplay = self.autoplay.running
        if summary.strip():
            self.state.add_diary(f"收尾：{summary.strip()}", "exit")
        self._diag(
            f"mumu_phone_exit 被调用 in_autoplay={in_autoplay} stop_vm={bool(stop_vm)} "
            f"summary={util.truncate(summary, 80)!r}"
        )
        if in_autoplay:
            # 在自主玩手机的工具循环里：先把循环标记为停止，再走收尾；不要 cancel 当前任务
            self.autoplay.request_internal_stop()
        played = 0.0
        if self.state.started_ts:
            played = max(0.0, (time.time() - self.state.started_ts) / 60.0)
        await self.finish_play_session(
            reason="Agent 主动收手",
            notify=False,
            skip_autoplay_stop=in_autoplay,
            stop_vm_override=bool(stop_vm),
        )
        return (
            f"已收起手机、退出手机模式（本次约 {played:.1f} 分钟）。"
            "普通工具组已恢复，请在同一轮回答里继续完成用户原本的请求，不要停掉这次回答。"
            "本次操作总结已自动写入长期记忆（iris L2），不用向用户复述整段过程；"
            "如果用户问起，简短说一句结果即可。"
        )

    # ------------------------------------------------------------------ 小工具
    async def _safe_focus(self) -> str:
        try:
            focus = await self.phone.focus()
            return focus.get("package", "")
        except Exception:
            return ""

    async def _safe_size(self) -> str:
        try:
            size = await self.phone.window_size()
            return f"{size.get('width')}x{size.get('height')}"
        except Exception:
            return ""

    def _parse_when(self, raw: str) -> float | None:
        raw = raw.strip()
        now = time.localtime()
        for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%m-%d %H:%M"):
            try:
                parsed = time.strptime(raw, fmt)
                if fmt == "%m-%d %H:%M":
                    parsed = time.strptime(f"{now.tm_year}-{raw}", "%Y-%m-%d %H:%M")
                return time.mktime(parsed)
            except Exception:
                continue
        try:
            parsed = time.strptime(raw, "%H:%M")
            ts = time.mktime(
                (now.tm_year, now.tm_mon, now.tm_mday, parsed.tm_hour, parsed.tm_min, 0, 0, 0, -1)
            )
            if ts < time.time():
                ts += 86400
            return ts
        except Exception:
            return None
