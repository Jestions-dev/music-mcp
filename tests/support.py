# -*- coding: utf-8 -*-
"""
测试支撑：起一个 music-mcp 服务器子进程，走真实 JSON-RPC。

用 unittest（标准库），不引入 pytest —— 本包的卖点就是零依赖，
为了测试装 pytest 等于自我否定。

注意两个 Windows 上的坑（都踩过）：
  · 直接替换 os.Stdin/os.Stdout + os.Pipe 会死锁 → 一律走子进程 + 管道
  · 控制台默认 GBK，需要 PYTHONIOENCODING=utf-8 才不会炸在 ✓ ⚠ 上
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_NAME = "music_mcp"

# 优先用已安装的 AI_MIDI Library；没有就跳过依赖它的测试
CANDIDATE_LIBS = [
    Path(r"C:\Users\Jestions\AppData\Local\Programs\AI_MIDI\Library"),
    REPO_ROOT / "Library",
]


def find_library() -> Path | None:
    for p in CANDIDATE_LIBS:
        if p.is_dir() and any(p.glob("*.md")):
            return p
    return None


class Server:
    """一个跑在子进程里的 music-mcp，提供 call/tool 两个方法"""

    def __init__(self, project_dir: str | Path, library_dir: str | Path | None = None,
                 cwd: str | Path | None = None, extra_args: list[str] | None = None,
                 state_dir: str | Path | None = None):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["AI_MIDI_OUTPUT_DIR"] = str(project_dir)
        env["MUSIC_MCP_PROJECT"] = str(project_dir)
        if library_dir:
            env["MUSIC_MCP_THEORY_DIRS"] = str(library_dir)
        if state_dir:
            env["MUSIC_MCP_STATE_DIR"] = str(state_dir)
        cmd = [sys.executable, "-m", f"{PKG_NAME}.server"] + list(extra_args or [])
        self.proc = subprocess.Popen(
            cmd, cwd=str(cwd or REPO_ROOT), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
        )
        self._id = 0
        self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "unittest", "version": "1"}})

    def call(self, method: str, params: dict | None = None) -> dict:
        """发一条 JSON-RPC 请求，读出属于它的那条响应"""
        self._id += 1
        rid = self._id
        self.proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}},
            ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError(f"{method} 无响应（服务器可能已退出）")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == rid:
                return msg

    def result(self, method: str, params: dict | None = None) -> dict:
        """要求成功，返回 result"""
        msg = self.call(method, params)
        if msg.get("error"):
            raise AssertionError(f"{method} 返回错误: {msg['error']}")
        if "result" not in msg:
            raise AssertionError(f"{method} 没有 result: {msg}")
        return msg["result"]

    def tool(self, _tool_name: str, **args) -> tuple[str, bool]:
        """调用工具，返回 (文本, 是否失败)。

        第一个参数刻意叫 _tool_name：避免与工具自己的 `name` 参数冲突
        （create_folder 等工具就有 name 参数）。
        """
        res = self.result("tools/call", {"name": _tool_name, "arguments": args})
        text = "".join(c.get("text", "") for c in res.get("content", []))
        return text, bool(res.get("isError"))

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        finally:
            # 关掉管道文件对象，否则解释器退出时报一堆 ResourceWarning
            for f in (self.proc.stdout, self.proc.stderr):
                try:
                    if f:
                        f.close()
                except Exception:
                    pass


def temp_project():
    """临时项目目录（测试产出不落到真实音乐项目里）"""
    return tempfile.TemporaryDirectory(prefix="music_mcp_test_")


NOTE_TABLE_3 = "\n".join([
    '[note: "C4", velocity: "80", start: "0", end: "1"]',
    '[note: "E4", velocity: "80", start: "1", end: "2"]',
    '[note: "G4", velocity: "80", start: "2", end: "3"]',
])

NOTE_TABLE_JSON = json.dumps([
    {"note": "C4", "velocity": 80, "start": 0, "end": 1},
    {"note": "E4", "velocity": 80, "start": 1, "end": 2},
])
