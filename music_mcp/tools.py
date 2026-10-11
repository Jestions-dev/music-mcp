# -*- coding: utf-8 -*-
"""
music-mcp 的工具层

**完整替代 AI_MIDI 自带的 MCP 服务器**（不再依赖 AI_MIDI.exe）。工具集分两部分：

  一、与 AI_MIDI 对齐的 7 个（参数名兼容，行为一致）
      read_library_file / list_midi_files / parse_midi / create_midi /
      delete_midi / create_folder / list_project_structure
      —— 其中 create_midi 额外支持多轨（Format 1），AI_MIDI 只能单轨。

  二、AI_MIDI 缺的 3 个（源码里核实过它做不到）
      1. 轨道结构：它的 parse_midi 返回扁平音符列表，NoteEvent 只有
         note/velocity/start/end（internal/midi/note_table.go:18-23），轨道号在
         解析时就被丢了（smf_reader.go 只按 channel+note 配对，不记 track）
         → parse_midi_tracks 给出 per-track 的音符、名称、音域、统计。
      2. 摘要：它只能整首吐出来，672 拍的曲子 = 103 KB 文本
         → parse_midi_summary 给"看谱"用的摘要。
      3. 校验回路：它的 create_midi 直接写文件，格式错要等生成完才发现，
         报错文案还是误导性的「未从 AI 回复中解析出任何音符」
         → validate_note_table 在写之前给出逐行诊断。

JSON-RPC 层在 server.py，资源层在 resources.py，音名基础层在 notes.py。
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import struct
from pathlib import Path
from typing import Optional

# 音名/取值范围来自共享基础层（single source of truth）
from .notes import (
    NOTE_NAMES, NOTE_TO_SEMITONE, NOTE_TO_SEMITONE as _NTS,  # noqa: F401
    MIDI_MIN, MIDI_MAX, VEL_MIN, VEL_MAX, BPM_MIN, BPM_MAX, TPB,
    midi_to_name, name_to_midi, clamp_midi, clamp_velocity, clamp_bpm,
)

TPB_FALLBACK = TPB              # 兼容旧引用
DANGLING_EPSILON = 1.0 / TPB    # AI_MIDI config.DanglingEpsilon

# 与 AI_MIDI internal/midi/note_table.go:26-29 完全一致
RE_NOTE = re.compile(r"note:\s*[^A-G]*([A-G][#b]*-?\d+)", re.I)
RE_VEL = re.compile(r"velocity:\s*[^\d.-]*([-\d.]+)", re.I)
RE_START = re.compile(r"start:\s*[^\d.-]*([-\d.]+)", re.I)
RE_END = re.compile(r"end:\s*[^\d.-]*([-\d.]+)", re.I)

# 大调音阶的半音结构（用于调性猜测）
MAJOR_STEPS = [0, 2, 4, 5, 7, 9, 11]

MIDI_EXT = {".mid", ".midi"}


# ---------------------------------------------------------------- note_table
def parse_note_table(text: str) -> tuple[list[dict], list[str]]:
    """
    解析 note_table。返回 (notes, problems)。
    顺序对齐 AI_MIDI ParseNoteTableLines：**先逐行正则**，没解析出音符才试 JSON
    （支持 ``` 围栏、数组或 {"notes": [...]} 两种 JSON 形状）。
    格式不符的行进 problems 而不是静默丢弃。
    """
    notes, problems = _parse_line_format(text)
    if notes:
        return notes, problems

    # 逐行解析没拿到音符才试 JSON —— 与 AI_MIDI ParseNoteTableLines 的顺序一致。
    # 反过来（先试 JSON）会把正常的 note_table 行格式误判成"JSON 解析失败"，
    # 因为行格式恰好以 [ 开头。
    jnotes, jproblem = _parse_json_format(text)
    if jnotes:
        return jnotes, problems + jproblem
    return [], problems + jproblem


def _parse_json_format(text: str) -> tuple[list[dict], list[str]]:
    """试按 JSON 解析（支持 ``` 围栏、数组或 {"notes": [...]}）"""
    notes: list[dict] = []
    problems: list[str] = []

    clean = _strip_fence(text)

    if not (RE_JSON_ARRAY.match(clean) or RE_JSON_OBJECT.match(clean)):
        return [], problems        # 不是 JSON（可能是行格式但一个音都没解析出来）

    try:
        data = json.loads(clean)
    except json.JSONDecodeError:
        return [], problems        # 不是 JSON 也不报错——交给行解析的结论

    if isinstance(data, dict):
        data = data.get("notes", [])
    if not isinstance(data, list):
        return [], ["JSON 顶层既不是数组也没有 notes 数组"]

    def num(v, default=None):
        if v is None:
            return default
        if isinstance(v, (int, float)):
            return float(v)
        try:
            return float(str(v).strip())
        except ValueError:
            return default

    for i, item in enumerate(data, 1):
        if not isinstance(item, dict):
            problems.append(f"JSON 第 {i} 项不是对象")
            continue
        note = str(item.get("note", "")).strip()
        if not note:
            problems.append(f"JSON 第 {i} 项缺少 note")
            continue
        st, en = num(item.get("start")), num(item.get("end"))
        if st is None or en is None:
            problems.append(f"JSON 第 {i} 项 start/end 无法解析")
            continue
        vel = num(item.get("velocity"), 80.0)
        st = max(0.0, st)
        if en < st:
            problems.append(f"JSON 第 {i} 项 end({en}) < start({st})，AI_MIDI 会丢弃它")
            continue
        notes.append({"note": note, "velocity": int(round(vel)), "start": st, "end": en})
    return notes, problems


# JSON 的形状：数组元素是 {，或对象含 "notes"。注意不能只看"以 [ 开头"——
# note_table 的行格式恰好也是 [ 开头（[note: ...]）。
RE_JSON_ARRAY = re.compile(r"^\[\s*[\{\]]")
RE_JSON_OBJECT = re.compile(r"^\{\s*[\"\}]")


def _strip_fence(text: str) -> str:
    c = (text or "").strip()
    if c.startswith("```"):
        lines = c.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        c = "\n".join(lines).strip()
    return c


def _looks_like_json(text: str) -> bool:
    """整体看起来是 JSON（用于决定报不报行级问题，避免把 JSON 误判成坏行）"""
    c = _strip_fence(text)
    return bool(RE_JSON_ARRAY.match(c) or RE_JSON_OBJECT.match(c))


def _parse_line_format(text: str) -> tuple[list[dict], list[str]]:
    """逐行正则提取（与 AI_MIDI ExtractFields 的正则一致）"""
    notes: list[dict] = []
    problems: list[str] = []
    # 整份是 JSON 时不逐行报问题：JSON 里的 "note":"C4" 会被半个正则匹配上，
    # 报出来的「第 N 行缺少字段」是假问题
    report_problems = not _looks_like_json(text)

    for lineno, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        mn, mv, ms, me = RE_NOTE.search(line), RE_VEL.search(line), RE_START.search(line), RE_END.search(line)
        if not all((mn, mv, ms, me)):
            if report_problems and "note:" in line.lower():
                missing = [n for n, m in (("note", mn), ("velocity", mv), ("start", ms), ("end", me)) if not m]
                problems.append(f"第 {lineno} 行缺少字段：{', '.join(missing)}")
            continue
        try:
            vel, st, en = float(mv.group(1)), float(ms.group(1)), float(me.group(1))
        except ValueError:
            if report_problems:
                problems.append(f"第 {lineno} 行数值无法解析")
            continue
        st = max(0.0, st)
        if en < st:
            if report_problems:
                problems.append(f"第 {lineno} 行 end({en}) < start({st})，AI_MIDI 会丢弃它")
            continue
        n = name_to_midi(mn.group(1))
        if n is None:
            if report_problems:
                problems.append(f"第 {lineno} 行音名无法识别：{mn.group(1)}")
            continue
        notes.append({"note": mn.group(1), "velocity": int(round(vel)), "start": st, "end": en})

    return notes, problems


def format_note(n: dict) -> str:
    """格式化为 AI_MIDI 认得的 note_table 行（写法与它 FormatNoteEvent 一致）"""
    def f(x: float) -> str:
        return str(int(x)) if float(x).is_integer() else repr(round(float(x), 6))
    return f'[note: "{n["note"]}", velocity: "{n["velocity"]}", start: "{f(n["start"])}", end: "{f(n["end"])}"]'


# ---------------------------------------------------------------- SMF 读取
def read_smf(data: bytes) -> dict:
    """
    纯标准库 SMF 解析，保留轨道结构。
    返回 {format, tpb, tracks:[{index, name, notes:[...]}]}
    """
    if len(data) < 14:
        raise ValueError(f"文件太短，不是完整的 MIDI（只有 {len(data)} 字节）")
    if data[:4] != b"MThd":
        raise ValueError(f"不是 MIDI 文件（头 4 字节是 {data[:4]!r}，应为 b'MThd'）")
    # MThd = 'MThd'(4) + length(4, 恒为 6) + format(2) + ntrks(2) + division(2)
    _hlen, fmt, ntrks, division = struct.unpack(">IHHH", data[4:14])
    pos = 8 + _hlen
    tracks = []

    for ti in range(ntrks):
        if data[pos:pos + 4] != b"MTrk":
            break
        length = struct.unpack(">I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + length]
        pos += 8 + length

        ticks = 0
        running = 0
        active: dict[tuple[int, int], tuple[int, int]] = {}
        events: list[tuple[int, int, int, int, int]] = []   # (tick, channel, note, vel, is_on)
        name = ""
        i = 0
        while i < len(body):
            # 变长时值
            delta = 0
            while i < len(body):
                b = body[i]; i += 1
                delta = (delta << 7) | (b & 0x7F)
                if not (b & 0x80):
                    break
            ticks += delta
            if i >= len(body):
                break
            status = body[i]
            if status & 0x80:
                i += 1
                running = status
            else:
                status = running

            if status == 0xFF:      # meta
                if i >= len(body):
                    break
                mtype = body[i]; i += 1
                ml = 0
                while i < len(body):
                    b = body[i]; i += 1
                    ml = (ml << 7) | (b & 0x7F)
                    if not (b & 0x80):
                        break
                payload = body[i:i + ml]; i += ml
                if mtype == 0x03 and not name:
                    name = payload.decode("utf-8", "replace").strip()
                elif mtype == 0x2F:
                    break
            elif status in (0xF0, 0xF7):   # sysex
                ml = 0
                while i < len(body):
                    b = body[i]; i += 1
                    ml = (ml << 7) | (b & 0x7F)
                    if not (b & 0x80):
                        break
                i += ml
            else:
                hi = status & 0xF0
                ch = status & 0x0F
                if hi in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
                    if i + 1 >= len(body):
                        break
                    d1, d2 = body[i], body[i + 1]; i += 2
                    if hi == 0x90 and d2 > 0:
                        events.append((ticks, ch, d1, d2, 1))
                    elif hi == 0x80 or (hi == 0x90 and d2 == 0):
                        events.append((ticks, ch, d1, 0, 0))
                elif hi in (0xC0, 0xD0):
                    i += 1
                else:
                    i += 1

        # 配对成音符
        notes = []
        for tick, ch, note, vel, is_on in events:
            key = (ch, note)
            if is_on:
                if key in active:
                    st, v0 = active.pop(key)
                    notes.append((st, tick, note, v0))
                active[key] = (tick, vel)
            else:
                if key in active:
                    st, v0 = active.pop(key)
                    notes.append((st, tick, note, v0))
        maxtick = max([t for t, *_ in events], default=0)
        for (ch, note), (st, v0) in active.items():
            en = maxtick if maxtick > st else st + 1
            notes.append((st, en, note, v0))

        notes.sort(key=lambda x: (x[0], x[1]))
        tracks.append({"index": ti, "name": name, "raw": notes})

    if division & 0x8000:
        tpb = TPB_FALLBACK     # SMPTE 时基：AI_MIDI 也回退默认值
    else:
        tpb = division or TPB_FALLBACK

    out = []
    for t in tracks:
        conv = [{
            "note": midi_to_name(n),
            "velocity": v,
            "start": round(s / tpb, 6),
            "end": round(max(e, s + 1) / tpb, 6),
        } for s, e, n, v in t["raw"]]
        out.append({"index": t["index"], "name": t["name"], "notes": conv})
    return {"format": fmt, "tpb": tpb, "tracks": out}


# ---------------------------------------------------------------- 音乐分析
def detect_key(names: list[str]) -> tuple[str, float, list[tuple[str, float]]]:
    """
    调性猜测：对 24 个大小调做音级覆盖打分（不涉及外部乐理库，是纯统计）。
    返回 (最佳调, 置信度, 前几名)
    """
    pcs = [n % 12 for n in (name_to_midi(x) for x in names) if n is not None]
    if not pcs:
        return "?", 0.0, []
    hist = [0] * 12
    for p in pcs:
        hist[p] += 1
    total = len(pcs)

    candidates = []
    for root in range(12):
        for mode, steps in (("大调", MAJOR_STEPS), ("小调", [(s + 9) % 12 for s in MAJOR_STEPS])):
            scale = {(root + s) % 12 for s in steps}
            inside = sum(hist[p] for p in scale)
            # 主音/属音/三音权重（粗糙但有效）
            tonic = hist[root] / total
            third = hist[(root + (4 if mode == "大调" else 3)) % 12] / total
            fifth = hist[(root + 7) % 12] / total
            score = inside / total * 0.6 + tonic * 0.25 + third * 0.1 + fifth * 0.05
            candidates.append((f"{NOTE_NAMES[root]} {mode}", score))
    candidates.sort(key=lambda x: -x[1])
    best = candidates[0]
    spread = best[1] - candidates[1][1] if len(candidates) > 1 else 0
    confidence = min(1.0, spread * 8)
    return best[0], round(confidence, 3), [(k, round(s, 3)) for k, s in candidates[:3]]


def summarize(parsed: dict, filename: str) -> str:
    tracks = parsed["tracks"]
    all_notes = [n for t in tracks for n in t["notes"]]
    if not all_notes:
        return f"{filename}：解析成功但没有音符（空谱）"

    starts = [n["start"] for n in all_notes]
    ends = [n["end"] for n in all_notes]
    nums = [x for x in (name_to_midi(n["note"]) for n in all_notes) if x is not None]
    key, conf, tops = detect_key([n["note"] for n in all_notes])
    bars = math.ceil(max(ends) / 4) if ends else 0

    lines = [
        f"# {filename}",
        f"格式 {parsed['format']}（{'多轨' if parsed['format'] == 1 else '单轨'}）｜"
        f"{parsed['tpb']} tick/拍｜{len(tracks)} 轨｜{len(all_notes)} 音符",
        f"时长 {max(ends):.1f} 拍 ≈ {bars} 小节(4/4)｜起始 {min(starts):.1f} 拍",
        f"音域 {midi_to_name(min(nums))} – {midi_to_name(max(nums))}（跨度 {max(nums) - min(nums)} 半音）",
        f"调性猜测 **{key}**（置信 {conf:.0%}）"
        + (f"｜备选 {', '.join(f'{k} {s}' for k, s in tops[1:])}" if len(tops) > 1 else ""),
        "",
        "## 轨道",
    ]
    for t in tracks:
        ns = t["notes"]
        if not ns:
            lines.append(f"- 轨 {t['index']}「{t['name'] or '未命名'}」：**空轨**")
            continue
        tn = [x for x in (name_to_midi(n["note"]) for n in ns) if x is not None]
        vs = [n["velocity"] for n in ns]
        # 活跃段落：把音符按每 4 拍装箱，看哪些小节有内容
        boxes = sorted({int(n["start"] // 4) for n in ns})
        active = f"{len(boxes)}/{bars} 小节有内容" if bars else f"{len(boxes)} 小节有内容"
        lines.append(
            f"- 轨 {t['index']}「{t['name'] or '未命名'}」：{len(ns)} 音符｜"
            f"音域 {midi_to_name(min(tn))}–{midi_to_name(max(tn))}｜"
            f"力度 {min(vs)}–{max(vs)}（均值 {sum(vs) // len(vs)}）｜{active}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------- 工具实现
def _project_dir() -> Path:
    """项目根目录。由 server.py 在启动时定死（见 resolve_project），工具层只读。"""
    p = os.environ.get("MUSIC_MCP_PROJECT") or os.environ.get("AI_MIDI_OUTPUT_DIR")
    return Path(p) if p else Path.cwd()


def _excluded() -> set[str]:
    """扫描时跳过的目录名（server.py 按 --exclude 写入环境变量）"""
    raw = os.environ.get("MUSIC_MCP_EXCLUDE", "")
    return {x for x in raw.split(os.pathsep) if x.strip()}


def _skip(path: Path, base: Path, ex: set[str]) -> bool:
    """路径中任一层落在排除名单里就跳过"""
    try:
        parts = path.relative_to(base).parts
    except ValueError:
        return True
    return any(p in ex for p in parts)


def _walk_midi(base: Path):
    """遍历项目树下所有 MIDI（已排除噪音目录）"""
    ex = _excluded()
    for p in base.rglob("*"):
        if p.is_file() and p.suffix.lower() in MIDI_EXT and not _skip(p, base, ex):
            yield p


def _deep_find(base: Path, name: str) -> Path | None:
    """
    在项目树下按文件名找唯一匹配（用于"裸文件名"的便利解析）。

    项目根可能是很深的目录树（如 D:\\...\\MusicMaker\\ 下有多个子项目），
    让模型每次写全相对路径既啰嗦又容易错，所以找不到时按 basename 深搜一次。
    只有唯一匹配才返回；多个匹配返回 None —— 由调用方报歧义，避免猜错文件。
    """
    if "/" in name or "\\" in name:
        return None                      # 已经给了相对路径，不代猜
    hits = [p for p in _walk_midi(base) if p.name == name]
    return hits[0] if len(hits) == 1 else None


def _resolve(filename: str) -> Path:
    """在项目目录下解析文件，防路径逃逸（与 AI_MIDI SafeJoin 同思路）"""
    base = _project_dir().resolve()
    name = (filename or "").strip()
    if not name:
        raise ValueError("filename 不能为空")
    if "\x00" in name:
        raise ValueError("文件名含非法字符")
    target = (base / name).resolve()
    if not str(target).startswith(str(base)):
        raise ValueError(f"路径逃逸：{filename}")
    if not target.is_file():
        hit = _deep_find(base, name)
        if hit is not None:
            return hit
        avail = sorted(p.relative_to(base).as_posix() for p in _walk_midi(base))[:20]
        raise ValueError(
            f"文件不存在：{filename}\n项目目录：{base}\n可用文件：{', '.join(avail) or '(无)'}")
    return target


def t_parse_summary(args: dict) -> str:
    path = _resolve(args.get("filename", ""))
    parsed = read_smf(path.read_bytes())
    return summarize(parsed, path.name)


def t_parse_tracks(args: dict) -> str:
    path = _resolve(args.get("filename", ""))
    parsed = read_smf(path.read_bytes())
    want = args.get("track")
    offset = float(args.get("offset_bars", 0) or 0) * 4
    limit = float(args.get("limit_bars", 0) or 0) * 4
    compact = bool(args.get("compact", False))

    sel = parsed["tracks"]
    if want is not None:
        sel = [t for t in sel if t["index"] == int(want) or t["name"] == str(want)]
        if not sel:
            return f"没有轨道匹配 {want!r}（共 {len(parsed['tracks'])} 轨）"

    out = [f"# {path.name} — {len(sel)} 轨"]
    for t in sel:
        ns = t["notes"]
        if offset or limit:
            hi = offset + limit if limit else float("inf")
            ns = [n for n in ns if offset <= n["start"] < hi]
        head = f"\n## 轨 {t['index']}「{t['name'] or '未命名'}」（{len(ns)} 音符）"
        if compact:
            out.append(head + f" → 用 compact=false 取音符明细")
            continue
        out.append(head)
        out.extend(format_note(n) for n in ns)
    return "\n".join(out)


def t_validate_note_table(args: dict) -> str:
    text = args.get("note_table") or args.get("notes") or ""
    notes, problems = parse_note_table(str(text))
    lines = [f"解析成功 {len(notes)} 个音符" + (f"，{len(problems)} 处问题" if problems else "，无问题 ✓")]

    if notes:
        vs = [n["velocity"] for n in notes]
        nums = [x for x in (name_to_midi(n["note"]) for n in notes) if x is not None]
        bad_oct = [n["note"] for n in notes if name_to_midi(n["note"]) is None]
        dup = len(notes) - len({(n["note"], n["start"]) for n in notes})
        lines.append(
            f"音域 {midi_to_name(min(nums))}–{midi_to_name(max(nums))}｜"
            f"时长 {max(n['end'] for n in notes):.1f} 拍｜"
            f"力度 {min(vs)}–{max(vs)}"
        )
        warn = []
        if dup:
            warn.append(f"{dup} 个音符与同音高的另一音符起始时间相同（和弦正常，误重复需查）")
        off = [v for v in vs if v < 1 or v > 127]
        if off:
            warn.append(f"{len(off)} 个力度超出 1–127（写 MIDI 时会被截断到 0/127）")
        if bad_oct:
            warn.append(f"{len(bad_oct)} 个音名无法识别：{', '.join(bad_oct[:5])}")
        neg = [n for n in notes if n["start"] < 0]
        if neg:
            warn.append(f"{len(neg)} 个 start 为负（会被截成 0）")
        lines.extend(f"⚠ {w}" for w in warn)
        if not warn:
            lines.append("结构检查通过，可直接交给 create_midi")
    if problems:
        lines.append("\n## 问题明细（前 20 条）")
        lines.extend(f"- {p}" for p in problems[:20])
        if len(problems) > 20:
            lines.append(f"- …另有 {len(problems) - 20} 条")
    lines.append("\n（提示：AI_MIDI 的 create_midi 只认这种带标签的行格式或 JSON 数组，CSV 会失败）")
    return "\n".join(lines)


# ---------------------------------------------------------------- 文件/目录安全解析
def _resolve_safe(filename: str, *, must_exist: bool = False, want_dir: bool = False) -> Path:
    """
    在项目目录下安全解析路径（不跟随 .. 逃逸）。与 AI_MIDI SafeJoin 同思路。
    want_dir=True 时解析目录（创建用，不要求存在）。
    """
    base = _project_dir().resolve()
    name = (filename or "").strip()
    if not name:
        raise ValueError("路径不能为空")
    if "\x00" in name:
        raise ValueError("路径包含非法字符")
    if Path(name).is_absolute():
        raise ValueError(f"路径不能是绝对路径：{filename}")

    target = (base / name).resolve()
    if target == base:
        raise ValueError("路径不能指向项目根目录本身")
    if not str(target).startswith(str(base) + os.sep) and target != base:
        raise ValueError(f"路径逃逸：{filename}")
    if must_exist and not target.exists():
        raise ValueError(f"不存在：{filename}")
    if not want_dir and must_exist and not target.is_file():
        raise ValueError(f"不是文件：{filename}")
    return target


def _list_midi(recursive: bool = True) -> list[tuple[str, int]]:
    """返回 [(相对路径, 字节数)]，按路径排序（已排除 --exclude 指定的目录）"""
    base = _project_dir().resolve()
    if not base.is_dir():
        return []
    if recursive:
        files = list(_walk_midi(base))
    else:
        ex = _excluded()
        files = [p for p in base.glob("*")
                 if p.is_file() and p.suffix.lower() in MIDI_EXT and not _skip(p, base, ex)]
    return sorted((p.relative_to(base).as_posix(), p.stat().st_size) for p in files)


# ---------------------------------------------------------------- 工具实现（对齐 AI_MIDI）
def _library_dir() -> Path:
    """知识库目录：优先环境变量，其次源码/安装目录的常见位置"""
    env = os.environ.get("MUSIC_MCP_THEORY_DIRS") or os.environ.get("AI_MIDI_LIBRARY_DIR")
    if env:
        for part in env.split(os.pathsep):
            p = Path(part.strip())
            if p.is_dir():
                return p
    for cand in (
        Path(r"C:\Users\Jestions\AppData\Local\Programs\AI_MIDI\Library"),
    ):
        if cand.is_dir():
            return cand
    raise ValueError("找不到知识库目录（设置 MUSIC_MCP_THEORY_DIRS 指向 Library）")


def t_read_library_file(args: dict) -> str:
    name = str(args.get("filename", "")).strip()
    if not name:
        raise ValueError("filename 不能为空")
    if ".." in name or "\x00" in name:
        raise ValueError(f"非法的文件名：{name}")
    root = _library_dir().resolve()

    # 用户自定义文件在 Library/user/ 下，允许 "user/xxx.md" 形式
    target = (root / name).resolve()
    if not str(target).startswith(str(root)):
        raise ValueError(f"路径逃逸：{name}")
    if target.suffix.lower() not in (".md", ".txt"):
        raise ValueError(f"仅支持读取 .md/.txt 知识文件：{name}")
    if not target.is_file():
        builtin = sorted(p.name for p in root.glob("*.md"))
        user = sorted(f"user/{p.name}" for p in (root / "user").glob("*")
                      if p.is_file() and p.suffix.lower() in (".md", ".txt")) if (root / "user").is_dir() else []
        raise ValueError(f"文件不存在 — {name}\n可用文件：{', '.join(builtin + user) or '(无)'}")
    return target.read_text(encoding="utf-8", errors="replace")


def t_list_midi_files(args: dict) -> str:
    files = _list_midi()
    if not files:
        return f"项目目录下没有 MIDI 文件：{_project_dir()}"
    lines = [f"{name}  ({size // 1024 if size >= 1024 else 1} KB)" for name, size in files]
    return "\n".join(lines)


def t_list_project_structure(args: dict) -> str:
    """树状列出项目下的 MIDI 文件（含子目录）"""
    base = _project_dir().resolve()
    files = _list_midi()
    if not files:
        return f"(空)  {base}"
    tree: dict = {}
    for name, _ in files:
        cur = tree
        parts = name.split("/")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur.setdefault("__files__", []).append(parts[-1])

    lines = [f"{base.name}/"]

    def walk(node: dict, prefix: str) -> None:
        dirs = sorted(k for k in node if k != "__files__")
        for d in dirs:
            lines.append(f"{prefix}├── {d}/")
            walk(node[d], prefix + "│   ")
        for f in sorted(node.get("__files__", [])):
            lines.append(f"{prefix}├── {f}")

    walk(tree, "")
    return "\n".join(lines)


def t_parse_midi(args: dict) -> str:
    """对齐 AI_MIDI 的 parse_midi：输出扁平 note_table（无轨道号）"""
    path = _resolve_safe(str(args.get("filename", "")), must_exist=True)
    parsed = read_smf(path.read_bytes())
    lines: list[str] = []
    for t in parsed["tracks"]:
        lines.extend(format_note(n) for n in t["notes"])
    if not lines:
        raise ValueError("未能从文件中解析出任何音符。")
    return "\n".join(lines)


def t_create_midi(args: dict) -> str:
    """
    兼容 AI_MIDI 的参数（notes / note_table + bpm + filename），
    并额外支持多轨：tracks=[{name, notes|note_table}, ...] → Format 1。
    """
    from .midi_writer import write_smf, DEFAULT_BPM

    fn = str(args.get("filename") or "").strip() or "output.mid"
    if Path(fn).suffix.lower() not in MIDI_EXT:
        fn += ".mid"
    bpm_raw = args.get("bpm", DEFAULT_BPM)
    try:
        bpm = clamp_bpm(int(bpm_raw))
    except (TypeError, ValueError):
        bpm = DEFAULT_BPM

    tracks: list[dict] = []
    problems: list[str] = []

    raw_tracks = args.get("tracks")
    if isinstance(raw_tracks, list) and raw_tracks:
        for i, item in enumerate(raw_tracks):
            if not isinstance(item, dict):
                problems.append(f"tracks[{i}] 不是对象")
                continue
            body = item.get("notes") or item.get("note_table") or ""
            notes, probs = parse_note_table(str(body))
            problems.extend(f"tracks[{i}]: {p}" for p in probs)
            tracks.append({"name": str(item.get("name", "") or f"track{i + 1}"), "notes": notes})
        if not any(t["notes"] for t in tracks):
            detail = ("；".join(problems[:5])) if problems else "所有轨道都没有可解析的音符"
            raise ValueError(f"没有解析出任何音符 —— {detail}")
    else:
        body = args.get("notes") or args.get("note_table") or ""
        if not str(body).strip():
            raise ValueError("缺少 notes 参数，请提供 note_table 格式的音符数据")
        notes, probs = parse_note_table(str(body))
        problems = probs
        if not notes:
            detail = ("；".join(problems[:5])) if problems else "格式无法识别"
            raise ValueError(
                f"没有解析出任何音符 —— {detail}\n"
                f"（提示：需要带标签的行格式 [note: \"C4\", velocity: \"80\", start: \"0\", end: \"1\"] 或 JSON 数组，CSV 不行）"
            )
        tracks = [{"name": "", "notes": notes}]

    data = write_smf(tracks, bpm)
    target = _resolve_safe(fn, want_dir=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)

    total = sum(len(t["notes"]) for t in tracks)
    size_kb = max(1, len(data) // 1024)
    fmt = 0 if len(tracks) == 1 else 1
    out = [f"成功创建 {fn}：{total} 个音符，BPM {bpm}，{size_kb} KB"
           f"（Format {fmt}，{len(tracks)} 轨）"]
    if len(tracks) > 1:
        for t in tracks:
            out.append(f"  · {t['name']}：{len(t['notes'])} 音符")
    if problems:
        out.append(f"⚠ 解析过程中跳过/修正了 {len(problems)} 处：")
        out.extend(f"  - {p}" for p in problems[:8])
        if len(problems) > 8:
            out.append(f"  - …另有 {len(problems) - 8} 处")
    return "\n".join(out)


def t_delete_midi(args: dict) -> str:
    fn = str(args.get("filename", "")).strip()
    if Path(fn).suffix.lower() not in MIDI_EXT:
        raise ValueError(f"只能删除 MIDI 文件（.mid / .midi）— {fn}")
    target = _resolve_safe(fn, must_exist=True)
    try:
        target.unlink()
    except PermissionError as e:
        raise ValueError(f"删除失败（文件可能正被播放器/DAW 占用）— {fn}：{e}")
    # 清理空目录（与 AI_MIDI cleanupEmptyParents 同行为，但不越过项目根）
    base = _project_dir().resolve()
    parent = target.parent
    while parent != base and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent
    return f"成功删除 {fn}"


def t_create_folder(args: dict) -> str:
    name = str(args.get("name", "")).strip()
    if not name:
        raise ValueError("文件夹名不能为空")
    target = _resolve_safe(name, want_dir=True)
    if target.exists():
        if target.is_dir():
            return f"文件夹已存在: {name}"
        raise ValueError(f"路径已存在且不是文件夹 - {name}")
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ValueError(f"创建文件夹失败 - {e}")
    return f"成功创建文件夹: {name}"


TOOLS = [
    # -------- AI_MIDI 缺失的三个（本服务器的主场）--------
    {
        "name": "parse_midi_summary",
        "description": "读取 MIDI 并给出乐谱摘要：格式/轨数/时长/音域/调性猜测/每轨统计。看谱、判断结构、决定怎么改之前先看这个，比读全部音符省几百倍上下文。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "MIDI 文件名（相对项目目录，如 破损的循环.mid）"},
            },
            "required": ["filename"],
        },
    },
    {
        "name": "parse_midi_tracks",
        "description": "按轨道读取 MIDI 音符（parse_midi 会丢掉轨道号，这个保留）。可按轨道筛选、按小节切片，避免一次拉出整首。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "MIDI 文件名"},
                "track": {"type": "integer", "description": "只取这一轨（轨道序号，从 0 开始）。省略则返回全部轨道"},
                "offset_bars": {"type": "number", "description": "起始小节（4/4 计），默认 0"},
                "limit_bars": {"type": "number", "description": "取多少个小节，默认不限"},
                "compact": {"type": "boolean", "description": "true 时只列轨道概要、不返回音符明细，默认 false"},
            },
            "required": ["filename"],
        },
    },
    {
        "name": "validate_note_table",
        "description": "在写 MIDI 之前校验 note_table 文本或 JSON：逐行报出缺字段、数值非法、end<start、力度越界、音名无法识别等问题。create_midi 遇到格式错只报「没有解析出任何音符」，用这个先查清楚。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "note_table": {"type": "string", "description": "note_table 文本或 JSON 数组"},
            },
            "required": ["note_table"],
        },
    },
    # -------- 与 AI_MIDI 对齐的七个（参数名保持一致）--------
    {
        "name": "read_library_file",
        "description": "读取 Library 目录下的乐理知识文件内容（.md/.txt）。与资源 music://theory/* 同一份文件；需要挑文件时先看 resources/list 或 music://theory/_index。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "文件名（如 02_配和弦指南.md）；自建笔记用 user/ 开头"},
            },
            "required": ["filename"],
        },
    },
    {
        "name": "list_midi_files",
        "description": "列出当前项目所有 MIDI 文件及其大小（含子目录，路径为相对项目目录）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "parse_midi",
        "description": "解析 MIDI 文件，返回 note_table 格式的扁平音符列表（不含轨道号）。需要分轨信息用 parse_midi_tracks，需要概览用 parse_midi_summary。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "MIDI 文件名（如 output.mid、song.mid）"},
            },
            "required": ["filename"],
        },
    },
    {
        "name": "create_midi",
        "description": "从 note_table 数据创建 MIDI 文件。单轨时写 Format 0；给 tracks 数组则写多轨 Format 1（AI_MIDI 只支持单轨）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "输出文件名（如 melody.mid），默认 output.mid"},
                "bpm": {"type": "integer", "description": "速度（1-600，如 120），默认 120"},
                "notes": {"type": "string", "description": "note_table 格式的音符数据，每行一个音符"},
                "note_table": {"type": "string", "description": "同 notes，兼容参数名"},
                "tracks": {
                    "type": "array",
                    "description": "多轨（可选）：每项 {name, notes}，给出则写 Format 1",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "轨道名"},
                            "notes": {"type": "string", "description": "该轨的 note_table"},
                        },
                    },
                },
            },
        },
    },
    {
        "name": "delete_midi",
        "description": "删除项目目录下的 MIDI 文件（会顺带清理变空的子目录）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "要删除的 MIDI 文件名（如 old.mid）"},
            },
            "required": ["filename"],
        },
    },
    {
        "name": "create_folder",
        "description": "在项目目录下创建文件夹（支持多级子目录）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "文件夹相对路径（如 drums、sectionA/drums）"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "list_project_structure",
        "description": "以树状图列出当前项目所有 MIDI 文件的目录层级结构。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

_DISPATCH = {
    # AI_MIDI 缺的
    "parse_midi_summary": t_parse_summary,
    "parse_midi_tracks": t_parse_tracks,
    "validate_note_table": t_validate_note_table,
    # 对齐 AI_MIDI 的
    "read_library_file": t_read_library_file,
    "list_midi_files": t_list_midi_files,
    "parse_midi": t_parse_midi,
    "create_midi": t_create_midi,
    "delete_midi": t_delete_midi,
    "create_folder": t_create_folder,
    "list_project_structure": t_list_project_structure,
}


def list_tools() -> list[dict]:
    return [
        {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
        for t in TOOLS
    ]


def call_tool_ex(name: str, args: dict) -> tuple[str, bool]:
    """执行工具，返回 (文本, 是否失败)。异常在这里收口，不抛给 JSON-RPC 层。"""
    fn = _DISPATCH.get(name)
    if fn is None:
        return f"未知工具：{name}（可用：{', '.join(_DISPATCH)}）", True
    if not isinstance(args, dict):
        return f"arguments 必须是对象，收到 {type(args).__name__}", True
    try:
        return fn(args), False
    except Exception as e:
        return f"{type(e).__name__}: {e}", True


def call_tool(name: str, args: dict) -> str:
    """向后兼容入口：只要文本。

    执行失败不抛异常，而是返回带 _error 标记的 JSON —— server.py 会把它
    转成 MCP 规范要求的 isError:true，这样模型能区分「工具说完了」和
    「工具没做成」，而不是把错误文本当成正常结果继续往下走。
    """
    text, is_error = call_tool_ex(name, args)
    if not is_error:
        return text
    return json.dumps({"_error": True, "message": text}, ensure_ascii=False)
