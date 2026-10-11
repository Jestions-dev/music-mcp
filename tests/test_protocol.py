# -*- coding: utf-8 -*-
"""协议层测试：版本协商、能力声明、resources/*、错误码、isError"""
from __future__ import annotations

import json
import unittest

from .support import Server, find_library, temp_project


class VersionNegotiation(unittest.TestCase):
    """按 MCP 规范 basic/lifecycle：客户端发它支持的版本，服务端回双方都认的"""

    def _init_version(self, requested: str) -> str:
        with temp_project() as td:
            srv = Server(td)
            try:
                # Server.__init__ 已经握手过一次，这里再发一次拿版本
                res = srv.result("initialize", {"protocolVersion": requested,
                                                "capabilities": {}})
                return res["protocolVersion"]
            finally:
                srv.close()

    def test_echoes_supported_version(self):
        self.assertEqual(self._init_version("2025-06-18"), "2025-06-18")

    def test_accepts_older_supported_version(self):
        self.assertEqual(self._init_version("2024-11-05"), "2024-11-05")

    def test_falls_back_for_unknown_version(self):
        """客户端报一个我们不支持的版本 → 回我们支持的最新版，由客户端决定是否断开"""
        self.assertEqual(self._init_version("2099-01-01"), "2025-06-18")


class Capabilities(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._td = temp_project()
        cls.srv = Server(cls._td.name)

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()
        cls._td.cleanup()

    def test_declares_tools_and_resources(self):
        res = self.srv.result("initialize", {"protocolVersion": "2025-06-18",
                                             "capabilities": {}})
        caps = res["capabilities"]
        self.assertIn("tools", caps)
        self.assertIn("resources", caps)
        self.assertEqual(res["serverInfo"]["name"], "music-mcp")

    def test_list_changed_declarations_are_truthful(self):
        """声明了 listChanged=true 就必须真的会发对应通知（我们在 initialized 后发）"""
        res = self.srv.result("initialize", {"protocolVersion": "2025-06-18",
                                             "capabilities": {}})
        caps = res["capabilities"]
        self.assertIs(caps["tools"]["listChanged"], True)
        self.assertIs(caps["resources"]["listChanged"], True)
        # resources 不支持订阅，声明必须为 false
        self.assertIs(caps["resources"]["subscribe"], False)


class Resources(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._td = temp_project()
        cls.lib = find_library()
        cls.srv = Server(cls._td.name, library_dir=cls.lib)

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()
        cls._td.cleanup()

    def test_list_returns_array(self):
        res = self.srv.result("resources/list")
        self.assertIsInstance(res["resources"], list)

    def test_all_uris_use_declared_scheme(self):
        res = self.srv.result("resources/list")
        for item in res["resources"]:
            self.assertTrue(item["uri"].startswith("music://"), item["uri"])
            self.assertTrue(item.get("name"), item)
            self.assertTrue(item.get("mimeType"), item)

    def test_read_theory_file(self):
        if not self.lib:
            self.skipTest("未找到 AI_MIDI Library")
        res = self.srv.result("resources/list")
        theory = [r for r in res["resources"] if r["uri"].startswith("music://theory/")]
        self.assertTrue(theory, "知识库应至少有一篇")
        got = self.srv.result("resources/read", {"uri": theory[0]["uri"]})
        c = got["contents"][0]
        self.assertEqual(c["uri"], theory[0]["uri"])
        self.assertTrue(c["text"].strip())

    def test_read_unknown_resource_is_error(self):
        msg = self.srv.call("resources/read", {"uri": "music://theory/不存在的文件.md"})
        self.assertIsNotNone(msg.get("error"), msg)
        self.assertEqual(msg["error"]["code"], -32602)

    def test_templates_list_available(self):
        res = self.srv.result("resources/templates/list")
        self.assertTrue(res["resourceTemplates"])
        for t in res["resourceTemplates"]:
            self.assertIn("uriTemplate", t)


class ToolErrors(unittest.TestCase):
    """工具的 JSON-RPC 契约：成功不带 isError，失败必须带"""

    @classmethod
    def setUpClass(cls):
        cls._td = temp_project()
        cls.srv = Server(cls._td.name)

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()
        cls._td.cleanup()

    def test_success_has_no_iserror(self):
        text, is_err = self.srv.tool("list_midi_files")
        self.assertFalse(is_err, text)

    def test_missing_file_sets_iserror(self):
        text, is_err = self.srv.tool("parse_midi", filename="definitely-missing.mid")
        self.assertTrue(is_err)
        self.assertIn("不存在", text)

    def test_unknown_tool_sets_iserror(self):
        _, is_err = self.srv.tool("no_such_tool_at_all")
        self.assertTrue(is_err)

    def test_path_escape_is_blocked(self):
        for bad in ("../../../etc/passwd", "C:/Windows/win.ini"):
            _, is_err = self.srv.tool("parse_midi", filename=bad)
            self.assertTrue(is_err, f"{bad} 应被拦住")

    def test_unknown_method_returns_32601(self):
        msg = self.srv.call("prompts/list")
        self.assertEqual(msg.get("error", {}).get("code"), -32601)

    def test_ping(self):
        msg = self.srv.call("ping")
        self.assertIn("result", msg)


class MalformedInput(unittest.TestCase):
    """坏输入不能让服务器死掉"""

    @classmethod
    def setUpClass(cls):
        cls._td = temp_project()
        cls.srv = Server(cls._td.name)

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()
        cls._td.cleanup()

    def test_bad_json_line_is_ignored(self):
        self.srv.proc.stdin.write("这不是 JSON\n")
        self.srv.proc.stdin.flush()
        # 服务器应继续服务
        self.assertIn("result", self.srv.call("ping"))

    def test_arguments_not_object(self):
        _, is_err = self.srv.tool("list_midi_files")
        self.assertFalse(is_err)
        msg = self.srv.call("tools/call", {"name": "parse_midi", "arguments": "字符串"})
        self.assertTrue((msg.get("result") or {}).get("isError"), msg)


if __name__ == "__main__":
    unittest.main()
