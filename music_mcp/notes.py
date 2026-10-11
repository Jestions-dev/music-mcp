# -*- coding: utf-8 -*-
"""
音名/音高/取值范围 —— tools 与 midi_writer 共用的基础层

放在单独模块，避免 tools ↔ midi_writer 互相 import。
取值范围与 AI_MIDI 的 internal/config 对齐：
  NotesPerOctave=12, NoteNumberMin=0, NoteNumberMax=127,
  MinVelocity=0, MaxVelocity=127, BPMMin=1, BPMMax=600, TicksPerBeat=480
"""
from __future__ import annotations

import re
from typing import Optional

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# 与 AI_MIDI internal/midi/note.go 的 notesMap 一致
NOTE_TO_SEMITONE = {
    "C": 0, "C#": 1, "Db": 1,
    "D": 2, "D#": 3, "Eb": 3,
    "E": 4, "Fb": 4,
    "F": 5, "E#": 5,
    "F#": 6, "Gb": 6,
    "G": 7, "G#": 8, "Ab": 8,
    "A": 9, "A#": 10, "Bb": 10,
    "B": 11, "Cb": 11,
}

MIDI_MIN, MIDI_MAX = 0, 127
VEL_MIN, VEL_MAX = 0, 127
BPM_MIN, BPM_MAX = 1, 600
TPB = 480                     # AI_MIDI config.TicksPerBeat

_NOTE_RE = re.compile(r"(?i)^([A-G][#b]*)")


def midi_to_name(n: int) -> str:
    """编号 → 音名，如 60 → C4。等价 AI_MIDI midi.MidiNumberToNoteName"""
    if not (MIDI_MIN <= n <= MIDI_MAX):
        return "Invalid"
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"


def name_to_midi(name: str) -> Optional[int]:
    """
    音名 → 编号，支持 C4 / Eb3 / C##4 / Bbb2。等价 AI_MIDI midi.NoteNameToMidiNumber。
    无法识别返回 None（调用方决定是丢弃还是报错）。
    """
    m = _NOTE_RE.match((name or "").strip())
    if not m:
        return None
    pitch = m.group(1)
    rest = name.strip()[len(pitch):]
    try:
        octave = int(rest)
    except ValueError:
        return None
    base = pitch[0].upper()
    if base not in NOTE_TO_SEMITONE:
        return None
    accidental = pitch[1:].lower().count("#") - pitch[1:].lower().count("b")
    n = (octave + 1) * 12 + NOTE_TO_SEMITONE[base] + accidental
    return clamp_midi(n)


def clamp_midi(n: int) -> int:
    return MIDI_MIN if n < MIDI_MIN else (MIDI_MAX if n > MIDI_MAX else n)


def clamp_velocity(v: int) -> int:
    return VEL_MIN if v < VEL_MIN else (VEL_MAX if v > VEL_MAX else v)


def clamp_bpm(b: int) -> int:
    return BPM_MIN if b < BPM_MIN else (BPM_MAX if b > BPM_MAX else b)
