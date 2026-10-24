# -*- coding: utf-8 -*-
"""插件自带的本地长期记忆：手机使用记录 / 操作技巧 + 语义检索。

设计目标：
- 不依赖 iris 记忆插件也能保存/检索本插件产生的经验；
- iris 关掉（或不可用）时作为唯一记忆后端；
- iris 开着时仍然额外存一份本地备份，便于审计/迁移；
- 语义检索优先用 AstrBot 的 Embedding Provider；没有 Provider 时退化为
  关键词/字符 bigram 重叠检索，保证永远有可用结果。
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from pathlib import Path
from typing import Any

from . import util

KIND_LABELS = {
    "session": "使用记录",
    "lesson": "操作技巧",
    "note": "备注",
}

KIND_ALIASES = {
    "session": "session",
    "summary": "session",
    "record": "session",
    "lesson": "lesson",
    "tip": "lesson",
    "skill": "lesson",
    "note": "note",
    "all": "",
    "": "",
}


class LocalMemoryStore:
    """轻量本地记忆：JSON 持久化 + 可选向量语义检索。"""

    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self.config = plugin.config
        self.logger = getattr(plugin, "logger", None)
        self.dir = Path(plugin.data_dir) / "local_memory"
        util.ensure_dir(self.dir)
        self.path = self.dir / "memory.json"
        self._entries: list[dict[str, Any]] | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ 配置
    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        if value is None or value == "":
            return default
        return value

    def enabled(self) -> bool:
        return bool(self._cfg("local_memory_enable", True))

    def max_entries(self) -> int:
        return int(util.clamp(int(self._cfg("local_memory_max_entries", 1000)), 20, 10000))

    # ------------------------------------------------------------------ 读写
    def _load(self) -> list[dict[str, Any]]:
        if self._entries is not None:
            return self._entries
        entries: list[dict[str, Any]] = []
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    entries = [item for item in data if isinstance(item, dict) and item.get("text")]
        except Exception as exc:
            try:
                self.plugin._diag(f"local_memory 读取失败: {util.truncate(repr(exc), 160)}")
            except Exception:
                pass
            entries = []
        # 避免重复句柄；按时间排序
        entries.sort(key=lambda item: float(item.get("ts") or 0))
        self._entries = entries
        return entries

    def _save(self) -> None:
        try:
            util.ensure_dir(self.dir)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._entries or [], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except Exception as exc:
            try:
                self.plugin._diag(f"local_memory 写入失败: {util.truncate(repr(exc), 160)}")
            except Exception:
                pass

    # ------------------------------------------------------------------ 向量
    def _embedding_provider(self):
        manager = getattr(getattr(self.plugin, "context", None), "provider_manager", None)
        if manager is None:
            return None
        insts = list(getattr(manager, "embedding_provider_insts", []) or [])
        wanted = str(self._cfg("local_memory_embedding_provider_id", "") or "").strip()
        if wanted:
            for provider in insts:
                config = getattr(provider, "provider_config", {}) or {}
                if str(config.get("id") or "") == wanted:
                    return provider
                try:
                    if provider.meta().id == wanted:
                        return provider
                except Exception:
                    pass
        return insts[0] if insts else None

    async def _embed(self, text: str) -> list[float] | None:
        provider = self._embedding_provider()
        if provider is None:
            return None
        try:
            vector = await asyncio.wait_for(provider.get_embedding(text), timeout=25)
            if not vector:
                return None
            return [float(item) for item in vector]
        except Exception as exc:
            try:
                self.plugin._diag(
                    f"local_memory embedding 失败（{type(provider).__name__}）: "
                    f"{util.truncate(repr(exc), 160)}"
                )
            except Exception:
                pass
            return None

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        dot = sum(a * b for a, b in zip(left, right))
        norm_left = math.sqrt(sum(a * a for a in left))
        norm_right = math.sqrt(sum(b * b for b in right))
        if not norm_left or not norm_right:
            return 0.0
        return dot / (norm_left * norm_right)

    @staticmethod
    def _lexical_score(query: str, text: str) -> float:
        """关键词 / 字符 bigram 重叠，embedding 不可用时的兜底。"""
        q = re.sub(r"\s+", "", (query or "").lower())
        t = re.sub(r"\s+", "", (text or "").lower())
        if not q or not t:
            return 0.0
        words = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]{1,4}", q))
        hits = sum(1 for word in words if word and word in t)
        word_score = hits / max(1, len(words))
        q_grams = {q[index : index + 2] for index in range(max(0, len(q) - 1))}
        t_grams = {t[index : index + 2] for index in range(max(0, len(t) - 1))}
        gram_score = len(q_grams & t_grams) / max(1, len(q_grams))
        return max(word_score, gram_score * 0.8)

    # ------------------------------------------------------------------ 公开 API
    @staticmethod
    def normalize_kind(kind: str) -> str:
        return KIND_ALIASES.get(str(kind or "").strip().lower(), "note")

    async def add(
        self,
        kind: str,
        text: str,
        tags: list[str] | None = None,
        source: str = "",
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """保存一条本地记忆（自动附带 embedding，失败则只存文本）。"""
        if not self.enabled():
            return {}
        content = (text or "").strip()
        if not content:
            return {}
        kind = self.normalize_kind(kind) or "note"
        embedding = await self._embed(content)
        entry: dict[str, Any] = {
            "id": f"mem_{int(time.time() * 1000)}_{len(content) % 1000}",
            "ts": time.time(),
            "kind": kind,
            "text": content,
            "tags": [str(tag).strip() for tag in (tags or []) if str(tag).strip()],
            "source": str(source or ""),
            "umo": str(getattr(getattr(self.plugin, "state", None), "owner_umo", "") or ""),
            "embedding": embedding,
        }
        if meta:
            entry["meta"] = meta
        async with self._lock:
            entries = self._load()
            entries.append(entry)
            limit = self.max_entries()
            if len(entries) > limit:
                # 保留最新的 limit 条，但尽量保留操作技巧
                lessons = [item for item in entries if item.get("kind") == "lesson"]
                others = [item for item in entries if item.get("kind") != "lesson"]
                keep_lessons = lessons[-max(1, limit // 3) :]
                keep_others = others[-(limit - len(keep_lessons)) :]
                self._entries = sorted(
                    keep_lessons + keep_others, key=lambda item: float(item.get("ts") or 0)
                )
            self._save()
        try:
            self.plugin._diag(
                f"local_memory 新增 kind={kind} chars={len(content)} "
                f"embedding={'有' if embedding else '无'} total={self.count()}"
            )
        except Exception:
            pass
        return entry

    async def search(
        self,
        query: str,
        top_k: int = 5,
        kinds: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """语义检索（embedding）或关键词兜底；返回带 score 的条目。"""
        if not self.enabled():
            return []
        entries = list(self._load())
        if not entries:
            return []
        wanted = {self.normalize_kind(kind) for kind in (kinds or []) if self.normalize_kind(kind)}
        if wanted:
            entries = [item for item in entries if item.get("kind") in wanted]
        if not entries:
            return []
        text = (query or "").strip()
        if not text:
            return self.recent(top_k, kinds=kinds)
        query_embedding = await self._embed(text)
        scored: list[tuple[float, dict[str, Any]]] = []
        for entry in entries:
            embedding = entry.get("embedding")
            if query_embedding and isinstance(embedding, list) and embedding:
                score = self._cosine(query_embedding, embedding)
                method = "semantic"
            else:
                score = self._lexical_score(text, str(entry.get("text") or ""))
                method = "keyword"
            if score <= 0:
                continue
            item = dict(entry)
            item["score"] = round(float(score), 4)
            item["method"] = method
            scored.append((float(score), item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _score, item in scored[:top_k]]

    def recent(self, limit: int = 10, kinds: list[str] | None = None) -> list[dict[str, Any]]:
        entries = list(self._load())
        wanted = {self.normalize_kind(kind) for kind in (kinds or []) if self.normalize_kind(kind)}
        if wanted:
            entries = [item for item in entries if item.get("kind") in wanted]
        return [dict(item) for item in reversed(entries[-max(1, int(limit)) :])]

    def count(self) -> int:
        return len(self._load())

    def stats_text(self) -> str:
        entries = self._load()
        total = len(entries)
        sessions = len([item for item in entries if item.get("kind") == "session"])
        lessons = len([item for item in entries if item.get("kind") == "lesson"])
        provider = self._embedding_provider()
        provider_name = ""
        if provider is not None:
            try:
                provider_name = provider.meta().model or provider.meta().id
            except Exception:
                provider_name = type(provider).__name__
        semantic = f"可用（{provider_name or 'Embedding Provider'}）" if provider else "不可用，使用关键词兜底"
        return (
            f"本地记忆：{total} 条（使用记录 {sessions} / 操作技巧 {lessons}）；"
            f"语义检索：{semantic}"
        )

    @staticmethod
    def format_entries(entries: list[dict[str, Any]], max_chars: int = 900) -> str:
        lines: list[str] = []
        for item in entries:
            kind = KIND_LABELS.get(str(item.get("kind") or ""), "记忆")
            timestamp = time.strftime("%m-%d %H:%M", time.localtime(float(item.get("ts") or 0)))
            score = item.get("score")
            prefix = f"[{kind} {timestamp}"
            if score is not None:
                prefix += f" 相关度{float(score):.2f}"
            prefix += "]"
            text = str(item.get("text") or "").strip().replace("\n", " ")
            lines.append(f"{prefix} {text}")
            if len("\n".join(lines)) >= max_chars:
                break
        return util.truncate("\n".join(lines), max_chars)
