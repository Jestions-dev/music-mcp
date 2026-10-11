# -*- coding: utf-8 -*-
"""
music_mcp —— 面向 AI 编曲的 MCP 服务器（纯标准库，零依赖）

与 AI_MIDI 自带的 MCP 服务器**并存**：
  * aimidi  —— 提供 7 个工具（写谱/解析/文件管理），但只声明 tools，
               resources/list 返回 -32601
  * music   —— 本包，补上 resources（乐理库 + 本项目引擎状态），
               另加三个 AI_MIDI 做不到的工具（轨道级解析/摘要/校验）

为什么不用 Go 改 AI_MIDI：本机没有 Go 工具链，项目要 Go 1.27.0，
为了补 resources 去装整套工具链并长期跟上游，代价不成比例。

入口：python -m music_mcp.server
"""
from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["server", "resources", "tools"]
