# -*- coding: utf-8 -*-
"""music-mcp 端到端探针：起服务器进程，走完整 JSON-RPC 流程"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

# Windows 控制台默认 GBK，会炸在 ✓ ⚠ 这类字符上；统一成 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PKG_DIR = r"D:\ForDeepseek\MusicMaker\音乐MCP"
PROJ = r"D:\ForDeepseek\MusicMaker\04_原创_破损的循环\MIDI"

env = dict(os.environ)
env["AI_MIDI_OUTPUT_DIR"] = PROJ
env["PYTHONIOENCODING"] = "utf-8"

proc = subprocess.Popen(
    [sys.executable, "-m", "music_mcp.server"],
    cwd=PKG_DIR, env=env,
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, encoding="utf-8", bufsize=1,
)

_next = [1]


def call(method, params=None, timeout=20.0):
    rid = _next[0]
    _next[0] += 1
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                 "params": params or {}}, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    t0 = time.time()
    while time.time() - t0 < timeout:
        line = proc.stdout.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            print(f"  [非 JSON 输出] {line[:120]}")
            continue
        if msg.get("id") == rid:
            return msg
    return {"error": {"message": "超时或无响应"}}


def body(msg):
    if "error" in msg and msg["error"]:
        return f"❌ {msg['error'].get('code')}: {msg['error'].get('message')}"
    return msg.get("result")


try:
    print("=" * 70)
    print("① initialize")
    r = body(call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}}))
    print("   协议:", r["protocolVersion"])
    print("   服务器:", r["serverInfo"]["name"], "v" + r["serverInfo"]["version"])
    print("   能力:", json.dumps(r["capabilities"], ensure_ascii=False))

    print("\n" + "=" * 70)
    print("② resources/list")
    r = body(call("resources/list"))
    res = r["resources"]
    theory = [x for x in res if x["uri"].startswith("music://theory")]
    state = [x for x in res if x["uri"].startswith("music://state")]
    print(f"   共 {len(res)} 项：乐理 {len(theory)}｜状态 {len(state)}")
    for x in theory[:4]:
        print(f"     · {x['uri']}")
        print(f"       {x['title']} — {x['description']}")
    print("     …")
    for x in state:
        print(f"     · {x['uri']}  — {x['description']}")

    print("\n" + "=" * 70)
    print("③ resources/templates/list")
    r = body(call("resources/templates/list"))
    for t in r["resourceTemplates"]:
        print(f"   · {t['uriTemplate']}  {t['name']}")

    print("\n" + "=" * 70)
    print("④ resources/read（读一篇乐理）")
    r = body(call("resources/read", {"uri": "music://theory/02_配和弦指南.md"}))
    txt = r["contents"][0]["text"]
    print(f"   mimeType: {r['contents'][0]['mimeType']}｜{len(txt)} 字符")
    print("   前 3 行:")
    for ln in txt.splitlines()[:3]:
        print("     " + ln)

    print("\n" + "=" * 70)
    print("⑤ resources/read（读 _index）")
    r = body(call("resources/read", {"uri": "music://theory/_index"}))
    idx = json.loads(r["contents"][0]["text"])
    print(f"   索引 {len(idx)} 条，前 3 条：")
    for it in idx[:3]:
        print(f"     · {it['name']} — {it['description']}")

    print("\n" + "=" * 70)
    print("⑥ tools/list")
    r = body(call("tools/list"))
    for t in r["tools"]:
        print(f"   · {t['name']}")
        print(f"       {t['description'][:90]}…")

    print("\n" + "=" * 70)
    print("⑦ tools/call → parse_midi_summary（破损的循环.mid）")
    r = body(call("tools/call", {"name": "parse_midi_summary",
                                 "arguments": {"filename": "破损的循环.mid"}}, timeout=30))
    print(r["content"][0]["text"])

    print("\n" + "=" * 70)
    print("⑧ tools/call → parse_midi_tracks（只看第 0 轨、前 2 小节）")
    r = body(call("tools/call", {"name": "parse_midi_tracks",
                                 "arguments": {"filename": "破损的循环.mid",
                                               "track": 0, "limit_bars": 2}}, timeout=30))
    out = r["content"][0]["text"]
    print("\n".join(out.splitlines()[:14]))

    print("\n" + "=" * 70)
    print("⑨ tools/call → validate_note_table（故意写错）")
    bad = '''[note: "C4", velocity: "80", start: "0", end: "1"]
[note: "E4", velocity: "999", start: "1", end: "2"]
[note: "G4", velocity: "80", start: "3", end: "2"]
[note: "X9", velocity: "80", start: "2", end: "3"]
[note: "B4", start: "4"]'''
    r = body(call("tools/call", {"name": "validate_note_table", "arguments": {"note_table": bad}}))
    print(r["content"][0]["text"])

    print("\n" + "=" * 70)
    print("⑩ 未知方法应回 -32601")
    r = body(call("prompts/list"))
    print("   ", r)

finally:
    proc.stdin.close()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    err = proc.stderr.read()
    if err.strip():
        print("\n[stderr]", err.strip()[:800])
