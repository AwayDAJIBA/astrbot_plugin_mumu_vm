# -*- coding: utf-8 -*-
"""自主玩手机：用 AstrBot 原生 tool_loop_agent 驱动一段"无人值守"的操作。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from . import util

DEFAULT_SYSTEM_PROMPT = """你正在"玩手机"：你通过 MuMu 安卓模拟器独占操作一台安卓手机，屏幕画面只有你能看到。
你的目标：{goal}

可用工具（完整手机工具集）：
- 观察：mumu_phone_screen（单帧）/ mumu_phone_frames（连续采样多帧、自动返回多张带时间戳图片；参数你定：duration 秒数、count 帧数、fps 每秒帧数、every 每几秒一帧、mode=frames 一帧一张图或 sheet 拼图、max_images 张数、region 裁剪；看视频先 duration=3~5 + fps=2 粗看，再定点细看）/ mumu_phone_ui（UI 树，可按文字点）/ mumu_phone_notifications（通知栏）
- 操作：mumu_phone_tap / mumu_phone_swipe / mumu_phone_text（中文）/ mumu_phone_key / mumu_phone_app / mumu_phone_system / mumu_phone_shell
- 消息：mumu_phone_messages（看 @你 的消息）/ mumu_phone_say（回复用户）/ mumu_phone_diary（记日记）
- 收手：**mumu_phone_exit**（想结束时必须调用它，无参数；只在文字里说结束是退不出去的）

行为准则：
1. 先观察再动手：先截图/看 UI，再点击；坐标与截图同尺度。
2. 像真人一样自然使用 App：可以浏览、点击、滑动、输入、看视频、拍照，但**不要**进行支付/转账/输入支付密码、不要改账号安全设置、不要点广告下载安装未知应用。
3. 每次工具调用后看一眼结果；如果界面没变化，换个方式（截图确认、UI 树、返回键）。
4. 遇到登录/验证码/实名认证/付费墙时，退出该 App，换别的玩。
5. 操作期间只有 @你 或带触发词的消息会转进来（以"📨"出现在工具结果里）；你自行决定是否用 mumu_phone_say 回复，不想回就继续玩。
6. 一次回合（本轮）最多做 6~10 次工具调用，然后停下来输出一句简短小结，格式：本轮小结：<你做了什么、看到什么、下一步想干嘛>。
7. 操作总结会自动写入长期记忆，不要在聊天里长篇复述；想收手直接调用 mumu_phone_exit。
8. 如果觉得该结束了，也调用 mumu_phone_exit（不要只用文字告别）。
9. 评论/发言规则：在视频/动态下评论时只评论当前内容本身；不要写与当前内容无关的个人近况、聊天内容、隐私或无关信息；不要暴露"记忆/资料/我是AI"；不确定是否合适就不发评论。
"""


class AutoplayEngine:
    def __init__(self, plugin):
        self.plugin = plugin
        self.logger = plugin.logger
        self.task: asyncio.Task | None = None
        self.goal: str = ""
        self.stop_event: asyncio.Event = asyncio.Event()
        self.started_at: float = 0.0
        self.minutes: float = 0.0
        self.source: str = ""
        self.rounds: int = 0
        self._finished = False
        self._stop_requested = False
        self._persona_id: str = ""
        self._persona_prompt: str = ""
        self._iris_context: str = ""

    # ------------------------------------------------------------------ 配置
    @property
    def state(self):
        return self.plugin.state

    @property
    def phone(self):
        return self.plugin.phone

    @property
    def context(self):
        return self.plugin.context

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self.plugin.config.get(key, default)
        except Exception:
            return default
        if value is None or value == "":
            return default
        return value

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    # ------------------------------------------------------------------ 控制
    async def start(
        self,
        goal: str,
        minutes: float,
        *,
        source: str = "manual",
        notify: bool = True,
    ) -> dict[str, Any]:
        if self.running:
            return {
                "ok": True,
                "already_running": True,
                "goal": self.goal,
                "elapsed": util.hms(time.time() - self.started_at),
            }
        if not self.state.active:
            self.state.begin(goal=goal, source=source, minutes=minutes)
        self.goal = goal or "随便玩玩手机，放松一下"
        self.minutes = float(minutes or 15)
        self.source = source
        self.stop_event = asyncio.Event()
        self.started_at = time.time()
        self.rounds = 0
        self._finished = False
        self._stop_requested = False
        self.state.autoplay_running = True
        self.state.save()
        self.task = asyncio.create_task(self._run(), name="mumu-vm-autoplay")
        self.plugin._diag(
            f"autoplay 启动 goal={util.truncate(self.goal, 60)!r} minutes={self.minutes:g} source={source}"
        )
        if notify:
            await self.plugin.notify_owner(
                f"📱 我去玩手机了（{self.minutes:g} 分钟，目标：{util.truncate(self.goal, 80)}）。"
                f"期间 @ 我或带触发词的消息会先回「正在玩手机」，然后我决定要不要回。"
            )
        self.logger.info(
            f"自主玩手机启动: goal={self.goal!r} minutes={self.minutes} source={source}"
        )
        return {
            "ok": True,
            "started": True,
            "goal": self.goal,
            "minutes": self.minutes,
            "remaining": self.state.remaining_text(),
        }

    def request_internal_stop(self) -> None:
        """在工具循环内部请求停止（不 cancel 当前任务，由调用方负责收尾）。"""
        self._stop_requested = True
        self.stop_event.set()
        self.state.autoplay_running = False

    async def stop(self, reason: str = "手动停止", notify: bool = False) -> dict[str, Any]:
        was_running = self.running
        self._stop_requested = True
        self.stop_event.set()
        self.state.autoplay_running = False
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        self.task = None
        self.state.save()
        return {"ok": True, "stopped": was_running, "reason": reason}

    # ------------------------------------------------------------------ 主循环
    async def _run(self) -> None:
        reason = "时间到"
        try:
            await self._sleep_small(0.1)
            await self.phone.ensure_started()
            tools = self.plugin.build_autoplay_toolset()
            iris_names = sorted(getattr(self.plugin, "_iris_tool_names", lambda: set())())
            self.plugin._diag(
                f"autoplay 工具集 {len(tools.tools)} 个 含exit={bool(tools.get_tool('mumu_phone_exit'))} "
                f"iris_tools={iris_names}"
            )
            # 后台自主玩时不会走 AstrBot 的 on_llm_request，需要自己把
            # "单独人格提示词"（phone_persona_prompt 或回退人格）带上
            self._persona_id, self._persona_prompt = "", ""
            if bool(self._cfg("autoplay_use_persona", True)):
                try:
                    self._persona_id, self._persona_prompt = (
                        await self.plugin.resolve_phone_persona()
                    )
                except Exception as exc:
                    self.plugin._diag(f"autoplay 解析人格失败: {exc!r}")
                self.plugin._diag(
                    f"autoplay 人格注入 source={self._persona_id or '—'} "
                    f"chars={len(self._persona_prompt)} enabled=True"
                )
            # 少量 iris 背景（用户是谁/最近在干嘛），供评论时参考，避免把大段记忆搬进提示词
            self._iris_context = ""
            try:
                self._iris_context = await self.plugin.get_iris_phone_context()
            except Exception as exc:
                self.plugin._diag(f"autoplay 取 iris 背景失败: {exc!r}")
            self.plugin._diag(
                f"autoplay iris 背景 chars={len(self._iris_context)}"
            )
            # 开机后先截一张，写进日记
            shot = None
            try:
                shot = await self.phone.screenshot()
                self.state.add_diary(f"手机开机完成，屏幕 {shot.width}x{shot.height}", "boot")
            except Exception:
                pass
            deadline = time.time() + self.minutes * 60
            while not self.stop_event.is_set():
                if not getattr(self.plugin, "_is_current", lambda: True)():
                    reason = "插件实例已失效"
                    break
                if not self.state.active:
                    reason = "模式已退出"
                    break
                remain = deadline - time.time()
                if remain <= 5:
                    reason = "时间到"
                    break
                if self.state.deadline_ts and time.time() > self.state.deadline_ts:
                    reason = "模式时限到"
                    break
                self.rounds += 1
                summary = await self._round(deadline)
                self.plugin._diag(
                    f"autoplay 第 {self.rounds} 轮完成: {util.truncate(summary or '', 100)!r}"
                )
                if summary:
                    self.state.add_diary(summary, "round")
                    if summary.startswith("结束"):
                        reason = "Agent 主动收手"
                        break
                gap = float(self._cfg("autoplay_round_gap_sec", 5))
                with_timeout = min(gap, max(1.0, remain))
                await self._sleep_small(with_timeout)
        except asyncio.CancelledError:
            reason = "被中断"
            raise
        except Exception as exc:
            reason = f"出错: {util.truncate(repr(exc), 200)}"
            self.logger.error(f"自主玩手机异常: {exc!r}", exc_info=True)
            try:
                # 定时/空闲自动会话出错默认静默，别半夜往聊天里发报错
                auto_session = (self.source or self.plugin.state.source) in (
                    "idle",
                    "schedule",
                )
                quiet = auto_session and not bool(
                    self._cfg("autoplay_notify_on_error", False)
                )
                if quiet:
                    self.plugin._diag(
                        f"autoplay 出错（自动会话，静默）: {util.truncate(repr(exc), 200)}"
                    )
                else:
                    await self.plugin.notify_owner(
                        f"⚠️ 玩手机时出错了：{util.truncate(str(exc), 160)}"
                    )
            except Exception:
                pass
        finally:
            self.plugin._diag(f"autoplay 结束 reason={reason!r} rounds={self.rounds}")
            if not self._finished and not self._stop_requested:
                self._finished = True
                self.state.autoplay_running = False
                try:
                    await self.plugin.finish_play_session(reason=reason, notify=True)
                except Exception as exc:
                    self.logger.error(f"结束玩手机会话失败: {exc!r}", exc_info=True)
            self.task = None

    async def _round(self, deadline: float) -> str:
        pending = self.state.unhandled_pending()
        remaining_text = util.hms(max(0.0, deadline - time.time()))
        elapsed_text = util.hms(time.time() - self.started_at)
        round_prompt_parts = [
            f"继续玩手机。已进行 {elapsed_text}，总共 {self.minutes:g} 分钟，剩余 {remaining_text}。",
            f"最近经历：\n{self.state.diary_text(limit=5)}",
        ]
        if pending:
            lines = ["📨 有未处理的新消息（你可以回，也可以不理）："]
            lines += [item.brief() for item in pending]
            lines.append("想回：mumu_phone_say(msg_id=消息编号, text=回复内容)；想全部忽略：mumu_phone_messages(mark_handled=true)。")
            round_prompt_parts.append("\n".join(lines))
        round_prompt_parts.append(
            "现在继续：先观察屏幕，然后自然地玩 2~6 个动作，最后用一句「本轮小结：……」收尾。"
            "看视频/动画/游戏过程时用 mumu_phone_frames 采样多帧（一次会返回多张带时间戳的图）；"
            "如果想结束玩手机，调用 mumu_phone_exit（不要只输出文字）；"
            "如果看到 📨 消息，自己决定是否用 mumu_phone_say 回复。"
        )
        prompt = "\n\n".join(round_prompt_parts)
        provider_id = await self.plugin.resolve_autoplay_provider_id()
        if not provider_id:
            raise RuntimeError("没有可用的对话模型 Provider，无法自主玩手机")
        tools = self.plugin.build_autoplay_toolset()
        event = self.plugin.build_synthetic_event(prompt)
        system_prompt = str(self._cfg("autoplay_system_prompt", "") or DEFAULT_SYSTEM_PROMPT)
        system_prompt = system_prompt.replace("{goal}", self.goal)
        if self._persona_prompt:
            system_prompt = (
                "# Persona Instructions\n\n"
                f"{self._persona_prompt}\n\n"
                "（以上人格设定同样适用于玩手机时的言行；操作总结不用发聊天。）\n\n"
                f"{system_prompt}"
            )
        if self._iris_context:
            system_prompt = f"{self._iris_context}\n\n{system_prompt}"
        extra_iris_names = sorted(getattr(self.plugin, "_iris_tool_names", lambda: set())())
        if extra_iris_names:
            system_prompt += (
                "\n\n# 记忆工具\n"
                "你可以调用 "
                + "、".join(f"`{name}`" for name in extra_iris_names)
                + "：评论/回复前可查一次用户画像或相关记忆（search_memory / get_profile），"
                "别反复查；需要补记忆可用 save_memory。看到 mumu_phone_recent 可回顾自己最近的操作。"
            )
        if "mumu_phone_lesson" in {tool.name for tool in tools.tools}:
            system_prompt += (
                "\n\n# 操作经验\n"
                "遇到踩坑或好用的操作技巧时，可调用 mumu_phone_lesson(situation, action) 记一条；"
                "收手时会整理成《操作教程总结》单独写入长期记忆（可选）。"
            )
        if "mumu_phone_record_start" in {tool.name for tool in tools.tools}:
            system_prompt += (
                "\n\n# 媒体辅助\n"
                "录制时仍可继续操作手机：mumu_phone_record_start/stop 后台录屏，停止时可让视觉/全模态模型转述画面；"
                "mumu_phone_hear_start/stop 后台录音（只录 MuMu 声音，不录 Windows 其它应用），停止时可 STT 转写/让音频模型描述声音；"
                "用 mumu_phone_media_status 查看当前可用的媒体能力。"
            )
        max_steps = int(util.clamp(int(self._cfg("autoplay_max_steps_per_round", 28)), 4, 120))
        resp = await self.context.tool_loop_agent(
            event=event,
            chat_provider_id=provider_id,
            prompt=prompt,
            tools=tools,
            system_prompt=system_prompt,
            max_steps=max_steps,
            tool_call_timeout=int(self._cfg("tool_call_timeout_sec", 150)),
        )
        text = (getattr(resp, "completion_text", "") or "").strip()
        return util.truncate(text, 500)

    @staticmethod
    async def _sleep_small(seconds: float) -> None:
        try:
            await asyncio.wait_for(asyncio.sleep(max(0.0, seconds)), timeout=max(0.05, seconds) + 5)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
