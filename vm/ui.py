# -*- coding: utf-8 -*-
"""uiautomator XML 解析：把界面压缩成 Agent 友好的可点击元素列表。"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

_SHORT_CLASS = {
    "android.widget.TextView": "文本",
    "android.widget.EditText": "输入框",
    "android.widget.Button": "按钮",
    "android.widget.ImageView": "图片",
    "android.widget.ImageButton": "图标按钮",
    "android.widget.Switch": "开关",
    "android.widget.CheckBox": "复选",
    "android.widget.RadioButton": "单选",
    "android.widget.SeekBar": "滑条",
    "android.widget.Spinner": "下拉",
    "android.widget.RecyclerView": "列表",
    "android.widget.ScrollView": "滚动区",
    "android.widget.LinearLayout": "布局",
    "android.widget.FrameLayout": "布局",
    "android.widget.RelativeLayout": "布局",
    "android.view.View": "视图",
    "android.view.ViewGroup": "布局",
    "androidx.recyclerview.widget.RecyclerView": "列表",
    "androidx.viewpager.widget.ViewPager": "翻页",
    "android.webkit.WebView": "网页",
}

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


@dataclass
class UiNode:
    index: int
    cls: str
    short_cls: str
    text: str = ""
    desc: str = ""
    rid: str = ""
    hint: str = ""
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    clickable: bool = False
    long_clickable: bool = False
    scrollable: bool = False
    editable: bool = False
    checkable: bool = False
    checked: bool = False
    selected: bool = False
    enabled: bool = True
    package: str = ""
    depth: int = 0

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    @property
    def width(self) -> int:
        return max(0, self.bounds[2] - self.bounds[0])

    @property
    def height(self) -> int:
        return max(0, self.bounds[3] - self.bounds[1])

    def label(self) -> str:
        if self.text:
            return self.text
        if self.desc:
            return self.desc
        if self.hint:
            return self.hint
        return ""

    def to_dict(self) -> dict[str, Any]:
        x, y = self.center
        data: dict[str, Any] = {
            "i": self.index,
            "c": self.short_cls,
        }
        label = self.label()
        if label:
            data["t"] = label
        if self.rid:
            data["id"] = self.rid
        data["b"] = list(self.bounds)
        data["p"] = [x, y]
        flags = []
        if self.clickable:
            flags.append("可点")
        if self.long_clickable:
            flags.append("可长按")
        if self.scrollable:
            flags.append("可滚动")
        if self.editable:
            flags.append("可输入")
        if self.checkable:
            flags.append("选中" if self.checked else "未选")
        if self.selected:
            flags.append("当前")
        if not self.enabled:
            flags.append("禁用")
        if flags:
            data["f"] = flags
        return data


@dataclass
class UiSnapshot:
    xml: str
    rotation: int
    nodes: list[UiNode] = field(default_factory=list)
    package: str = ""
    raw_count: int = 0

    def summary(self, max_items: int = 60, only_interactive: bool = True) -> list[dict[str, Any]]:
        result = []
        for node in self.nodes:
            if len(result) >= max_items:
                break
            if only_interactive and not (
                node.clickable
                or node.long_clickable
                or node.scrollable
                or node.editable
                or node.label()
            ):
                continue
            if not only_interactive and not (node.label() or node.clickable or node.scrollable):
                continue
            result.append(node.to_dict())
        return result

    # ------------------------------------------------------------------ 查找
    def find(
        self,
        text: str = "",
        index: int | None = None,
        *,
        prefer_clickable: bool = True,
    ) -> UiNode | None:
        if index is not None:
            for node in self.nodes:
                if node.index == int(index):
                    return node
            if 0 <= int(index) < len(self.nodes):
                return self.nodes[int(index)]
            return None
        text = (text or "").strip()
        if not text:
            return None

        def score(node: UiNode) -> tuple:
            label = node.label()
            if label == text:
                base = 0
            elif label.lower() == text.lower():
                base = 1
            elif text in label:
                base = 2
            elif label and label in text:
                base = 3
            else:
                base = 9
            return (
                base,
                0 if (prefer_clickable and node.clickable) else 1,
                node.depth,
                node.width * node.height,
            )

        candidates = [n for n in self.nodes if n.label()]
        candidates = [n for n in candidates if score(n)[0] < 9]
        if not candidates:
            # 内容也可能在 resource-id 里
            candidates = [
                n for n in self.nodes if text.lower() in (n.rid or "").lower()
            ]
        if not candidates:
            return None
        candidates.sort(key=score)
        return candidates[0]


def parse_ui_xml(xml: str) -> UiSnapshot:
    xml = (xml or "").strip()
    if not xml:
        raise ValueError("空的 UI XML")
    root = ET.fromstring(xml)
    rotation = int(root.attrib.get("rotation", "0") or 0)
    package = ""
    nodes: list[UiNode] = []

    def walk(element: ET.Element, depth: int = 0) -> None:
        nonlocal package
        attrib = element.attrib
        cls = attrib.get("class", "")
        pkg = attrib.get("package", "")
        if not package and pkg:
            package = pkg
        bounds = (0, 0, 0, 0)
        match = _BOUNDS_RE.search(attrib.get("bounds", ""))
        if match:
            bounds = tuple(int(v) for v in match.groups())  # type: ignore[assignment]
        rid = attrib.get("resource-id", "") or ""
        if rid.startswith("android:"):
            rid = rid[len("android:"):]
        node = UiNode(
            index=len(nodes),
            cls=cls,
            short_cls=_SHORT_CLASS.get(cls, cls.split(".")[-1][:12] if cls else "节点"),
            text=(attrib.get("text", "") or "").strip(),
            desc=(attrib.get("content-desc", "") or "").strip(),
            rid=rid,
            hint=(attrib.get("hint", "") or "").strip(),
            bounds=bounds,
            clickable=attrib.get("clickable") == "true",
            long_clickable=attrib.get("long-clickable") == "true",
            scrollable=attrib.get("scrollable") == "true",
            editable=("EditText" in cls) or attrib.get("focusable") == "true" and "EditText" in cls,
            checkable=attrib.get("checkable") == "true",
            checked=attrib.get("checked") == "true",
            selected=attrib.get("selected") == "true",
            enabled=attrib.get("enabled") != "false",
            package=pkg,
            depth=depth,
        )
        nodes.append(node)
        for child in list(element):
            if child.tag == "node":
                walk(child, depth + 1)

    for child in list(root):
        if child.tag == "node":
            walk(child, 1)
    return UiSnapshot(xml=xml, rotation=rotation, nodes=nodes, package=package, raw_count=len(nodes))
