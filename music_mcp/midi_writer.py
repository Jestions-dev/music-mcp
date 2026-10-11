# -*- coding: utf-8 -*-
"""
music-mcp 的 MIDI 写入层（纯标准库）

规格对齐 AI_MIDI 的 smf_writer.go，保证两边生成的文件能互相读：
  · Format 0 / 单轨 / Division = 480 tick/拍（config.TicksPerBeat）
  · startTick = max(0, round(start * tpb))
  · endTick   = max(startTick + 1, round(end * tpb))
  · 同一 tick 上先 note_off 再 note_on（否则同音高相邻音符会被并成一个长音）

额外提供 AI_MIDI 没有的能力：**多轨（Format 1）输出**。
它的 create_midi 只写单轨，而编排一首曲子需要分轨。
"""
from __future__ import annotations

import struct

from .notes import TPB, name_to_midi, clamp_velocity, clamp_bpm

DEFAULT_BPM = 120


def _vlq(n: int) -> bytes:
    """MIDI 变长数量（每字节 7 位，最高位表示是否继续）"""
    if n < 0:
        n = 0
    out = bytearray([n & 0x7F])
    n >>= 7
    while n > 0:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    return bytes(reversed(out))


def _track_chunk(name: str, notes: list[dict], bpm: int, *, with_tempo: bool) -> bytes:
    """把一组音符编码成一个 MTrk 块。"""
    events: list[tuple[int, int, int, int]] = []   # (tick, kind, note, vel)；kind 0=off 1=on
    for n in notes:
        midi = name_to_midi(n.get("note", ""))
        if midi is None:
            continue
        vel = clamp_velocity(int(n.get("velocity", 80)))
        try:
            st = max(0.0, float(n.get("start", 0)))
            en = max(float(n.get("end", 0)), st)
        except (TypeError, ValueError):
            continue
        st_t = max(0, int(round(st * TPB)))
        en_t = max(st_t + 1, int(round(en * TPB)))
        events.append((st_t, 1, midi, vel))
        events.append((en_t, 0, midi, 0))

    events.sort(key=lambda e: (e[0], e[1]))

    buf = bytearray()
    if with_tempo:
        us = int(round(60_000_000 / clamp_bpm(int(bpm or DEFAULT_BPM))))
        buf += _vlq(0) + b"\xFF\x51\x03" + struct.pack(">I", us)[1:]
    if name:
        raw = name.encode("utf-8")[:127]
        buf += _vlq(0) + b"\xFF\x03" + _vlq(len(raw)) + raw

    last = 0
    for tick, kind, note, vel in events:
        buf += _vlq(tick - last)
        last = tick
        if kind == 1:
            buf += bytes([0x90, note, vel])          # note_on, channel 0
        else:
            buf += bytes([0x80, note, 0x40])         # note_off, channel 0

    buf += _vlq(0) + b"\xFF\x2F\x00"                 # End of Track
    return b"MTrk" + struct.pack(">I", len(buf)) + bytes(buf)


def write_smf(tracks: list[dict], bpm: int = DEFAULT_BPM) -> bytes:
    """
    tracks: [{"name": str, "notes": [{note, velocity, start, end}, ...]}, ...]
    1 轨 → Format 0（与 AI_MIDI 一致）；多轨 → Format 1（tempo 放第一轨）。
    """
    bpm = clamp_bpm(int(bpm or DEFAULT_BPM))
    if not tracks:
        tracks = [{"name": "", "notes": []}]

    fmt = 0 if len(tracks) == 1 else 1
    header = b"MThd" + struct.pack(">IHHH", 6, fmt, len(tracks), TPB)

    chunks = [
        _track_chunk(t.get("name", ""), t.get("notes", []), bpm, with_tempo=(i == 0))
        for i, t in enumerate(tracks)
    ]
    return header + b"".join(chunks)
