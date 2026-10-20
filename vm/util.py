# -*- coding: utf-8 -*-
"""MuMu 安卓虚拟机插件 - 通用工具。"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

IS_WINDOWS = os.name == "nt"

CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


def ts() -> float:
    return time.time()


def now_str(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return time.strftime(fmt, time.localtime())


def hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}小时{m}分{s}秒"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def truncate(text: str, limit: int, suffix: str = "…[已截断]") -> str:
    if text is None:
        return ""
    text = str(text)
    if limit > 0 and len(text) > limit:
        return text[:limit] + suffix
    return text


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    try:
        ensure_dir(path.parent)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(path)
    except Exception:
        pass


async def run_process(
    args: list[str],
    *,
    timeout: float = 60,
    input_bytes: bytes | None = None,
    cwd: str | None = None,
    env: dict | None = None,
) -> tuple[int, bytes, bytes]:
    """异步执行外部命令，返回 (returncode, stdout, stderr)。

    使用 CREATE_NO_WINDOW，避免 adb / MuMuManager 在桌面弹黑框。
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if input_bytes is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        creationflags=CREATE_NO_WINDOW,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=input_bytes), timeout=timeout
        )
    except asyncio.TimeoutError:
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return -1, b"", f"[超时 {timeout}s]".encode("utf-8")
    return proc.returncode or 0, stdout or b"", stderr or b""


def decode(b: bytes) -> str:
    if not b:
        return ""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


def b64_png(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def file_sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def find_first_existing(candidates: list[str | None]) -> str | None:
    for c in candidates:
        if not c:
            continue
        p = Path(c).expanduser()
        if p.is_file():
            return str(p)
    return None


def which(name: str) -> str | None:
    return shutil.which(name)


def guess_mumu_manager() -> str | None:
    """自动定位 MuMuManager.exe / adb.exe。"""
    env = os.environ.get("MUMU_MANAGER_PATH")
    candidates = [
        env,
        r"C:\Program Files\Netease\MuMu\nx_main\MuMuManager.exe",
        r"C:\Program Files\Netease\MuMuPlayer-12.0\shell\MuMuManager.exe",
        r"C:\Program Files\Netease\MuMuPlayer-12.0\MuMuManager.exe",
        r"C:\Program Files\MuMuPlayer-12.0\shell\MuMuManager.exe",
        r"D:\Program Files\Netease\MuMu\nx_main\MuMuManager.exe",
    ]
    hit = find_first_existing(candidates)
    if hit:
        return hit
    # 宽松搜索：常见安装根目录下找 nx_main/MuMuManager.exe 或 shell/MuMuManager.exe
    roots = [
        Path(r"C:\Program Files\Netease"),
        Path(r"C:\Program Files"),
        Path(r"D:\Program Files"),
        Path(r"D:\Netease"),
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
    ]
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for pattern in ("**/MuMuManager.exe", "**/MuMuManager/MuMuManager.exe"):
                for path in root.glob(pattern):
                    if path.is_file():
                        return str(path)
        except Exception:
            continue
    return which("MuMuManager")


def guess_adb(manager_path: str | None) -> str | None:
    env = os.environ.get("ADB_PATH")
    candidates = [env]
    if manager_path:
        d = Path(manager_path).parent
        candidates += [
            str(d / "adb.exe"),
            str(d.parent / "adb.exe"),
            str(d / "MuMuManager.exe"),
        ]
    candidates += [
        r"C:\Program Files\Netease\MuMu\nx_main\adb.exe",
        r"C:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe",
    ]
    hit = find_first_existing(candidates)
    if hit:
        return hit
    return which("adb")


def guess_ffmpeg(configured: str = "") -> str | None:
    candidates = [
        configured,
        os.environ.get("FFMPEG_PATH"),
        which("ffmpeg"),
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\Users\Public\ffmpeg\bin\ffmpeg.exe",
    ]
    return find_first_existing(candidates)


def cleanup_dir(path: Path, keep: int = 300, ext: tuple[str, ...] = (".png", ".jpg")) -> None:
    """按修改时间清理目录，只保留最近 keep 个截图。"""
    try:
        files = [p for p in path.iterdir() if p.is_file() and p.suffix.lower() in ext]
        if len(files) <= keep:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for p in files[: len(files) - keep]:
            with contextlib.suppress(Exception):
                p.unlink()
    except Exception:
        pass


def escape_adb_input_text(text: str) -> str:
    """adb shell input text 的转义（仅 ASCII 兜底方案用）。"""
    out = text.replace("%", "\\%").replace(" ", "%s")
    for ch in ("&", "<", ">", "|", ";", "(", ")", "$", "'", '"', "\\", "`"):
        out = out.replace(ch, "\\" + ch)
    return out
