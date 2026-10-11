# -*- coding: utf-8 -*-
"""
music-mcp 手动自检（人看的版本，带逐项打印）

与 tests/ 的区别：
  · tests/     —— unittest 断言，给 CI / 回归用，输出简洁
  · 本脚本     —— 打印每一步的实际输出，给人排查用

用法：
  python tools/manual_check.py                       # 用环境变量里的项目路径
  python tools/manual_check.py D:\\path\\to\\project   # 指定项目目录

依赖两个可选路径，缺了会跳过对应项而不是报错：
  MUSIC_MCP_PROJECT / AI_MIDI_OUTPUT_DIR  —— 项目目录（默认取第一个参数或当前目录）
  MUSIC_MCP_THEORY_DIRS                   —— 乐理知识库
  AI_MIDI_EXE                             —— 装了 AI_MIDI 的话，额外验证互操作
"""
import json
import os
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PKG = str(Path(__file__).resolve().parent.parent)

PROJ = os.environ.get("MUSIC_MCP_PROJECT") or os.environ.get("AI_MIDI_OUTPUT_DIR") \
    or (sys.argv[1] if len(sys.argv) > 1 else os.getcwd())
LIB = os.environ.get("MUSIC_MCP_THEORY_DIRS", "")
AIMIDI = os.environ.get("AI_MIDI_EXE",
                        r"C:\Users\Jestions\AppData\Local\Programs\AI_MIDI\AI_MIDI.exe")
TESTDIR = "zz_mcp_selftest"

if not Path(PROJ).is_dir():
    print(f"项目目录不存在：{PROJ}")
    print("用法：python tools/manual_check.py <项目目录>，或设 MUSIC_MCP_PROJECT")
    raise SystemExit(2)

base_env = {**os.environ, "PYTHONIOENCODING": "utf-8",
            "AI_MIDI_OUTPUT_DIR": PROJ, "MUSIC_MCP_PROJECT": PROJ}
if LIB:
    base_env["MUSIC_MCP_THEORY_DIRS"] = LIB

print(f"项目目录：{PROJ}")
print(f"知识库：  {LIB or '(未设置，相关项将跳过)'}")
print(f"包目录：  {PKG}\n")


class Server:
    def __init__(self, cmd, cwd=None):
        self.p = subprocess.Popen(cmd, cwd=cwd, env=base_env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
        self.n = 0
        self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})

    def call(self, method, params=None, timeout=30):
        self.n += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method,
                                       "params": params or {}}, ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        line = self.p.stdout.readline()
        return json.loads(line.strip()) if line else {"error": {"message": "无响应"}}

    def tool(self, _tool_name, **args):
        r = self.call("tools/call", {"name": _tool_name, "arguments": args})
        res = r.get("result") or {}
        return (res.get("content") or [{}])[0].get("text", ""), bool(res.get("isError"))

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


ok = lambda b: "✓" if b else "✗ 失败"
music = Server([sys.executable, "-m", "music_mcp.server"], cwd=PKG)

try:
    print("=" * 74)
    print("① tools/list")
    names = [t["name"] for t in music.call("tools/list")["result"]["tools"]]
    print(f"  {len(names)} 个:", ", ".join(names))
    need = {"parse_midi_summary", "parse_midi_tracks", "validate_note_table",
            "read_library_file", "list_midi_files", "parse_midi", "create_midi",
            "delete_midi", "create_folder", "list_project_structure"}
    miss = need - set(names)
    print("  覆盖 AI_MIDI 的 7 个 + 独有 3 个:", ok(not miss), f"缺 {miss}" if miss else "")

    print("\n" + "=" * 74)
    print("② create_folder")
    txt, err = music.tool("create_folder", name=TESTDIR)
    print("  ", txt.strip(), ok(not err))

    print("\n" + "=" * 74)
    print("③ create_midi 单轨（Format 0，与 AI_MIDI 同规格）")
    NOTES = "\n".join([
        '[note: "C4", velocity: "80", start: "0", end: "1"]',
        '[note: "E4", velocity: "80", start: "1", end: "2"]',
        '[note: "G4", velocity: "80", start: "2", end: "3"]',
        '[note: "C5", velocity: "90", start: "3", end: "4"]',
    ])
    txt, err = music.tool("create_midi", filename=f"{TESTDIR}/single.mid", bpm=100, notes=NOTES)
    print("  ", txt.strip().replace("\n", "\n   "), ok(not err))

    print("\n" + "=" * 74)
    print("④ create_midi 多轨（Format 1）—— AI_MIDI 只能单轨")
    txt, err = music.tool("create_midi", filename=f"{TESTDIR}/multi.mid", bpm=128, tracks=[
        {"name": "旋律", "notes": NOTES},
        {"name": "低音", "notes": '[note: "C3", velocity: "70", start: "0", end: "2"]\n'
                                  '[note: "G2", velocity: "70", start: "2", end: "4"]'},
        {"name": "鼓", "notes": '[{"note":"C2","velocity":100,"start":0,"end":0.25},'
                                '{"note":"C2","velocity":100,"start":1,"end":1.25}]'},
    ])
    print("  ", txt.strip().replace("\n", "\n   "), ok(not err))

    print("\n" + "=" * 74)
    print("⑤ parse_midi_summary —— 单轨")
    txt, err = music.tool("parse_midi_summary", filename=f"{TESTDIR}/single.mid")
    print("  ", txt.strip().replace("\n", "\n   ")[:340])

    print("\n" + "=" * 74)
    print("⑥ parse_midi_summary —— 多轨")
    txt, err = music.tool("parse_midi_summary", filename=f"{TESTDIR}/multi.mid")
    for l in txt.strip().split("\n"):
        print("  ", l)
    print("  三轨齐全:", ok(all(f"「{x}」" in txt for x in ("旋律", "低音", "鼓"))))

    print("\n" + "=" * 74)
    print("⑦ parse_midi_tracks（compact）")
    txt, err = music.tool("parse_midi_tracks", filename=f"{TESTDIR}/multi.mid", compact=True)
    print("  ", txt.strip().replace("\n", "\n   ")[:260])

    print("\n" + "=" * 74)
    print("⑧ parse_midi 扁平输出（格式对齐 AI_MIDI）")
    txt, err = music.tool("parse_midi", filename=f"{TESTDIR}/single.mid")
    rows = txt.strip().split("\n")
    print(f"   {len(rows)} 行 | 首行: {rows[0]}", ok(not err))
    print("   行格式与 AI_MIDI 一致:", ok(rows[0].startswith('[note: "')))

    print("\n" + "=" * 74)
    print("⑨ read_library_file")
    txt, err = music.tool("read_library_file", filename="02_配和弦指南.md")
    print(f"   {len(txt)} 字符 | {txt.splitlines()[0] if txt else '(空)'}", ok(not err))
    _, err2 = music.tool("read_library_file", filename="不存在的.md")
    print("   不存在的文件 → isError:", ok(err2))

    print("\n" + "=" * 74)
    print("⑩ list_midi_files / list_project_structure")
    txt, _ = music.tool("list_midi_files")
    print("   list_midi_files 首行:", txt.strip().split("\n")[0][:60])
    txt, err = music.tool("list_project_structure")
    print("   list_project_structure 含测试目录:", ok(TESTDIR in txt))
    print("  ", txt.strip().split("\n")[0])

    print("\n" + "=" * 74)
    print("⑪ 路径逃逸必须被挡")
    for bad in ("../../../etc/passwd", "C:/Windows/win.ini"):
        _, e = music.tool("parse_midi", filename=bad)
        print(f"   {bad:<34} isError={e}", ok(e))

    print("\n" + "=" * 74)
    print("⑫ 互操作：用 aimidi 读我们写的文件")
    aim = Server([AIMIDI, "--mcp-child"])
    try:
        t1, e1 = aim.tool("parse_midi", filename=f"{TESTDIR}/single.mid")
        print(f"   aimidi.parse_midi(我们写的 single.mid) → {len(t1.strip().splitlines())} 行, isError={e1}")
        print("   ", (t1.strip().splitlines() or ["(空)"])[0][:80], ok(not e1 and "note:" in t1))
        t2, e2 = aim.tool("parse_midi_summary", filename="x")  # aimidi 没这个工具
        print("   aimidi 调用我们的工具名（应报未知工具）:", ok(e2))
    finally:
        aim.close()

    print("\n" + "=" * 74)
    print("⑬ 清理")
    for f in ("single.mid", "multi.mid"):
        txt, err = music.tool("delete_midi", filename=f"{TESTDIR}/{f}")
        print(f"   删 {f}:", txt.strip(), ok(not err))

finally:
    music.close()

print("\n完成。")
