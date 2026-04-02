#!/usr/bin/env python3
"""Tests for Bug #9: read --format blocks JSON serialization robustness.

Tests verify that _safe_serialize handles various types correctly
and that _block_to_dict always returns JSON-serializable output.
"""

import sys
import os
import json
import unittest
from unittest.mock import MagicMock

# Add scripts dir to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))


class TestSafeSerialize(unittest.TestCase):
    """Test _safe_serialize handles various types correctly."""

    def test_none(self):
        from feishu_doc import _safe_serialize
        self.assertIsNone(_safe_serialize(None))

    def test_string(self):
        from feishu_doc import _safe_serialize
        self.assertEqual(_safe_serialize("hello"), "hello")

    def test_int(self):
        from feishu_doc import _safe_serialize
        self.assertEqual(_safe_serialize(42), 42)

    def test_float(self):
        from feishu_doc import _safe_serialize
        self.assertEqual(_safe_serialize(3.14), 3.14)

    def test_bool(self):
        from feishu_doc import _safe_serialize
        self.assertTrue(_safe_serialize(True))
        self.assertFalse(_safe_serialize(False))

    def test_list(self):
        from feishu_doc import _safe_serialize
        result = _safe_serialize([1, "two", None])
        self.assertEqual(result, [1, "two", None])

    def test_dict(self):
        from feishu_doc import _safe_serialize
        result = _safe_serialize({"key": "value", "num": 42})
        self.assertEqual(result, {"key": "value", "num": 42})

    def test_nested_dict(self):
        from feishu_doc import _safe_serialize
        result = _safe_serialize({"a": {"b": [1, 2, {"c": 3}]}})
        self.assertEqual(result, {"a": {"b": [1, 2, {"c": 3}]}})

    def test_lark_oapi_object(self):
        """Objects with __dict__ should be converted to dicts."""
        from feishu_doc import _safe_serialize

        class FakeObj:
            def __init__(self):
                self.name = "test"
                self.value = 42
                self._private = "hidden"

        obj = FakeObj()
        result = _safe_serialize(obj)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["name"], "test")
        self.assertEqual(result["value"], 42)
        self.assertNotIn("_private", result)

    def test_nested_lark_object(self):
        """Nested lark-oapi objects should be recursively serialized."""
        from feishu_doc import _safe_serialize

        class InnerObj:
            def __init__(self):
                self.data = "inner"

        class OuterObj:
            def __init__(self):
                self.child = InnerObj()
                self.items = [InnerObj(), "text"]

        result = _safe_serialize(OuterObj())
        self.assertEqual(result["child"]["data"], "inner")
        self.assertEqual(result["items"][0]["data"], "inner")
        self.assertEqual(result["items"][1], "text")

    def test_fallback_to_str(self):
        """Unknown types without __dict__ should be converted to str."""
        from feishu_doc import _safe_serialize

        result = _safe_serialize(set([1, 2, 3]))
        self.assertIsInstance(result, str)

    def test_result_is_json_serializable(self):
        """The output of _safe_serialize should always be JSON-serializable."""
        from feishu_doc import _safe_serialize

        class ComplexObj:
            def __init__(self):
                self.name = "test"
                self.data = set([1, 2])
                self.nested = MagicMock()

        result = _safe_serialize(ComplexObj())
        # Should not raise
        json_str = json.dumps(result, ensure_ascii=False)
        self.assertIsInstance(json_str, str)


class TestBlockToDictSerialization(unittest.TestCase):
    """Test that _block_to_dict output is always JSON-serializable."""

    def _make_mock_block(self, block_id, block_type, parent_id=None, children=None):
        block = MagicMock()
        block.block_id = block_id
        block.block_type = block_type
        block.parent_id = parent_id
        block.children = children
        block.text = None
        block.heading1 = None
        block.heading2 = None
        block.heading3 = None
        block.heading4 = None
        block.heading5 = None
        block.heading6 = None
        block.heading7 = None
        block.heading8 = None
        block.heading9 = None
        block.bullet = None
        block.ordered = None
        block.code = None
        block.quote = None
        block.todo = None
        block.table = None
        return block

    def test_text_block_serializable(self):
        from feishu_doc import _block_to_dict

        block = self._make_mock_block("b1", 2)
        text_obj = MagicMock()
        elem = MagicMock()
        elem.text_run = MagicMock()
        elem.text_run.content = "Hello"
        text_obj.elements = [elem]
        block.text = text_obj

        result = _block_to_dict(block)
        json_str = json.dumps(result, ensure_ascii=False)
        self.assertIsInstance(json_str, str)
        parsed = json.loads(json_str)
        self.assertEqual(parsed["content"], "Hello")

    def test_table_block_serializable(self):
        from feishu_doc import _block_to_dict

        block = self._make_mock_block("t1", 31, children=["c1"])
        table_obj = MagicMock()
        prop = MagicMock()
        prop.row_size = 2
        prop.column_size = 3
        prop.header_row = True
        table_obj.property = prop
        table_obj.cells = ["c1", "c2", "c3", "c4", "c5", "c6"]
        block.table = table_obj

        result = _block_to_dict(block)
        json_str = json.dumps(result, ensure_ascii=False)
        self.assertIsInstance(json_str, str)
        parsed = json.loads(json_str)
        self.assertEqual(parsed["table"]["row_size"], 2)

    def test_unknown_block_type_serializable(self):
        """Unknown block types should still produce valid JSON."""
        from feishu_doc import _block_to_dict

        block = self._make_mock_block("u1", 99)
        result = _block_to_dict(block)
        json_str = json.dumps(result, ensure_ascii=False)
        self.assertIsInstance(json_str, str)

    def test_divider_block_serializable(self):
        from feishu_doc import _block_to_dict

        block = self._make_mock_block("d1", 22)
        result = _block_to_dict(block)
        json_str = json.dumps(result, ensure_ascii=False)
        self.assertIsInstance(json_str, str)


if __name__ == "__main__":
    unittest.main()
