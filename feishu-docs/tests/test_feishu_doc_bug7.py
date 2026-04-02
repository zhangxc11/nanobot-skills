#!/usr/bin/env python3
"""Tests for Bug #7: read --format blocks — table cell text reading.

Tests verify that _block_to_dict handles table blocks (block_type=31)
and that _read_blocks resolves cell content via parent-child relationships.
"""

import sys
import os
import unittest
from unittest.mock import MagicMock

# Add scripts dir to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))


class TestBlockToDictTable(unittest.TestCase):
    """Test _block_to_dict handling of table blocks (block_type=31)."""

    def _make_mock_block(self, block_id, block_type, parent_id=None, children=None):
        """Create a mock lark-oapi Block object."""
        block = MagicMock()
        block.block_id = block_id
        block.block_type = block_type
        block.parent_id = parent_id
        block.children = children
        # Default: no text fields
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

    def _make_mock_text_block(self, block_id, content, parent_id=None):
        """Create a mock text block (block_type=2) with content."""
        block = self._make_mock_block(block_id, 2, parent_id=parent_id)
        text_obj = MagicMock()
        elem = MagicMock()
        elem.text_run = MagicMock()
        elem.text_run.content = content
        text_obj.elements = [elem]
        block.text = text_obj
        return block

    def test_table_block_has_table_info(self):
        """Table block (type=31) should include table property in result."""
        from feishu_doc import _block_to_dict

        block = self._make_mock_block("tbl_001", 31, children=["cell_001", "cell_002"])
        table_obj = MagicMock()
        prop = MagicMock()
        prop.row_size = 2
        prop.column_size = 2
        prop.header_row = True
        table_obj.property = prop
        table_obj.cells = ["cell_001", "cell_002", "cell_003", "cell_004"]
        block.table = table_obj

        result = _block_to_dict(block)
        self.assertEqual(result["block_type"], 31)
        self.assertIn("table", result)
        self.assertEqual(result["table"]["row_size"], 2)
        self.assertEqual(result["table"]["column_size"], 2)
        self.assertTrue(result["table"]["header_row"])
        self.assertEqual(result["table"]["cells"], ["cell_001", "cell_002", "cell_003", "cell_004"])

    def test_table_block_without_table_obj(self):
        """Table block without table attribute should not crash."""
        from feishu_doc import _block_to_dict

        block = self._make_mock_block("tbl_002", 31)
        block.table = None
        result = _block_to_dict(block)
        self.assertEqual(result["block_type"], 31)
        self.assertNotIn("table", result)

    def test_table_block_without_property(self):
        """Table block with table but no property should handle gracefully."""
        from feishu_doc import _block_to_dict

        block = self._make_mock_block("tbl_003", 31)
        table_obj = MagicMock()
        table_obj.property = None
        table_obj.cells = ["c1", "c2"]
        block.table = table_obj

        result = _block_to_dict(block)
        self.assertIn("table", result)
        self.assertEqual(result["table"]["cells"], ["c1", "c2"])

    def test_text_block_still_works(self):
        """Regular text blocks should still work correctly."""
        from feishu_doc import _block_to_dict

        block = self._make_mock_text_block("txt_001", "Hello world")
        result = _block_to_dict(block)
        self.assertEqual(result["content"], "Hello world")
        self.assertNotIn("table", result)


class TestReadBlocksTableCellContent(unittest.TestCase):
    """Test that _read_blocks resolves table cell content via parent-child relationships."""

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

    def _make_mock_text_block(self, block_id, content, parent_id=None):
        block = self._make_mock_block(block_id, 2, parent_id=parent_id)
        text_obj = MagicMock()
        elem = MagicMock()
        elem.text_run = MagicMock()
        elem.text_run.content = content
        text_obj.elements = [elem]
        block.text = text_obj
        return block

    @unittest.mock.patch('feishu_doc.create_client')
    def test_table_cell_contents_resolved(self, mock_create_client):
        """Table cells should have their text content resolved in the output."""
        from feishu_doc import _read_blocks
        import json

        client = MagicMock()

        # Build mock blocks: page → table → cells → text
        page_block = self._make_mock_block("page_001", 1, children=["tbl_001"])

        table_block = self._make_mock_block("tbl_001", 31, parent_id="page_001",
                                            children=["cell_001", "cell_002"])
        table_obj = MagicMock()
        prop = MagicMock()
        prop.row_size = 1
        prop.column_size = 2
        prop.header_row = True
        table_obj.property = prop
        table_obj.cells = ["cell_001", "cell_002"]
        table_block.table = table_obj

        cell1_block = self._make_mock_block("cell_001", 32, parent_id="tbl_001",
                                            children=["txt_001"])
        cell2_block = self._make_mock_block("cell_002", 32, parent_id="tbl_001",
                                            children=["txt_002"])

        txt1_block = self._make_mock_text_block("txt_001", "Header A", parent_id="cell_001")
        txt2_block = self._make_mock_text_block("txt_002", "Header B", parent_id="cell_002")

        # Mock API response
        response = MagicMock()
        response.success.return_value = True
        response.data.items = [page_block, table_block, cell1_block, cell2_block,
                               txt1_block, txt2_block]

        client.docx.v1.document_block.list.return_value = response

        # Capture stdout
        import io
        from unittest.mock import patch
        with patch('sys.stdout', new_callable=io.StringIO) as mock_stdout:
            result = _read_blocks(client, "doc_test")

        output = mock_stdout.getvalue()
        data = json.loads(output)

        self.assertTrue(data["success"])

        # Find the table block in output
        table_blocks = [b for b in data["blocks"] if b["block_type"] == 31]
        self.assertEqual(len(table_blocks), 1)

        table_data = table_blocks[0]["table"]
        self.assertIn("cell_contents", table_data)
        self.assertEqual(table_data["cell_contents"], ["Header A", "Header B"])


if __name__ == "__main__":
    unittest.main()
