#!/usr/bin/env python3
"""Tests for P0-2 and P0-3 improvements in feishu_doc.py.

These tests use mocks since they involve Feishu API calls.
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch, call
import time

# Add scripts dir to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from md_to_blocks import BLOCK_TYPE_TABLE, BLOCK_TYPE_TEXT


class TestTableCreateRetry(unittest.TestCase):
    """Test P0-2: _write_table_block() Step 1 retry logic."""

    def _make_table_dict(self, rows=3, cols=2):
        """Helper: create a table dict."""
        data_rows = [
            [f"H{j+1}" for j in range(cols)]
        ] + [
            [f"R{i+1}C{j+1}" for j in range(cols)]
            for i in range(rows - 1)
        ]
        return {
            "block_type": BLOCK_TYPE_TABLE,
            "table": {
                "rows": data_rows,
                "column_size": cols,
                "header_row": True,
                "column_widths": [200] * cols,
            }
        }

    @patch('feishu_doc._get_tenant_token', return_value='fake_token')
    @patch('feishu_doc.http_requests')
    def test_retry_on_rate_limit(self, mock_http, mock_token):
        """Table creation retries on rate limit (code 99991400)."""
        # Import here to ensure patches are applied
        from feishu_doc import _write_table_block

        # Mock client
        client = MagicMock()

        # First call: rate limited, Second call: success
        fail_response = MagicMock()
        fail_response.success.return_value = False
        fail_response.code = 99991400
        fail_response.msg = "rate limited"

        success_response = MagicMock()
        success_response.success.return_value = True
        success_response.data = None  # Empty data — should return True gracefully

        client.docx.v1.document_block_children.create.side_effect = [
            fail_response, success_response
        ]

        table_dict = self._make_table_dict(rows=3, cols=2)
        result = _write_table_block(client, "doc123", table_dict)

        # Should succeed (retry worked)
        self.assertTrue(result)
        # Should have been called twice (1 fail + 1 success)
        self.assertEqual(client.docx.v1.document_block_children.create.call_count, 2)

    def test_empty_response_data_handled(self):
        """Table creation handles empty response.data gracefully."""
        from feishu_doc import _write_table_block

        client = MagicMock()

        # Success but response.data is None
        response = MagicMock()
        response.success.return_value = True
        response.data = None

        client.docx.v1.document_block_children.create.return_value = response

        table_dict = self._make_table_dict(rows=3, cols=2)
        result = _write_table_block(client, "doc123", table_dict)

        # Should return True (table created, just no cell IDs to fill)
        self.assertTrue(result)

    def test_non_retryable_error_fails_immediately(self):
        """Non-rate-limit errors should not retry."""
        from feishu_doc import _write_table_block

        client = MagicMock()

        fail_response = MagicMock()
        fail_response.success.return_value = False
        fail_response.code = 1770001
        fail_response.msg = "invalid param"

        client.docx.v1.document_block_children.create.return_value = fail_response

        table_dict = self._make_table_dict(rows=3, cols=2)
        result = _write_table_block(client, "doc123", table_dict)

        # Should fail
        self.assertFalse(result)
        # Should only be called once (no retry for non-rate-limit errors)
        self.assertEqual(client.docx.v1.document_block_children.create.call_count, 1)


class TestTableDelayBetweenTables(unittest.TestCase):
    """Test P0-3: _write_blocks_to_doc() adds delay between tables."""

    @patch('feishu_doc._write_table_block')
    @patch('feishu_doc._write_regular_blocks')
    @patch('feishu_doc.time')
    def test_delay_between_consecutive_tables(self, mock_time, mock_regular, mock_table):
        """Consecutive table blocks should have 3s delay between them."""
        from feishu_doc import _write_blocks_to_doc

        mock_regular.return_value = 1
        mock_table.return_value = True

        # Simulate: text + table + table
        block_dicts = [
            {"block_type": BLOCK_TYPE_TEXT, "text": {"elements": [{"text_run": {"content": "hi", "text_element_style": {}}}]}},
            {"block_type": BLOCK_TYPE_TABLE, "table": {"rows": [["A", "B"], ["1", "2"]], "column_size": 2, "header_row": True, "column_widths": [200, 200]}},
            {"block_type": BLOCK_TYPE_TABLE, "table": {"rows": [["X", "Y"], ["3", "4"]], "column_size": 2, "header_row": True, "column_widths": [200, 200]}},
        ]

        client = MagicMock()
        _write_blocks_to_doc(client, "doc123", block_dicts)

        # Check that time.sleep(3) was called (between the two tables)
        sleep_calls = [c for c in mock_time.sleep.call_args_list if c == call(3)]
        self.assertGreaterEqual(len(sleep_calls), 1,
                                "Should have at least one 3s sleep between tables")

    @patch('feishu_doc._write_table_block')
    @patch('feishu_doc._write_regular_blocks')
    @patch('feishu_doc.time')
    def test_no_delay_for_first_table(self, mock_time, mock_regular, mock_table):
        """First table should not have delay."""
        from feishu_doc import _write_blocks_to_doc

        mock_regular.return_value = 1
        mock_table.return_value = True

        # Simulate: just one table
        block_dicts = [
            {"block_type": BLOCK_TYPE_TABLE, "table": {"rows": [["A", "B"], ["1", "2"]], "column_size": 2, "header_row": True, "column_widths": [200, 200]}},
        ]

        client = MagicMock()
        _write_blocks_to_doc(client, "doc123", block_dicts)

        # No 3s sleep for single table
        sleep_calls = [c for c in mock_time.sleep.call_args_list if c == call(3)]
        self.assertEqual(len(sleep_calls), 0,
                         "Single table should not trigger delay")

    @patch('feishu_doc._write_table_block')
    @patch('feishu_doc._write_regular_blocks')
    @patch('feishu_doc.time')
    def test_no_delay_for_regular_blocks(self, mock_time, mock_regular, mock_table):
        """Regular text blocks should not trigger any delay."""
        from feishu_doc import _write_blocks_to_doc

        mock_regular.return_value = 2
        mock_table.return_value = True

        # Simulate: two text blocks only
        block_dicts = [
            {"block_type": BLOCK_TYPE_TEXT, "text": {"elements": [{"text_run": {"content": "hi", "text_element_style": {}}}]}},
            {"block_type": BLOCK_TYPE_TEXT, "text": {"elements": [{"text_run": {"content": "bye", "text_element_style": {}}}]}},
        ]

        client = MagicMock()
        _write_blocks_to_doc(client, "doc123", block_dicts)

        # No 3s sleep at all
        sleep_calls = [c for c in mock_time.sleep.call_args_list if c == call(3)]
        self.assertEqual(len(sleep_calls), 0)


if __name__ == "__main__":
    unittest.main()
