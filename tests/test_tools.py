# -*- coding: utf-8 -*-
"""工具层测试：10 个工具的完整生命周期 + 往返一致性"""
from __future__ import annotations

import json
import unittest

from .support import NOTE_TABLE_3, NOTE_TABLE_JSON, Server, find_library, temp_project

EXPECTED_TOOLS = {
    # 对齐 AI_MIDI 的 7 个
    "read_library_file", "list_midi_files", "parse_midi", "create_midi",
    "delete_midi", "create_folder", "list_project_structure",
    # 本服务器独有的 3 个
    "parse_midi_summary", "parse_midi_tracks", "validate_note_table",
}


class ToolCatalog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._td = temp_project()
        cls.srv = Server(cls._td.name)

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()
        cls._td.cleanup()

    def test_exposes_all_ten_tools(self):
        names = {t["name"] for t in self.srv.result("tools/list")["tools"]}
        self.assertEqual(names, EXPECTED_TOOLS, f"缺失 {EXPECTED_TOOLS - names}")

    def test_every_tool_has_schema(self):
        for t in self.srv.result("tools/list")["tools"]:
            self.assertIsInstance(t.get("description"), str, t["name"])
            self.assertTrue(t["description"].strip(), t["name"])
            self.assertEqual(t["inputSchema"]["type"], "object", t["name"])


class MidiLifecycle(unittest.TestCase):
    """建目录 → 写单轨 → 写多轨 → 读回 → 删除"""

    @classmethod
    def setUpClass(cls):
        cls._td = temp_project()
        cls.srv = Server(cls._td.name)

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()
        cls._td.cleanup()

    def test_01_create_folder(self):
        text, err = self.srv.tool("create_folder", name="sub/dir")
        self.assertFalse(err, text)
        self.assertIn("成功", text)

    def test_02_create_single_track(self):
        text, err = self.srv.tool("create_midi", filename="sub/dir/one.mid",
                                  bpm=100, notes=NOTE_TABLE_3)
        self.assertFalse(err, text)
        self.assertIn("Format 0", text)
        self.assertIn("3 个音符", text)

    def test_03_create_multi_track(self):
        """AI_MIDI 只能写单轨，多轨是本服务器的增量能力"""
        text, err = self.srv.tool("create_midi", filename="sub/dir/many.mid", bpm=128, tracks=[
            {"name": "旋律", "notes": NOTE_TABLE_3},
            {"name": "低音", "notes": '[note: "C3", velocity: "70", start: "0", end: "3"]'},
            {"name": "鼓", "notes": NOTE_TABLE_JSON},
        ])
        self.assertFalse(err, text)
        self.assertIn("Format 1", text)
        self.assertIn("3 轨", text)

    def test_04_summary_of_single(self):
        text, err = self.srv.tool("parse_midi_summary", filename="sub/dir/one.mid")
        self.assertFalse(err, text)
        self.assertIn("格式 0", text)
        self.assertIn("C4", text)
        self.assertIn("音域", text)

    def test_05_summary_keeps_track_names(self):
        text, err = self.srv.tool("parse_midi_summary", filename="sub/dir/many.mid")
        self.assertFalse(err, text)
        self.assertIn("格式 1", text)
        for name in ("旋律", "低音", "鼓"):
            self.assertIn(f"「{name}」", text)

    def test_06_read_tracks(self):
        text, err = self.srv.tool("parse_midi_tracks", filename="sub/dir/many.mid",
                                  track=0)
        self.assertFalse(err, text)
        self.assertIn("旋律", text)
        self.assertIn('[note: "', text)

    def test_07_roundtrip_note_table_format(self):
        """扁平输出必须与 AI_MIDI 的 note_table 格式一致（它要能读回去）"""
        text, err = self.srv.tool("parse_midi", filename="sub/dir/one.mid")
        self.assertFalse(err, text)
        first = text.strip().splitlines()[0]
        self.assertTrue(first.startswith('[note: "'), first)
        for field in ("note:", "velocity:", "start:", "end:"):
            self.assertIn(field, first)

    def test_08_json_input_accepted(self):
        text, err = self.srv.tool("create_midi", filename="sub/dir/json.mid",
                                  notes=NOTE_TABLE_JSON)
        self.assertFalse(err, text)
        self.assertIn("2 个音符", text)

    def test_09_list_and_structure(self):
        text, err = self.srv.tool("list_midi_files")
        self.assertFalse(err, text)
        self.assertIn("one.mid", text)

        text, err = self.srv.tool("list_project_structure")
        self.assertFalse(err, text)
        self.assertIn("sub", text)
        self.assertIn("one.mid", text)

    def test_10_delete_and_cleanup(self):
        for f in ("one.mid", "many.mid", "json.mid"):
            text, err = self.srv.tool("delete_midi", filename=f"sub/dir/{f}")
            self.assertFalse(err, text)
        _, err = self.srv.tool("list_midi_files")
        self.assertFalse(err)

    def test_11_delete_rejects_non_midi(self):
        _, err = self.srv.tool("delete_midi", filename="sub/dir/notes.txt")
        self.assertTrue(err, "只应允许删 MIDI")


class ValidateNoteTable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._td = temp_project()
        cls.srv = Server(cls._td.name)

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()
        cls._td.cleanup()

    def test_valid_table(self):
        text, _ = self.srv.tool("validate_note_table", note_table=NOTE_TABLE_3)
        self.assertIn("3 个音符", text)
        self.assertIn("无问题", text)

    def test_line_format_does_not_fake_json_error(self):
        """行格式以 [ 开头，曾经被误判成「看起来像 JSON 但解析失败」。

        判据要精确到"报错内容"，不能只是 text 里出现 JSON 二字——
        工具末尾的提示语本来就写着「或 JSON 数组」。
        """
        text, _ = self.srv.tool("validate_note_table", note_table=NOTE_TABLE_3)
        self.assertNotIn("解析失败", text)
        self.assertNotIn("看起来像 JSON", text)

    def test_reports_missing_fields_with_line_numbers(self):
        bad = '[note: "C4", velocity: "80", start: "0", end: "1"]\n[note: "E4", start: "1"]'
        text, _ = self.srv.tool("validate_note_table", note_table=bad)
        self.assertIn("第 2 行缺少字段", text)
        self.assertIn("end", text)

    def test_reports_end_before_start(self):
        bad = '[note: "C4", velocity: "80", start: "3", end: "1"]'
        text, _ = self.srv.tool("validate_note_table", note_table=bad)
        self.assertIn("end(1.0) < start(3.0)", text)

    def test_reports_out_of_range_velocity(self):
        bad = '[note: "C4", velocity: "999", start: "0", end: "1"]'
        text, _ = self.srv.tool("validate_note_table", note_table=bad)
        self.assertIn("力度", text)

    def test_json_input_clean(self):
        text, _ = self.srv.tool("validate_note_table", note_table=NOTE_TABLE_JSON)
        self.assertIn("2 个音符", text)
        self.assertIn("无问题", text)


class LibraryTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._td = temp_project()
        cls.lib = find_library()
        cls.srv = Server(cls._td.name, library_dir=cls.lib)

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()
        cls._td.cleanup()

    def test_read_known_file(self):
        if not self.lib:
            self.skipTest("未找到 AI_MIDI Library")
        names = sorted(p.name for p in self.lib.glob("*.md"))
        text, err = self.srv.tool("read_library_file", filename=names[0])
        self.assertFalse(err, text)
        self.assertTrue(text.strip())

    def test_missing_file_lists_alternatives(self):
        if not self.lib:
            self.skipTest("未找到 AI_MIDI Library")
        text, err = self.srv.tool("read_library_file", filename="绝对不存在.md")
        self.assertTrue(err)
        self.assertIn("可用文件", text)

    def test_rejects_non_text_extension(self):
        text, err = self.srv.tool("read_library_file", filename="soundfonts/GeneralUser GS v1.471.sf2")
        self.assertTrue(err, text)


class ParameterClamping(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._td = temp_project()
        cls.srv = Server(cls._td.name)

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()
        cls._td.cleanup()

    def test_bpm_clamped_to_range(self):
        text, err = self.srv.tool("create_midi", filename="clamp.mid", bpm=9999,
                                  notes=NOTE_TABLE_3)
        self.assertFalse(err, text)
        self.assertIn("BPM 600", text)      # 上限 600
        self.srv.tool("delete_midi", filename="clamp.mid")

    def test_default_bpm(self):
        text, err = self.srv.tool("create_midi", filename="default.mid", notes=NOTE_TABLE_3)
        self.assertFalse(err, text)
        self.assertIn("BPM 120", text)
        self.srv.tool("delete_midi", filename="default.mid")

    def test_filename_extension_auto_added(self):
        text, err = self.srv.tool("create_midi", filename="noext", notes=NOTE_TABLE_3)
        self.assertFalse(err, text)
        self.assertIn(".mid", text)
        self.srv.tool("delete_midi", filename="noext.mid")

    def test_csv_input_rejected_with_hint(self):
        text, err = self.srv.tool("create_midi", filename="csv.mid",
                                  notes="C4,80,0,1\nE4,80,1,2")
        self.assertTrue(err, text)
        self.assertIn("没有解析出任何音符", text)


if __name__ == "__main__":
    unittest.main()
