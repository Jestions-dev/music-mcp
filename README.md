# music-mcp

面向 AI 编曲的 **MCP 服务器**：MIDI 读写、乐谱分析、乐理知识库资源。
**纯标准库，零依赖，不需要 API key，不需要 GUI。**

给 agent（DeepSeek Harness / Claude Code / Cursor / 任何 MCP 客户端）用，
把"写 MIDI、看谱、查乐理"这三件事暴露成工具与资源。

```
agent ──stdio JSON-RPC──▶ music-mcp ──▶ 项目目录里的 .mid 文件
                                     └─▶ AI_MIDI 的 Library/ 乐理知识库
```

---

## 快速开始

```bash
# 直接跑（无需安装）
python -m music_mcp.server --project "D:/你的音乐项目"

# 或者安装成命令
pip install -e .
music-mcp --project "D:/你的音乐项目"
```

### 命令行参数

**项目地址在启动时定死**（类比 Playwright MCP 的 `--executable-path`）：
给一次 `--project`，之后所有工具调用都用它，**没有按调用切换项目的参数**。
想同时服务两个项目 → 起两个实例、给不同的 `serverName`。

| 参数 | 作用 |
|---|---|
| `--project DIR` | 项目根目录，所有 MIDI 读写都在它下面。优先级高于环境变量 |
| `--state-dir DIR` | 引擎状态资源（`music://state/*`）所在目录。**与项目根刻意分开**：项目区可以整体换掉，状态资源仍指向实际产出它的子项目 |
| `--theory DIRS` | 乐理知识库目录，多个用 `;` 分隔 |
| `--exclude NAME` | 扫描时跳过的目录名，可重复。默认已排除 `.git` / `node_modules` / `__pycache__` / `dist` / `build` 等 |
| `--project-must-exist` | 项目目录不存在时直接退出（默认自动创建） |

> **为什么要 `--exclude`**：项目根常是一个大目录树（父目录）。若树里放着
> 克隆来的仓库或依赖，它们自带的 demo `.mid` 会被算成"项目里的 MIDI"。
> 例：`--exclude 参考_AI_MIDI` 可以把源码克隆整个排除掉。

### 挂到 DSH / Claude Code / Cursor

stdio 传输，配置一段即可。注意 **Windows 上不要用 `command: npx`**
（Node 直接 spawn 找不到 `npx.exe`，会 ENOENT），用解释器绝对路径：

```yaml
# DSH：~/.dsh/profiles/<profile>/cordis.patch.yml
- id: mcp-music
  name: "@deepseek-ai/dsh-mcp-client"
  config:
    serverName: music
    transport: stdio
    command: "C:/Python314/python.exe"
    args:
      - "-m"
      - "music_mcp.server"
      - "--project"
      - "D:/你的音乐项目"          # ← 启动时定死
      - "--state-dir"
      - "D:/你的音乐项目/某个子项目"   # ← 引擎状态资源（可选）
    cwd: "C:/Users/<你>/.dsh/music-mcp"
    env:
      PYTHONIOENCODING: "utf-8"
      MUSIC_MCP_THEORY_DIRS: "D:/.../AI_MIDI/Library"   # 乐理知识库（可选）
```

```json
// Claude Code / Cursor：mcpServers 里加一项
{
  "music": {
    "command": "python",
    "args": ["-m", "music_mcp.server", "--project", "D:/你的音乐项目"],
    "env": { "PYTHONIOENCODING": "utf-8" }
  }
}
```

---

## 环境变量

命令行参数优先；以下环境变量在没有对应参数时生效。

| 变量 | 作用 | 默认 |
|---|---|---|
| `MUSIC_MCP_PROJECT` / `AI_MIDI_OUTPUT_DIR` | 项目根目录 | 当前工作目录 |
| `MUSIC_MCP_STATE_DIR` | 引擎状态目录 | 项目根（有 `分析/` 子目录时即项目根） |
| `MUSIC_MCP_THEORY_DIRS` | 乐理知识库目录，多个用 `;` 分隔 | AI_MIDI 安装目录下的 `Library/` |
| `MUSIC_MCP_THEORY_USER` | 自建笔记目录（资源里以 `user/` 前缀呈现） | `<项目>/乐理/user` |
| `MUSIC_MCP_EXCLUDE` | 扫描跳过的目录名，`;` 分隔（由 `--exclude` 写入） | 见上 |
| `PYTHONIOENCODING` | 建议设 `utf-8`（Windows 控制台默认 GBK 会炸在 ✓ ⚠ 上） | — |

---

## 工具（10 个）

### 对齐 AI_MIDI 的 7 个（参数名兼容，可直接替换）

| 工具 | 说明 |
|---|---|
| `read_library_file` | 读乐理知识库文件（`.md`/`.txt`，自建笔记用 `user/` 前缀） |
| `list_midi_files` | 列出项目下所有 MIDI 及大小 |
| `parse_midi` | 解析 MIDI → 扁平 note_table（不含轨道号，兼容旧调用） |
| `create_midi` | 从 note_table 建 MIDI；**给 `tracks` 数组则写多轨 Format 1** |
| `delete_midi` | 删除 MIDI（顺带清理变空的子目录） |
| `create_folder` | 建项目子目录 |
| `list_project_structure` | 树状列出项目的 MIDI 结构 |

### 本服务器独有的 3 个

| 工具 | 为什么需要 |
|---|---|
| `parse_midi_summary` | 一屏看懂整首：格式/轨数/时长/音域/**调性猜测**/每轨统计。原始 `parse_midi` 会把整首吐出来——672 拍的曲子约 103 KB 文本，一次就能把上下文打满 |
| `parse_midi_tracks` | **带轨道号**读取，可按轨道筛选、按小节切片。`parse_midi` 在解析时就丢掉了轨道信息 |
| `validate_note_table` | 写入前校验：逐行报出缺字段、数值非法、`end<start`、力度越界、音名无法识别 |

---

## 资源（按需读取，不占常驻上下文）

| URI | 内容 |
|---|---|
| `music://theory/<文件名>` | 乐理知识库（内置 20 篇 + 自建笔记） |
| `music://theory/_index` | 按主题分类的索引，便于挑文件 |
| `music://state/<名字>` | 本项目的引擎状态（编排信息、验收数据、母带参数…） |
| `music://state/_index` | 状态资源索引 |

> 资源是 MCP 的"按需读取"通道：模型先看目录，需要时才读某一篇。
> 这比把知识库整篇塞进提示词省几百倍上下文。

---

## MIDI 格式说明

写出的文件与 AI_MIDI 的 `smf_writer.go` 同规格，**两边可互读**（已实测）：

- Division **480 tick/拍**，`startTick = max(0, round(start × 480))`
- 同一 tick 上先 `note_off` 再 `note_on`（否则同音高相邻音符会被并成一个长音）
- 单轨 → Format 0；多轨 → Format 1（tempo 放第一轨）
- 力度截断到 0–127，音高截断到 0–127，BPM 截断到 1–600

`note_table` 输入支持两种写法（与 AI_MIDI 的解析正则一致）：

```
[note: "C4", velocity: "80", start: "0", end: "1"]
```
```json
[{"note": "C4", "velocity": 80, "start": 0, "end": 1}]
```

**CSV 不行**（`C4,80,0,1` 会失败）——这是刻意与 AI_MIDI 保持一致。

---

## 本仓库与运行位置的关系

**GitHub 是源码的主副本**（`Jestions-dev/music-mcp`），
而 DSH 实际加载的是本机目录 `C:\Users\Jestions\.dsh\music-mcp\`（配置里 `cwd` 指向它）。

改动流程：

```powershell
# ① 在运行位置改（或从 GitHub 拉下来改），跑测试
cd C:\Users\Jestions\.dsh\music-mcp
python -m unittest discover -s tests -t .

# ② 备份目录同步一份（可搬运的干净副本）
robocopy "C:\Users\Jestions\.dsh\music-mcp" "D:\ForDeepseek\MusicMakerMCP\MCP备份" `
         /E /XD __pycache__ *.egg-info .git

# ③ 传到 GitHub
gh api repos/Jestions-dev/music-mcp/contents/<路径> -X PUT --input payload.json
```

> 注：本机 git 的网络操作受沙箱限制（Windows Schannel 拿不到证书容器），
> 所以推送走 `gh api` 而不是 `git push`。

在**另一台机器**上要用，只需：

```bash
git clone https://github.com/Jestions-dev/music-mcp
cd music-mcp && python -m unittest discover -s tests -t .    # 自检
python -m music_mcp.server --project "D:/你的音乐项目"
```

## 测试

```bash
python -m unittest discover -s tests -t . -v
```

标准库 `unittest`，无第三方依赖，**53 项**。覆盖：版本协商、能力声明、
`resources/*`、错误码、`isError` 语义、10 个工具的完整生命周期、
路径逃逸拦截、参数截断、坏输入不致崩、CLI 优先级与深搜解析。

---

## 设计取舍

- **为什么不用官方 MCP SDK？** 那会带进依赖；本服务器只用标准库就能满足
  协议子集（initialize / tools / resources / ping）。
- **为什么自造 SMF 读写？** 只为读写 MIDI 引入 `mido`/`pretty_midi` 不划算，
  而 SMF 格式本身很小（本仓库读写合计约 190 行）。
- **不读摄像头、不联网、不需要凭据**：除读写你指定的项目目录与知识库外，
  没有其他 I/O。
- **项目地址在启动时定死**：见上文 CLI 参数一节。

---

## 与 AI_MIDI 的关系

本项目**不是** [AI_MIDI](https://github.com/abab996/AI_MIDI) 的 fork，也没有使用它的任何代码。
它是那个项目里 MCP 层的**独立重写与扩展**，动机是三个具体缺口：

| 缺口 | AI_MIDI 的 MCP 层 | 本项目 |
|---|---|---|
| 知识库 | 只有 `read_library_file` 工具，模型只能整篇读，且无法先看目录 | 暴露为 MCP **资源**（`music://theory/*`），按需读取 |
| 失败语义 | 工具执行失败不标记，调用方会把错误当结果继续处理 | 失败置 `isError: true` |
| 多轨 | `create_midi` 只能写单轨 Format 0 | 支持 `tracks` 数组 → **Format 1** |

两者的 MIDI 格式规格一致（Format 0 / 480 tick/拍 / 同样的 `note_table` 文本格式），
所以**文件可以互相读写**（已实测）。也正因如此，本服务器可以直接读
AI_MIDI 安装目录下的 `Library/` 作为乐理知识库——那是只读的。

致谢：AI_MIDI 的 `Library/` 乐理知识库（20 篇）由该项目提供，
本服务器把它当作外部知识源使用，不随本仓库分发。

## 许可

MIT，见 [LICENSE](LICENSE)。
