# -*- coding: utf-8 -*-
"""
music-mcp —— 面向 AI 编曲的 MCP 服务器（纯标准库，零依赖）

与 AI_MIDI 自带 MCP 层的区别：
  * 它只声明 tools（resources/list 返回 -32601）、工具失败不标 isError、只能写单轨
  * 本服务器补齐 resources、isError、多轨 Format 1，并把它的 7 个工具一并实现

再多一层：**把项目的引擎状态也暴露成 Resource** —— 编排信息、验收数据、
母带参数、YuE2 的 ABC 谱。原版只能回答"乐理是什么"，这里还能回答
"我们这首曲子现在什么样"。

协议：JSON-RPC 2.0 over stdio，单行消息
支持：initialize / tools/list / tools/call
      resources/list / resources/templates/list / resources/read / ping
      notifications/tools/list_changed、notifications/resources/list_changed

启动：
    python -m music_mcp.server                     # 用环境变量
    python -m music_mcp.server --project D:\\音乐   # 启动时定死项目地址

「启动时定死」的含义：--project 在进程启动时确定一次，之后所有工具调用
（parse_midi / create_midi / list_midi_files …）都用它，**不提供按调用传项目的参数**。
想同时服务两个项目就起两个实例、给不同的 serverName。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

__version__ = "1.1.0"

SERVER_NAME = "music-mcp"

# 支持的协议版本（新→旧）。协商规则见 MCP 规范 basic/lifecycle：
# 客户端在 initialize 里发它支持的版本；服务端若也支持就回同一个，
# 否则回自己支持的最新版，由客户端决定是否断开。
# 注意：AI_MIDI 的实现硬编码 "2024-11-05"，我们不复刻这个行为。
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]


# ---------------------------------------------------------------- 启动参数
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="music-mcp",
        description="面向 AI 编曲的 MCP 服务器（stdio）。项目地址在启动时确定。")
    p.add_argument("--project", metavar="DIR",
                   help="项目根目录：所有 MIDI 读写都在它下面。优先级高于 MUSIC_MCP_PROJECT")
    p.add_argument("--state-dir", metavar="DIR",
                   help="引擎状态资源所在目录（默认跟随 --project）")
    p.add_argument("--theory", metavar="DIRS",
                   help="乐理知识库目录，多个用 ; 分隔")
    p.add_argument("--project-must-exist", action="store_true",
                   help="项目目录不存在时直接退出（默认是自动创建）")
    p.add_argument("--exclude", metavar="NAMES", action="append", default=[],
                   help="扫描时跳过的目录名（可重复；如 --exclude .git --exclude node_modules）")
    return p.parse_args(argv)


def resolve_project(args: argparse.Namespace) -> Path:
    """
    定死本次进程的项目目录，并把结果写回环境变量 ——
    工具层与资源层都从环境变量读，这样它们拿到的就是"启动时定死"的那个值，
    不存在按调用切换项目的口子。
    """
    raw = (args.project
           or os.environ.get("MUSIC_MCP_PROJECT")
           or os.environ.get("AI_MIDI_OUTPUT_DIR")
           or os.getcwd())
    path = Path(raw).expanduser().resolve()

    if path.exists() and not path.is_dir():
        raise SystemExit(f"--project 指向的不是目录：{path}")
    if not path.exists():
        if args.project_must_exist:
            raise SystemExit(f"项目目录不存在（--project-must-exist）：{path}")
        path.mkdir(parents=True, exist_ok=True)

    os.environ["MUSIC_MCP_PROJECT"] = str(path)
    os.environ["AI_MIDI_OUTPUT_DIR"] = str(path)      # 兼容旧工具层的读取顺序

    if args.state_dir:
        os.environ["MUSIC_MCP_STATE_DIR"] = str(Path(args.state_dir).expanduser().resolve())
    if args.theory:
        os.environ["MUSIC_MCP_THEORY_DIRS"] = args.theory

    # 默认跳过常见噪音目录：项目根一旦放大到父目录，克隆来的仓库/依赖会把
    # demo 素材也算成"项目里的 MIDI"
    ex = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
    ex.update(args.exclude or [])
    os.environ["MUSIC_MCP_EXCLUDE"] = os.pathsep.join(sorted(ex))
    return path


# ---------------------------------------------------------------- 基础
def write_msg(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def ok(rid, result):
    write_msg({"jsonrpc": "2.0", "id": rid, "result": result})


def err(rid, code: int, message: str):
    write_msg({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def notify(method: str, params: dict | None = None) -> None:
    """发 JSON-RPC 通知（无 id，不需要响应）"""
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    write_msg(msg)


def run(argv: list[str] | None = None) -> int:
    """
    跑主循环，返回退出码（0 = 正常结束）。

    `pyproject.toml` 的 [project.scripts] 直接指向本函数：setuptools 生成的
    包装层会把它当成 `sys.exit(run())` 使用，所以这里返回 int 就够了。
    argv 省略时由 argparse 读 sys.argv（console script 会带上启动参数）。
    """
    args = parse_args(argv)
    project = resolve_project(args)

    from . import resources as R
    from . import tools as T

    # 预扫描一次，initialize 时就能报出资源数量
    R.refresh()

    # 启动日志走 stderr：stdout 是 JSON-RPC 通道，不能污染
    print(f"[music-mcp] 项目目录：{project}", file=sys.stderr, flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = req.get("method")
        rid = req.get("id")
        params = req.get("params") or {}

        try:
            if method == "initialize":
                # 版本协商：客户端发它支持的版本，我们回一个双方都认的
                want = str(params.get("protocolVersion") or "")
                ver = want if want in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
                ok(rid, {
                    "protocolVersion": ver,
                    "capabilities": {
                        # listChanged 与实际行为一致：初始化完成后我们发一次
                        # notifications/tools/list_changed（工具集随代码更新而变）
                        "tools": {"listChanged": True},
                        "resources": {"subscribe": False, "listChanged": True},
                    },
                    "serverInfo": {"name": SERVER_NAME, "version": __version__},
                })

            elif method in ("notifications/initialized", "initialized"):
                # 客户端确认初始化完成。按 MCP 规范，能力里声明了 listChanged 才可发
                # 对应通知；我们两个都声明了 true，所以都发一次：
                #   · tools/list_changed    —— 工具集随代码更新而变
                #   · resources/list_changed —— Library 目录下可能新增/删除知识文件
                notify("notifications/tools/list_changed")
                notify("notifications/resources/list_changed")
                continue

            # ---------------- resources ----------------
            elif method == "resources/list":
                ok(rid, {"resources": R.list_resources()})

            elif method == "resources/templates/list":
                ok(rid, {"resourceTemplates": R.list_templates()})

            elif method == "resources/read":
                uri = params.get("uri", "")
                got = R.read_resource(uri)
                if got is None:
                    err(rid, -32602, f"未知资源: {uri}")
                else:
                    mime, text = got
                    ok(rid, {"contents": [{"uri": uri, "mimeType": mime, "text": text}]})

            # ---------------- tools ----------------
            elif method == "tools/list":
                ok(rid, {"tools": T.list_tools()})

            elif method == "tools/call":
                name = params.get("name", "")
                args = params.get("arguments") or {}
                # 规范：工具"执行"错误用 result.isError=true 表示（不是 JSON-RPC error），
                # 这样模型才分得清成功/失败。AI_MIDI 的实现没有这个标记，我们补上。
                if hasattr(T, "call_tool_ex"):
                    text, is_err = T.call_tool_ex(name, args)
                else:
                    text, is_err = T.call_tool(name, args), False
                result = {"content": [{"type": "text", "text": text}]}
                if is_err:
                    result["isError"] = True
                ok(rid, result)

            elif method in ("ping",):
                ok(rid, {})

            else:
                err(rid, -32601, "Method not found")

        except Exception as e:  # 任何内部异常都回成 JSON-RPC 错误，别让服务器死掉
            err(rid, -32603, f"{type(e).__name__}: {e}")

    return 0


def main() -> None:
    """`python -m music_mcp.server` 的入口，转调 run()"""
    raise SystemExit(run())


if __name__ == "__main__":
    main()
