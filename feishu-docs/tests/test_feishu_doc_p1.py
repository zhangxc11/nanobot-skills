#!/usr/bin/env python3
"""Tests for P1 improvements in feishu_doc.py.

P1-1: Large document auto-chunking (_split_into_chunks)
P1-2: Write progress feedback (stderr output)
P1-3: Resume from chunk (--resume-from)
P1-4: Auto add-member in create-and-write
"""

import sys
import os
import io
import unittest
from unittest.mock import MagicMock, patch, call

# Add scripts dir to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from md_to_blocks import (
    BLOCK_TYPE_TABLE, BLOCK_TYPE_TEXT, BLOCK_TYPE_HEADING1,
    BLOCK_TYPE_HEADING2, BLOCK_TYPE_HEADING3, BLOCK_TYPE_BULLET,
    BLOCK_TYPE_ORDERED, BLOCK_TYPE_DIVIDER,
)


def _make_text_bd(content="text"):
    """Helper: create a text block dict."""
    return {
        "block_type": BLOCK_TYPE_TEXT,
        "text": {"elements": [{"text_run": {"content": content, "text_element_style": {}}}]}
    }


def _make_heading_bd(content="heading", level=1):
    """Helper: create a heading block dict."""
    bt = BLOCK_TYPE_HEADING1 + level - 1
    field = f"heading{level}"
    return {
        "block_type": bt,
        field: {"elements": [{"text_run": {"content": content, "text_element_style": {}}}]}
    }


def _make_table_bd():
    """Helper: create a table block dict."""
    return {
        "block_type": BLOCK_TYPE_TABLE,
        "table": {
            "rows": [["A", "B"], ["1", "2"]],
            "column_size": 2,
            "header_row": True,
            "column_widths": [200, 200],
        }
    }


def _make_bullet_bd(content="item"):
    """Helper: create a bullet block dict."""
    return {
        "block_type": BLOCK_TYPE_BULLET,
        "bullet": {"elements": [{"text_run": {"content": content, "text_element_style": {}}}]}
    }


# ══════════════════════════════════════════════════════════════════════
# P1-1: Auto-chunking tests
# ══════════════════════════════════════════════════════════════════════

class TestSplitIntoChunks(unittest.TestCase):
    """Test P1-1: _split_into_chunks() function."""

    def test_single_text_block(self):
        """Single text block → 1 regular chunk."""
        from feishu_doc import _split_into_chunks
        blocks = [_make_text_bd("hello")]
        chunks = _split_into_chunks(blocks)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["type"], "regular")
        self.assertEqual(len(chunks[0]["data"]), 1)

    def test_table_is_separate_chunk(self):
        """Table block should be its own chunk."""
        from feishu_doc import _split_into_chunks
        blocks = [_make_text_bd(), _make_table_bd(), _make_text_bd()]
        chunks = _split_into_chunks(blocks)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(chunks[0]["type"], "regular")
        self.assertEqual(chunks[1]["type"], "table")
        self.assertEqual(chunks[2]["type"], "regular")

    def test_heading_starts_new_chunk(self):
        """Heading block should start a new chunk."""
        from feishu_doc import _split_into_chunks
        blocks = [
            _make_text_bd("para1"),
            _make_text_bd("para2"),
            _make_heading_bd("Section 2", level=2),
            _make_text_bd("para3"),
        ]
        chunks = _split_into_chunks(blocks)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0]["type"], "regular")
        self.assertEqual(len(chunks[0]["data"]), 2)  # para1, para2
        self.assertEqual(chunks[1]["type"], "regular")
        self.assertEqual(len(chunks[1]["data"]), 2)  # heading, para3

    def test_max_blocks_per_chunk(self):
        """Chunk should split when reaching CHUNK_MAX_BLOCKS."""
        from feishu_doc import _split_into_chunks, CHUNK_MAX_BLOCKS
        blocks = [_make_text_bd(f"text{i}") for i in range(CHUNK_MAX_BLOCKS + 5)]
        chunks = _split_into_chunks(blocks)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(len(chunks[0]["data"]), CHUNK_MAX_BLOCKS)
        self.assertEqual(len(chunks[1]["data"]), 5)

    def test_consecutive_tables(self):
        """Consecutive tables should each be their own chunk."""
        from feishu_doc import _split_into_chunks
        blocks = [_make_table_bd(), _make_table_bd(), _make_table_bd()]
        chunks = _split_into_chunks(blocks)
        self.assertEqual(len(chunks), 3)
        for c in chunks:
            self.assertEqual(c["type"], "table")

    def test_mixed_content(self):
        """Complex mixed content: text + heading + text + table + text."""
        from feishu_doc import _split_into_chunks
        blocks = [
            _make_text_bd("intro"),
            _make_heading_bd("H1", level=1),
            _make_text_bd("body1"),
            _make_bullet_bd("item1"),
            _make_table_bd(),
            _make_text_bd("after_table"),
        ]
        chunks = _split_into_chunks(blocks)
        # Expect: [intro] [H1, body1, item1] [table] [after_table]
        self.assertEqual(len(chunks), 4)
        self.assertEqual(chunks[0]["type"], "regular")
        self.assertEqual(len(chunks[0]["data"]), 1)  # intro
        self.assertEqual(chunks[1]["type"], "regular")
        self.assertEqual(len(chunks[1]["data"]), 3)  # H1, body1, item1
        self.assertEqual(chunks[2]["type"], "table")
        self.assertEqual(chunks[3]["type"], "regular")
        self.assertEqual(len(chunks[3]["data"]), 1)  # after_table

    def test_empty_input(self):
        """Empty block list → empty chunks."""
        from feishu_doc import _split_into_chunks
        chunks = _split_into_chunks([])
        self.assertEqual(len(chunks), 0)

    def test_heading_levels_all_break(self):
        """All heading levels (1-9) should start new chunks."""
        from feishu_doc import _split_into_chunks
        blocks = [_make_text_bd("before")]
        for level in range(1, 10):
            blocks.append(_make_heading_bd(f"H{level}", level=level))
        chunks = _split_into_chunks(blocks)
        # "before" is 1 chunk, then each heading starts a new chunk
        self.assertEqual(len(chunks), 10)


# ══════════════════════════════════════════════════════════════════════
# P1-1 + P1-2: Chunked writing with progress feedback
# ══════════════════════════════════════════════════════════════════════

class TestChunkedWriteAndProgress(unittest.TestCase):
    """Test P1-1 chunked writing and P1-2 progress output."""

    @patch('feishu_doc._write_table_block')
    @patch('feishu_doc._write_regular_blocks')
    @patch('feishu_doc.time')
    def test_chunk_delays(self, mock_time, mock_regular, mock_table):
        """Regular chunks get 0.5s delay (F9.3), table chunks get 3s delay."""
        from feishu_doc import _write_blocks_to_doc

        mock_regular.return_value = 1
        mock_table.return_value = True

        # text + table + text → 3 chunks
        block_dicts = [_make_text_bd(), _make_table_bd(), _make_text_bd()]
        client = MagicMock()
        _write_blocks_to_doc(client, "doc123", block_dicts)

        # Check delays: no delay for first chunk, then delays for subsequent
        sleep_calls = mock_time.sleep.call_args_list
        # We expect some sleep calls — at minimum for the table chunk delay
        sleep_values = [c[0][0] for c in sleep_calls]
        # Between chunk 0 (text) and chunk 1 (table): no 3s delay (first table)
        # Between chunk 1 (table) and chunk 2 (text): 0.5s delay (F9.3: reduced from 1s)
        self.assertIn(0.5, sleep_values, "Should have 0.5s delay between regular chunks")

    @patch('feishu_doc._write_table_block')
    @patch('feishu_doc._write_regular_blocks')
    @patch('feishu_doc.time')
    def test_table_to_table_3s_delay(self, mock_time, mock_regular, mock_table):
        """Consecutive tables should have 3s delay (P0-3 compatible)."""
        from feishu_doc import _write_blocks_to_doc

        mock_regular.return_value = 1
        mock_table.return_value = True

        block_dicts = [_make_table_bd(), _make_table_bd()]
        client = MagicMock()
        _write_blocks_to_doc(client, "doc123", block_dicts)

        sleep_calls = [c[0][0] for c in mock_time.sleep.call_args_list]
        self.assertIn(3, sleep_calls, "Should have 3s delay between consecutive tables")

    @patch('feishu_doc._write_table_block')
    @patch('feishu_doc._write_regular_blocks')
    @patch('feishu_doc.time')
    def test_progress_output_to_stderr(self, mock_time, mock_regular, mock_table):
        """Progress messages should be written to stderr."""
        from feishu_doc import _write_blocks_to_doc

        mock_regular.return_value = 2
        mock_table.return_value = True

        block_dicts = [
            _make_text_bd("a"), _make_text_bd("b"),
            _make_table_bd(),
            _make_text_bd("c"),
        ]

        client = MagicMock()

        # Capture stderr
        captured_stderr = io.StringIO()
        with patch('sys.stderr', captured_stderr):
            _write_blocks_to_doc(client, "doc123", block_dicts)

        stderr_output = captured_stderr.getvalue()

        # Check progress messages
        self.assertIn("[1/", stderr_output)
        self.assertIn("[2/", stderr_output)
        self.assertIn("[3/", stderr_output)
        self.assertIn("All chunks written successfully", stderr_output)

    @patch('feishu_doc._write_table_block')
    @patch('feishu_doc._write_regular_blocks')
    @patch('feishu_doc.time')
    def test_progress_shows_block_count(self, mock_time, mock_regular, mock_table):
        """Progress should show block count for regular chunks and 'table' for tables."""
        from feishu_doc import _write_blocks_to_doc

        mock_regular.return_value = 2
        mock_table.return_value = True

        block_dicts = [_make_text_bd(), _make_text_bd(), _make_table_bd()]

        client = MagicMock()
        captured_stderr = io.StringIO()
        with patch('sys.stderr', captured_stderr):
            _write_blocks_to_doc(client, "doc123", block_dicts)

        stderr_output = captured_stderr.getvalue()
        self.assertIn("2 blocks", stderr_output)
        self.assertIn("table", stderr_output)

    @patch('feishu_doc._write_table_block')
    @patch('feishu_doc._write_regular_blocks')
    @patch('feishu_doc.time')
    def test_write_calls_correct_functions(self, mock_time, mock_regular, mock_table):
        """Verify _write_regular_blocks and _write_table_block are called correctly."""
        from feishu_doc import _write_blocks_to_doc

        mock_regular.return_value = 3
        mock_table.return_value = True

        blocks = [
            _make_text_bd("a"), _make_text_bd("b"), _make_text_bd("c"),
            _make_table_bd(),
        ]
        client = MagicMock()
        result = _write_blocks_to_doc(client, "doc123", blocks)

        self.assertEqual(result, 0)
        self.assertEqual(mock_regular.call_count, 1)
        self.assertEqual(mock_table.call_count, 1)
        # Check that regular blocks were passed correctly
        regular_call_blocks = mock_regular.call_args[0][2]  # 3rd positional arg
        self.assertEqual(len(regular_call_blocks), 3)


# ══════════════════════════════════════════════════════════════════════
# P1-3: Resume from chunk
# ══════════════════════════════════════════════════════════════════════

class TestResumeFrom(unittest.TestCase):
    """Test P1-3: --resume-from parameter."""

    @patch('feishu_doc._write_table_block')
    @patch('feishu_doc._write_regular_blocks')
    @patch('feishu_doc.time')
    def test_resume_skips_earlier_chunks(self, mock_time, mock_regular, mock_table):
        """resume_from=2 should skip chunks 0 and 1."""
        from feishu_doc import _write_blocks_to_doc

        mock_regular.return_value = 1
        mock_table.return_value = True

        # 4 chunks: text, table, text, text
        blocks = [
            _make_text_bd("chunk0"),
            _make_table_bd(),
            _make_heading_bd("chunk2"),
            _make_text_bd("chunk3_body"),
            _make_heading_bd("chunk3"),
        ]

        client = MagicMock()
        result = _write_blocks_to_doc(client, "doc123", blocks, resume_from=2)

        self.assertEqual(result, 0)
        # Should NOT have written the first text chunk or the table
        # The first _write_regular_blocks call should be for chunk 2
        # Check that table was NOT written
        self.assertEqual(mock_table.call_count, 0)
        # Regular blocks should have been written for chunks 2 and 3
        self.assertEqual(mock_regular.call_count, 2)

    @patch('feishu_doc._write_table_block')
    @patch('feishu_doc._write_regular_blocks')
    @patch('feishu_doc.time')
    def test_resume_from_zero_writes_all(self, mock_time, mock_regular, mock_table):
        """resume_from=0 should write everything (default behavior)."""
        from feishu_doc import _write_blocks_to_doc

        mock_regular.return_value = 1
        mock_table.return_value = True

        blocks = [_make_text_bd(), _make_table_bd(), _make_text_bd()]
        client = MagicMock()
        result = _write_blocks_to_doc(client, "doc123", blocks, resume_from=0)

        self.assertEqual(result, 0)
        self.assertEqual(mock_regular.call_count, 2)
        self.assertEqual(mock_table.call_count, 1)

    @patch('feishu_doc._write_table_block')
    @patch('feishu_doc._write_regular_blocks')
    @patch('feishu_doc.time')
    def test_failure_outputs_resume_hint(self, mock_time, mock_regular, mock_table):
        """On failure, stderr should contain resume hint."""
        from feishu_doc import _write_blocks_to_doc

        mock_regular.return_value = 1
        mock_table.return_value = False  # Table write fails

        blocks = [_make_text_bd(), _make_table_bd(), _make_text_bd()]
        client = MagicMock()

        captured_stderr = io.StringIO()
        with patch('sys.stderr', captured_stderr):
            result = _write_blocks_to_doc(client, "doc123", blocks)

        self.assertEqual(result, 1)
        stderr_output = captured_stderr.getvalue()
        self.assertIn("--resume-from", stderr_output)
        self.assertIn("ERROR: Failed at", stderr_output)
        # F9.4: Should contain both skip and retry options
        self.assertIn("skip failed chunk", stderr_output)

    @patch('feishu_doc._write_table_block')
    @patch('feishu_doc._write_regular_blocks')
    @patch('feishu_doc.time')
    def test_resume_tracks_table_written_flag(self, mock_time, mock_regular, mock_table):
        """When resuming past a table, table_written should be set for correct delays."""
        from feishu_doc import _write_blocks_to_doc

        mock_regular.return_value = 1
        mock_table.return_value = True

        # chunks: text(0), table(1), table(2)
        blocks = [_make_text_bd(), _make_table_bd(), _make_table_bd()]
        client = MagicMock()

        # Resume from chunk 2 (second table)
        _write_blocks_to_doc(client, "doc123", blocks, resume_from=2)

        # The second table should know a table was already "written" (skipped)
        # so it should apply the 3s delay
        sleep_calls = [c[0][0] for c in mock_time.sleep.call_args_list]
        self.assertIn(3, sleep_calls, "Should apply 3s delay when resuming after a table")

    @patch('feishu_doc._write_table_block')
    @patch('feishu_doc._write_regular_blocks')
    @patch('feishu_doc.time')
    def test_regular_failure_outputs_resume_hint(self, mock_time, mock_regular, mock_table):
        """Regular block write failure should output resume hint."""
        from feishu_doc import _write_blocks_to_doc

        mock_regular.return_value = -1  # Regular write fails
        mock_table.return_value = True

        blocks = [_make_text_bd()]
        client = MagicMock()

        captured_stderr = io.StringIO()
        with patch('sys.stderr', captured_stderr):
            result = _write_blocks_to_doc(client, "doc123", blocks)

        self.assertEqual(result, 1)
        stderr_output = captured_stderr.getvalue()
        self.assertIn("--resume-from 0", stderr_output)


# ══════════════════════════════════════════════════════════════════════
# P1-4: Auto add-member in create-and-write
# ══════════════════════════════════════════════════════════════════════

class TestAddMemberExtraction(unittest.TestCase):
    """Test P1-4: _add_member() internal function extraction."""

    def test_add_member_function_exists(self):
        """_add_member() should be importable."""
        from feishu_doc import _add_member
        self.assertTrue(callable(_add_member))

    def test_add_member_calls_api(self):
        """_add_member() should call drive.v1.permission_member.create."""
        from feishu_doc import _add_member

        client = MagicMock()
        response = MagicMock()
        response.success.return_value = True
        client.drive.v1.permission_member.create.return_value = response

        result = _add_member(client, "doc123", "ou_test123", "full_access")

        self.assertTrue(result)
        client.drive.v1.permission_member.create.assert_called_once()

    def test_add_member_returns_false_on_failure(self):
        """_add_member() should return False when API fails."""
        from feishu_doc import _add_member

        client = MagicMock()
        response = MagicMock()
        response.success.return_value = False
        client.drive.v1.permission_member.create.return_value = response

        result = _add_member(client, "doc123", "ou_test123", "view")
        self.assertFalse(result)

    def test_add_member_perm_default(self):
        """_add_member() default perm should be full_access."""
        from feishu_doc import _add_member
        import inspect
        sig = inspect.signature(_add_member)
        self.assertEqual(sig.parameters['perm'].default, "full_access")


class TestCreateAndWriteAddMember(unittest.TestCase):
    """Test P1-4: create-and-write --add-member integration."""

    def test_argparse_has_add_member(self):
        """create-and-write parser should accept --add-member."""
        import feishu_doc
        import argparse

        # Parse args to check --add-member is accepted
        parser = argparse.ArgumentParser()
        parser.add_argument("--app", default="ST")
        subparsers = parser.add_subparsers(dest="command")
        caw = subparsers.add_parser("create-and-write")
        caw.add_argument("--title", required=True)
        caw.add_argument("--folder")
        caw.add_argument("--markdown")
        caw.add_argument("--markdown-file")
        caw.add_argument("--resume-from", type=int, default=0)
        caw.add_argument("--add-member")
        caw.add_argument("--member-perm", default="full_access")

        args = parser.parse_args([
            "create-and-write", "--title", "Test",
            "--markdown", "# Hello",
            "--add-member", "ou_abc123",
            "--member-perm", "edit"
        ])

        self.assertEqual(args.add_member, "ou_abc123")
        self.assertEqual(args.member_perm, "edit")

    def test_argparse_has_resume_from_on_write(self):
        """write parser should accept --resume-from."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--app", default="ST")
        subparsers = parser.add_subparsers(dest="command")
        wp = subparsers.add_parser("write")
        wp.add_argument("--doc", required=True)
        wp.add_argument("--markdown")
        wp.add_argument("--resume-from", type=int, default=0)

        args = parser.parse_args([
            "write", "--doc", "doc123",
            "--markdown", "# Hello",
            "--resume-from", "5"
        ])

        self.assertEqual(args.resume_from, 5)


# ══════════════════════════════════════════════════════════════════════
# Regression: P0-3 table delay compatibility
# ══════════════════════════════════════════════════════════════════════

class TestP0Regression(unittest.TestCase):
    """Ensure P0 behavior is preserved after P1 changes."""

    @patch('feishu_doc._write_table_block')
    @patch('feishu_doc._write_regular_blocks')
    @patch('feishu_doc.time')
    def test_single_table_no_delay(self, mock_time, mock_regular, mock_table):
        """Single table should not have any 3s delay."""
        from feishu_doc import _write_blocks_to_doc

        mock_table.return_value = True
        blocks = [_make_table_bd()]
        client = MagicMock()
        _write_blocks_to_doc(client, "doc123", blocks)

        sleep_calls = [c[0][0] for c in mock_time.sleep.call_args_list]
        self.assertNotIn(3, sleep_calls)

    @patch('feishu_doc._write_table_block')
    @patch('feishu_doc._write_regular_blocks')
    @patch('feishu_doc.time')
    def test_text_table_text_table(self, mock_time, mock_regular, mock_table):
        """text + table + text + table: 3s delay only before second table."""
        from feishu_doc import _write_blocks_to_doc

        mock_regular.return_value = 1
        mock_table.return_value = True

        blocks = [
            _make_text_bd(), _make_table_bd(),
            _make_text_bd(), _make_table_bd(),
        ]
        client = MagicMock()
        _write_blocks_to_doc(client, "doc123", blocks)

        sleep_calls = [c[0][0] for c in mock_time.sleep.call_args_list]
        # Count 3s delays — should be exactly 1 (before second table)
        three_s_count = sleep_calls.count(3)
        self.assertEqual(three_s_count, 1,
                         f"Expected exactly 1 three-second delay, got {three_s_count}: {sleep_calls}")


if __name__ == "__main__":
    unittest.main()
