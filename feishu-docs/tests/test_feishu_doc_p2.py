#!/usr/bin/env python3
"""Tests for P2-B: Enhanced error messages in _write_table_block().

Tests verify that error messages include table dimensions and
appropriate diagnostic hints for specific error codes.
"""

import sys
import os
import json
import unittest
from unittest.mock import MagicMock, patch
from io import StringIO

# Add scripts dir to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from md_to_blocks import BLOCK_TYPE_TABLE


class TestTableCreateErrorMessages(unittest.TestCase):
    """Test P2-B: _write_table_block() error message enhancement."""

    def _make_table_dict(self, rows=3, cols=2):
        """Helper: create a table dict with specified dimensions."""
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

    def test_invalid_param_with_rows_over_9(self):
        """Error code 1770001 + rows > 9 should include row limit hint."""
        from feishu_doc import _write_table_block

        client = MagicMock()

        fail_response = MagicMock()
        fail_response.success.return_value = False
        fail_response.code = 1770001
        fail_response.msg = "invalid param"

        client.docx.v1.document_block_children.create.return_value = fail_response

        table_dict = self._make_table_dict(rows=11, cols=7)

        # Capture stderr
        captured_stderr = StringIO()
        with patch('sys.stderr', captured_stderr):
            result = _write_table_block(client, "doc123", table_dict)

        self.assertFalse(result)
        stderr_output = captured_stderr.getvalue()

        # Should contain table dimensions
        self.assertIn("rows=11", stderr_output)
        self.assertIn("cols=7", stderr_output)
        # Should contain the error code and message
        self.assertIn("1770001", stderr_output)
        self.assertIn("invalid param", stderr_output)
        # Should contain the row limit hint
        self.assertIn("飞书限制单次创建最多 9 行", stderr_output)

    def test_invalid_param_with_rows_under_9(self):
        """Error code 1770001 + rows <= 9 should NOT include row limit hint."""
        from feishu_doc import _write_table_block

        client = MagicMock()

        fail_response = MagicMock()
        fail_response.success.return_value = False
        fail_response.code = 1770001
        fail_response.msg = "invalid param"

        client.docx.v1.document_block_children.create.return_value = fail_response

        table_dict = self._make_table_dict(rows=5, cols=3)

        captured_stderr = StringIO()
        with patch('sys.stderr', captured_stderr):
            result = _write_table_block(client, "doc123", table_dict)

        self.assertFalse(result)
        stderr_output = captured_stderr.getvalue()

        # Should contain dimensions
        self.assertIn("rows=5", stderr_output)
        self.assertIn("cols=3", stderr_output)
        # Should NOT contain row limit hint (rows <= 9)
        self.assertNotIn("飞书限制单次创建最多 9 行", stderr_output)

    @patch('feishu_doc._get_tenant_token', return_value='fake_token')
    @patch('feishu_doc.http_requests')
    def test_rate_limit_retry_message_includes_dimensions(self, mock_http, mock_token):
        """Rate limit retry message should include table dimensions and hint."""
        from feishu_doc import _write_table_block

        client = MagicMock()

        # First call: rate limited, second call: success with empty data
        fail_response = MagicMock()
        fail_response.success.return_value = False
        fail_response.code = 99991400
        fail_response.msg = "rate limited"

        success_response = MagicMock()
        success_response.success.return_value = True
        success_response.data = None

        client.docx.v1.document_block_children.create.side_effect = [
            fail_response, success_response
        ]

        table_dict = self._make_table_dict(rows=8, cols=4)

        captured_stderr = StringIO()
        with patch('sys.stderr', captured_stderr):
            result = _write_table_block(client, "doc123", table_dict)

        self.assertTrue(result)
        stderr_output = captured_stderr.getvalue()

        # Retry message should contain dimensions and rate limit hint
        self.assertIn("rows=8", stderr_output)
        self.assertIn("cols=4", stderr_output)
        self.assertIn("触发频率限制，将自动重试", stderr_output)

    def test_rate_limit_final_failure_message(self):
        """Rate limit error on final attempt should include dimensions and hint."""
        from feishu_doc import _write_table_block

        client = MagicMock()

        # All attempts fail with rate limit
        fail_response = MagicMock()
        fail_response.success.return_value = False
        fail_response.code = 99991400
        fail_response.msg = "rate limited"

        client.docx.v1.document_block_children.create.return_value = fail_response

        table_dict = self._make_table_dict(rows=6, cols=3)

        captured_stderr = StringIO()
        with patch('sys.stderr', captured_stderr):
            result = _write_table_block(client, "doc123", table_dict)

        self.assertFalse(result)
        stderr_output = captured_stderr.getvalue()

        # Final failure message should contain dimensions
        self.assertIn("rows=6", stderr_output)
        self.assertIn("cols=3", stderr_output)
        # Should contain rate limit hint in at least one of the messages
        self.assertIn("触发频率限制", stderr_output)

    def test_error_message_format_with_dimensions(self):
        """Verify the exact format: 'Table create failed (rows=N, cols=M): [code] msg'."""
        from feishu_doc import _write_table_block

        client = MagicMock()

        fail_response = MagicMock()
        fail_response.success.return_value = False
        fail_response.code = 12345
        fail_response.msg = "some error"

        client.docx.v1.document_block_children.create.return_value = fail_response

        table_dict = self._make_table_dict(rows=4, cols=5)

        captured_stderr = StringIO()
        with patch('sys.stderr', captured_stderr):
            result = _write_table_block(client, "doc123", table_dict)

        self.assertFalse(result)
        stderr_output = captured_stderr.getvalue()

        # Parse the JSON from stderr
        # The error message should be in JSON format
        self.assertIn("Table create failed (rows=4, cols=5): [12345] some error", stderr_output)


if __name__ == "__main__":
    unittest.main()
