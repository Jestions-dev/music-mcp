# -*- coding: utf-8 -*-
"""CLI 与「启动时定死项目地址」的测试

设计意图（用户 2026-10-11 要求）：
    项目地址像 Playwright MCP 的 --executable-path 那样，**在启动时定一次**，
    之后所有工具调用都用它，不给"按调用切项目"的口子。
    想同时服务两个项目 → 起两个实例、不同 serverName。
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

from .support import REPO_ROOT, Server, NOTE_TABLE_3, temp_project


def run_help() -> str:
    r = subprocess.run([sys.executable, "-m", "music_mcp.server", "--help"],
                       cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8")
    return r.stdout


class CommandLine(unittest.TestCase):
    def test_help_mentions_project_flag(self):
        out = run_help()
        self.assertIn("--project", out)
        self.assertIn("--state-dir", out)

    def test_flag_overrides_environment(self):
        """--project 优先于 MUSIC_MCP_PROJECT：这是"启动时定死"的实现方式"""
        with temp_project() as env_dir, temp_project() as flag_dir:
            # 环境变量指向 A，命令行指向 B
            srv = Server(env_dir, extra_args=["--project", flag_dir])
            try:
                text, err = srv.tool("create_midi", filename="flag_wins.mid",
                                     notes=NOTE_TABLE_3)
                self.assertFalse(err, text)
            finally:
                srv.close()
            # 文件必须落在 B（命令行指定的那个），不能落在 A
            self.assertTrue((Path(flag_dir) / "flag_wins.mid").is_file(),
                            "文件应写入 --project 指定的目录")
            self.assertFalse((Path(env_dir) / "flag_wins.mid").exists(),
                             "不应写进环境变量指定的目录")

    def test_creates_missing_project_dir(self):
        """默认自动建目录（首次使用友好）；--project-must-exist 时才报错"""
        with temp_project() as td:
            target = Path(td) / "还不存在" / "深层"
            srv = Server(td, extra_args=["--project", str(target)])
            try:
                text, err = srv.tool("create_folder", name="demo")
                self.assertFalse(err, text)
            finally:
                srv.close()
            self.assertTrue(target.is_dir(), "缺失的项目目录应被自动创建")

    def test_must_exist_flag_exits(self):
        with temp_project() as td:
            missing = Path(td) / "不存在的项目"
            r = subprocess.run(
                [sys.executable, "-m", "music_mcp.server",
                 "--project", str(missing), "--project-must-exist"],
                cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", input="")
            self.assertNotEqual(r.returncode, 0, "应非零退出")
            self.assertIn("不存在", r.stderr + r.stdout)

    def test_state_dir_is_separate_from_project(self):
        """项目根与状态目录解耦：项目根变了，引擎状态资源仍指向原处"""
        with temp_project() as proj, temp_project() as state:
            ana = Path(state) / "分析"
            ana.mkdir()
            (ana / "编排信息.json").write_text('{"demo": true}', encoding="utf-8")

            srv = Server(proj, extra_args=["--state-dir", state])
            try:
                res = srv.result("resources/list")
                names = {r["name"] for r in res["resources"]}
                self.assertIn("编曲设计", names, "state-dir 下的状态文件应被列为资源")
            finally:
                srv.close()


class DeepFind(unittest.TestCase):
    """项目根是大目录树时，裸文件名应能被深搜到"""

    @classmethod
    def setUpClass(cls):
        cls._td = temp_project()
        cls.base = Path(cls._td.name)
        sys.path.insert(0, str(REPO_ROOT))
        from music_mcp.midi_writer import write_smf
        good = write_smf([{"name": "", "notes": [
            {"note": "C4", "velocity": 80, "start": 0, "end": 1}]}], 120)

        deep = cls.base / "01_子项目A" / "MIDI"
        deep.mkdir(parents=True)
        (deep / "唯一.mid").write_bytes(good)         # 有效 MIDI，便于真解析
        # 同名两份，且都**不在**项目根 —— 这样根路径不命中，才走深搜并发现歧义
        for sub in ("02_子项目B/MIDI", "03_子项目C/MIDI"):
            d = cls.base / sub
            d.mkdir(parents=True)
            (d / "重复.mid").write_bytes(good)
        cls.srv = Server(cls.base)

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()
        cls._td.cleanup()

    def test_unique_basename_resolves_and_parses(self):
        """唯一匹配：不用写全相对路径也能解析成功"""
        text, err = self.srv.tool("parse_midi_summary", filename="唯一.mid")
        self.assertFalse(err, text)
        self.assertIn("1 音符", text)

    def test_ambiguous_basename_is_not_guessed(self):
        """多个同名文件 → 报"文件不存在"并列出可用路径，绝不猜"""
        text, err = self.srv.tool("parse_midi_summary", filename="重复.mid")
        self.assertTrue(err, text)
        self.assertIn("文件不存在", text)

    def test_explicit_relative_path_still_works(self):
        text, err = self.srv.tool("parse_midi_summary",
                                  filename="01_子项目A/MIDI/唯一.mid")
        self.assertFalse(err, text)

    def test_path_escape_still_blocked(self):
        for bad in ("../../../etc/passwd", "C:/Windows/win.ini"):
            _, err = self.srv.tool("parse_midi_summary", filename=bad)
            self.assertTrue(err, f"{bad} 必须被拦住")


if __name__ == "__main__":
    unittest.main()
