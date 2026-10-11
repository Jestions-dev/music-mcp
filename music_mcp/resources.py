# -*- coding: utf-8 -*-
"""
music-mcp 的资源层

资源分三类，全部通过一个 URI 命名空间暴露：

  music://theory/<文件名>       乐理库（AI_MIDI 的 20 篇 + 用户自定义）
  music://theory/_index         乐理库索引（按主题分类，便于挑选）
  music://state/<名字>          我们引擎的当前状态（乐谱/和声校验/密度/量测…）
  music://state/_index          状态资源索引

设计意图：原版 MCP 只能回答「乐理是什么」，这里还能回答「我们这首曲子现在什么样」。
最后一项是它比原版多出来的价值。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------- 路径
# 默认值指向本机实际位置；需要换机器/换目录时用环境变量覆盖（DSH 配置档里设），
# 免得改代码。见 cordis.patch.yml 的 mcp-music 条目。
import os as _os


def _env_paths(var: str, defaults: list[str]) -> list[Path]:
    raw = _os.environ.get(var, "").strip()
    if raw:
        return [Path(p) for p in raw.split(_os.pathsep) if p.strip()]
    return [Path(p) for p in defaults]


# 乐理库：AI_MIDI 安装目录（含内置 20 篇），可多目录
THEORY_DIRS = _env_paths("MUSIC_MCP_THEORY_DIRS", [
    r"C:\Users\Jestions\AppData\Local\Programs\AI_MIDI\Library",
    r"D:\ForDeepseek\MusicMaker\04_原创_破损的循环\参考_AI_MIDI\Library",
])
# 我们自己的笔记（Library/user 语义）
THEORY_USER = _env_paths("MUSIC_MCP_THEORY_USER", [
    r"D:\ForDeepseek\MusicMaker\乐理\user",
])[0]

# 项目根目录（工具层同样读这个）。默认值仅作兜底，实际由 server.py --project 定死。
PROJECT = _env_paths("MUSIC_MCP_PROJECT", [
    r"D:\ForDeepseek\MusicMaker",
])[0]

# 引擎状态：显式映射，不要把整个项目暴露出去。
# 状态目录与"项目根"是两回事：项目根可以是一个大目录树（如 D:\...\MusicMaker\），
# 而分析产物集中在一个子项目里。所以单独用 MUSIC_MCP_STATE_DIR 指定；
# 没设时按「项目根/分析 存在就用它，否则用项目根」推断，保证两边都能跑。
def _resolve_state_dir() -> Path:
    env = _os.environ.get("MUSIC_MCP_STATE_DIR", "").strip()
    if env:
        return Path(env)
    if (PROJECT / "分析").is_dir():
        return PROJECT
    return PROJECT


STATE_DIR = _resolve_state_dir()
STATE_FILES = {
    "编曲设计":          STATE_DIR / "分析" / "编排信息.json",
    "验收数据":          STATE_DIR / "分析" / "前进的循环_验收.json",
    "对标基准":          STATE_DIR / "分析" / "对标_新.json",
    "母带参数":          STATE_DIR / "分析" / "母带日志.json",
    "配平记录":          STATE_DIR / "分析" / "配平.json",
    # 4.0 参考曲的和声分析（我们扒出来的调性/和弦证据）
    "参考4.0和声":       STATE_DIR / "分析" / "4.0和声v2.json",
    # YuE2 生成的 ABC 谱索引
    "YuE2乐谱索引":      STATE_DIR / "分析" / "yue2谱面解析.json",
}
# 逐文件的原始谱面（ABC），单独一项一项列出
YUE2_DIR = STATE_DIR / "yue2翻唱"

# 主题分类：让模型挑文件时不用先读一遍才知道有什么
THEORY_TOPICS = {
    "01_乐理基础.md": "音名/音程/音阶/调号等基础定义",
    "02_配和弦指南.md": "为旋律配和声的方法",
    "04_转音设计指南.md": "装饰音与花腔",
    "05_作曲编曲通用技巧.md": "通用写作手法",
    "06_和弦进阶与风格化.md": "七和弦/延伸音/风格化和声",
    "07_音阶与即兴创作模板.md": "各调式音阶与即兴套路",
    "08_和弦进行词典.md": "现成的和弦走向库",
    "09_音域运用与音程写作.md": "各乐器音域与音程选择",
    "10_旋律写作与记忆点.md": "旋律动机发展与hook设计",
    "11_节奏与律动.md": "节奏型/切分/groove/BPM与密度",
    "12_风格化写作与编曲要素.md": "各风格编曲要素",
    "13_调性识别与和弦功能分析.md": "功能分析（扒带用）",
    "14_MIDI真实感与演奏润色.md": "让MIDI听起来像真人演奏",
    "16_织体关系与声部配合.md": "声部层次与织体",
    "17_调式互换与转调.md": "借用和弦与转调",
    "18_对位与多声部写作.md": "对位法",
    "19_配器法入门.md": "乐器音域表与配器组合模板（含音区分层原则）",
    "20_曲式结构与段落设计.md": "曲式/段落对比/动态曲线/过渡手法",
}

_CACHE: dict[str, tuple[str, str]] = {}     # uri -> (mime, text)
_INDEX: list[dict] = []


# ---------------------------------------------------------------- 扫描
def _theory_root() -> Optional[Path]:
    for d in THEORY_DIRS:
        if d.is_dir():
            return d
    return None


def _scan_theory() -> list[dict]:
    out = []
    root = _theory_root()
    if root:
        for f in sorted(root.glob("*.md")):
            out.append({
                "uri": f"music://theory/{f.name}",
                "name": f.name,
                "title": THEORY_TOPICS.get(f.name, f.stem.split('_', 1)[-1]),
                "description": THEORY_TOPICS.get(f.name, "乐理知识"),
                "mimeType": "text/markdown",
                "path": f,
                "origin": "内置",
            })
        # 内置 txt 也带上
        for f in sorted(root.glob("*.txt")):
            out.append({
                "uri": f"music://theory/{f.name}", "name": f.name,
                "title": f.stem, "description": "乐理知识（纯文本）",
                "mimeType": "text/plain", "path": f, "origin": "内置",
            })
    # 我们自己的笔记（Library/user 语义）
    if THEORY_USER.is_dir():
        for f in sorted(THEORY_USER.rglob("*")):
            if f.suffix.lower() not in (".md", ".txt"):
                continue
            rel = f.relative_to(THEORY_USER).as_posix()
            out.append({
                "uri": f"music://theory/user/{rel}",
                "name": f"user/{rel}",
                "title": f.stem,
                "description": "本项目自建乐理笔记",
                "mimeType": "text/markdown" if f.suffix.lower() == ".md" else "text/plain",
                "path": f, "origin": "自建",
            })
    return out


def _scan_state() -> list[dict]:
    out = []
    for name, p in STATE_FILES.items():
        if p.is_file():
            out.append({
                "uri": f"music://state/{name}",
                "name": name,
                "title": name,
                "description": f"引擎状态：{p.name}",
                "mimeType": "application/json",
                "path": p, "origin": "本项目",
            })
    for f in sorted(YUE2_DIR.glob("*.abc")) if YUE2_DIR.is_dir() else []:
        out.append({
            "uri": f"music://state/yue2/{f.name}",
            "name": f"yue2/{f.name}",
            "title": f"YuE2 生成的谱面 {f.stem}",
            "description": "YuE2 翻唱生成的 ABC 乐谱（可读，含和弦标记）",
            "mimeType": "text/plain", "path": f, "origin": "YuE2",
        })
    return out


def refresh() -> None:
    """重扫资源（initialize 与每次 list 前调用，保证文件新增后可见）。"""
    global _INDEX, _CACHE
    _INDEX = _scan_theory() + _scan_state()
    _CACHE = {}


# ---------------------------------------------------------------- 对外
def list_resources() -> list[dict]:
    refresh()
    return [{k: v for k, v in r.items() if k in
             ("uri", "name", "title", "description", "mimeType")}
            for r in _INDEX]


def list_templates() -> list[dict]:
    return [
        {"uriTemplate": "music://theory/{filename}",
         "name": "乐理知识文件",
         "description": "按文件名读取乐理库（如 19_配器法入门.md）",
         "mimeType": "text/markdown"},
        {"uriTemplate": "music://state/{name}",
         "name": "引擎状态",
         "description": "读取本项目引擎的分析结果与谱面",
         "mimeType": "application/json"},
    ]


def read_resource(uri: str) -> Optional[tuple[str, str]]:
    if uri.endswith("/_index"):
        if uri.startswith("music://theory"):
            return ("application/json", json.dumps(
                [{"name": r["name"], "title": r["title"], "description": r["description"],
                  "origin": r["origin"]} for r in _INDEX if r["uri"].startswith("music://theory")],
                ensure_ascii=False, indent=2))
        if uri.startswith("music://state"):
            return ("application/json", json.dumps(
                [{"name": r["name"], "description": r["description"]}
                 for r in _INDEX if r["uri"].startswith("music://state")],
                ensure_ascii=False, indent=2))

    if uri in _CACHE:
        return _CACHE[uri]

    refresh()
    for r in _INDEX:
        if r["uri"] == uri:
            try:
                text = r["path"].read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                return ("text/plain", f"读取失败: {type(e).__name__}: {e}")
            got = (r["mimeType"], text)
            _CACHE[uri] = got
            return got
    return None


def summary() -> dict:
    refresh()
    th = [r for r in _INDEX if r["uri"].startswith("music://theory")]
    st = [r for r in _INDEX if r["uri"].startswith("music://state")]
    return {"乐理": len(th), "状态": len(st),
            "自建笔记": len([r for r in th if r["origin"] == "自建"])}
