#!/usr/bin/env python3
"""飞书文档操作 CLI — nanobot feishu-docs skill

统一入口脚本，提供飞书文档的创建、读取、写入功能。
从 ~/.nanobot/config.json 自动加载飞书应用凭证（ST 应用）。

用法:
  python3 feishu_doc.py create --title "标题" [--folder TOKEN]
  python3 feishu_doc.py write --doc DOC_ID --markdown "内容" [--markdown-file FILE]
  python3 feishu_doc.py read --doc DOC_ID [--format raw|blocks]
  python3 feishu_doc.py create-and-write --title "标题" --markdown "内容" [--markdown-file FILE] [--folder TOKEN]

安全说明:
  - appSecret 仅在此脚本进程内使用，不输出到 stdout
  - Agent 不直接接触密钥
"""

import argparse
import json
import os
import sys
import time
from typing import Optional, Tuple

# Add current dir to path for md_to_blocks import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from md_to_blocks import markdown_to_blocks, BLOCK_TYPE_TABLE, BLOCK_TYPE_TEXT

try:
    import lark_oapi as lark
    from lark_oapi.api.docx.v1 import *
    from lark_oapi.api.drive.v1 import (
        ListFileCommentRequest, PatchFileCommentRequest,
        CreateFileCommentRequest, CreatePermissionMemberRequest,
        FileComment as DriveFileComment, BaseMember,
        ReplyContent, ReplyElement, TextRun as DriveTextRun,
        FileCommentReply, ReplyList,
    )
except ImportError:
    print("ERROR: lark-oapi not installed. Run: pip install lark-oapi", file=sys.stderr)
    sys.exit(1)

# For reply-comment we need requests (SDK lacks CreateFileCommentReply)
try:
    import requests as http_requests
except ImportError:
    http_requests = None


# ── Timeout & retry constants ─────────────────────────────────────────

SDK_TIMEOUT = 30   # SDK 请求超时（秒）
HTTP_TIMEOUT = 30  # HTTP 请求超时（秒）
MAX_RETRIES = 3    # 默认最大重试次数


# ── Connection pool (requests.Session) ────────────────────────────────

_http_session = None


def _get_http_session():
    """Get or create a reusable HTTP session with connection pooling.

    Benefits: TCP connection reuse, keep-alive, reduced handshake overhead.
    """
    global _http_session
    if _http_session is None:
        _http_session = http_requests.Session()
        adapter = http_requests.adapters.HTTPAdapter(
            max_retries=0,          # We manage retries ourselves
            pool_connections=5,
            pool_maxsize=10,
        )
        _http_session.mount('https://', adapter)
        _http_session.mount('http://', adapter)
    return _http_session


# ── Retry wrappers ────────────────────────────────────────────────────

def _http_request_with_retry(method, url, max_retries=MAX_RETRIES, desc="", **kwargs):
    """Execute an HTTP request with retry on timeout/transient errors.

    Uses the global connection-pooled session. Retries on:
    - Timeout exceptions
    - Connection errors
    - HTTP 429 (rate limit)
    - HTTP 5xx (server errors)
    - Feishu API code 99991400 (rate limit)

    Does NOT retry on 4xx (except 429) — those are client errors.

    Args:
        method: HTTP method string ("post", "patch", "delete", "get")
        url: Request URL
        max_retries: Maximum number of attempts (default 3)
        desc: Description for log messages
        **kwargs: Passed to requests (headers, json, timeout, etc.)

    Returns:
        requests.Response

    Raises:
        requests.exceptions.Timeout: After all retries exhausted
        requests.exceptions.ConnectionError: After all retries exhausted
    """
    kwargs.setdefault("timeout", HTTP_TIMEOUT)
    session = _get_http_session()
    func = getattr(session, method)
    label = f" ({desc})" if desc else ""

    last_resp = None
    for attempt in range(max_retries):
        try:
            resp = func(url, **kwargs)
            last_resp = resp

            # HTTP 429 — rate limited, retry
            if resp.status_code == 429:
                if attempt < max_retries - 1:
                    wait = 1 * (2 ** attempt)  # 1s, 2s, 4s
                    print(f"HTTP 429{label}, retry {attempt+1}/{max_retries} "
                          f"after {wait}s", file=sys.stderr)
                    time.sleep(wait)
                    continue
                return resp

            # 5xx — server error, retry
            if resp.status_code >= 500:
                if attempt < max_retries - 1:
                    wait = 1 * (2 ** attempt)
                    print(f"HTTP {resp.status_code}{label}, retry {attempt+1}/{max_retries} "
                          f"after {wait}s", file=sys.stderr)
                    time.sleep(wait)
                    continue
                return resp

            # Check for Feishu rate limit in response body (code 99991400)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    if data.get("code") == 99991400:
                        if attempt < max_retries - 1:
                            wait = 1 * (2 ** attempt)
                            print(f"Rate limited (99991400){label}, retry {attempt+1}/{max_retries} "
                                  f"after {wait}s", file=sys.stderr)
                            time.sleep(wait)
                            continue
                except (ValueError, KeyError):
                    pass  # Not JSON or no code field — return as-is

            # Success or non-retryable error (4xx)
            return resp

        except http_requests.exceptions.Timeout as e:
            if attempt < max_retries - 1:
                wait = 1 * (2 ** attempt)
                print(f"Timeout{label}, retry {attempt+1}/{max_retries} "
                      f"after {wait}s", file=sys.stderr)
                time.sleep(wait)
            else:
                raise

        except http_requests.exceptions.ConnectionError as e:
            if attempt < max_retries - 1:
                wait = 1 * (2 ** attempt)
                print(f"Connection error{label}, retry {attempt+1}/{max_retries} "
                      f"after {wait}s", file=sys.stderr)
                time.sleep(wait)
            else:
                raise

    return last_resp  # Should not reach here normally


def _sdk_call_with_retry(call_func, max_retries=MAX_RETRIES, desc=""):
    """Execute an SDK call with retry on timeout/transient errors.

    Retries on:
    - requests.exceptions.Timeout
    - requests.exceptions.ConnectionError
    - Feishu API code 99991400 (rate limit)

    Does NOT retry on other API errors (4xx-level business errors).

    Args:
        call_func: Callable that returns an SDK response
                   (e.g., lambda: client.docx.v1.document.create(request))
        max_retries: Maximum number of attempts (default 3)
        desc: Description for log messages

    Returns:
        SDK response object

    Raises:
        requests.exceptions.Timeout: After all retries exhausted
        requests.exceptions.ConnectionError: After all retries exhausted
    """
    label = f" ({desc})" if desc else ""
    last_response = None
    last_exception = None

    for attempt in range(max_retries):
        try:
            response = call_func()
            last_response = response

            if response.success():
                return response

            # Rate limit → retry
            if response.code == 99991400:
                if attempt < max_retries - 1:
                    wait = 2 * (2 ** attempt)  # 2s, 4s, 8s
                    print(f"SDK rate limited{label}, retry {attempt+1}/{max_retries} "
                          f"after {wait}s", file=sys.stderr)
                    time.sleep(wait)
                    continue
                return response

            # Non-retryable API error → return immediately
            return response

        except (http_requests.exceptions.Timeout,
                http_requests.exceptions.ConnectionError) as e:
            last_exception = e
            if attempt < max_retries - 1:
                wait = 1 * (2 ** attempt)
                etype = "Timeout" if isinstance(e, http_requests.exceptions.Timeout) else "Connection error"
                print(f"SDK {etype}{label}, retry {attempt+1}/{max_retries} "
                      f"after {wait}s", file=sys.stderr)
                time.sleep(wait)
            else:
                raise

    # All retries exhausted (rate limit case)
    if last_response:
        return last_response
    if last_exception:
        raise last_exception


# ── Config loading ────────────────────────────────────────────────────

def load_feishu_credentials(app_name: str = "ST") -> Tuple[str, str]:
    """Load Feishu app credentials from nanobot config.

    Args:
        app_name: Name of the Feishu app in config (default: "ST")

    Returns:
        Tuple of (app_id, app_secret)

    Raises:
        SystemExit: If config not found or app not configured
    """
    config_path = os.path.expanduser("~/.nanobot/config.json")
    if not os.path.exists(config_path):
        print(f"ERROR: Config not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    with open(config_path, 'r') as f:
        config = json.load(f)

    feishu_apps = config.get("channels", {}).get("feishu", [])
    if not isinstance(feishu_apps, list):
        print("ERROR: channels.feishu should be an array in config.json", file=sys.stderr)
        sys.exit(1)

    target_app = None
    for app in feishu_apps:
        if app.get("name") == app_name:
            target_app = app
            break

    if not target_app:
        available = [a.get("name", "?") for a in feishu_apps]
        print(f"ERROR: Feishu app '{app_name}' not found. Available: {available}", file=sys.stderr)
        sys.exit(1)

    app_id = target_app.get("appId", "")
    app_secret = target_app.get("appSecret", "")
    if not app_id or not app_secret:
        print(f"ERROR: appId or appSecret not configured for '{app_name}'", file=sys.stderr)
        sys.exit(1)

    return app_id, app_secret


def create_client(app_name: str = "ST") -> lark.Client:
    """Create a Feishu API client.

    Args:
        app_name: Name of the Feishu app

    Returns:
        lark.Client instance
    """
    app_id, app_secret = load_feishu_credentials(app_name)
    client = lark.Client.builder() \
        .app_id(app_id) \
        .app_secret(app_secret) \
        .log_level(lark.LogLevel.WARNING) \
        .timeout(SDK_TIMEOUT) \
        .build()
    return client


# ── Document operations ───────────────────────────────────────────────

def cmd_create(args):
    """Create a new Feishu document."""
    client = create_client(args.app)

    # Build request
    body_builder = CreateDocumentRequestBody.builder() \
        .title(args.title)

    if args.folder:
        body_builder = body_builder.folder_token(args.folder)

    request = CreateDocumentRequest.builder() \
        .request_body(body_builder.build()) \
        .build()

    # Call API (with retry)
    response = _sdk_call_with_retry(
        lambda: client.docx.v1.document.create(request),
        desc="create document"
    )

    if not response.success():
        print(json.dumps({
            "success": False,
            "error": f"[{response.code}] {response.msg}"
        }, ensure_ascii=False))
        return 1

    doc = response.data.document
    result = {
        "success": True,
        "document_id": doc.document_id,
        "revision_id": doc.revision_id,
        "title": doc.title,
        "url": f"https://feishu.cn/docx/{doc.document_id}"
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_write(args):
    """Write content to an existing Feishu document."""
    client = create_client(args.app)

    # Get markdown content
    markdown = _get_markdown_content(args)
    if markdown is None:
        return 1

    # If overwrite mode, clear existing content first
    if hasattr(args, 'mode') and args.mode == 'overwrite':
        clear_result = _clear_document(client, args.doc)
        if clear_result != 0:
            return clear_result

    # Convert markdown to blocks
    block_dicts = markdown_to_blocks(markdown)
    if not block_dicts:
        print(json.dumps({"success": False, "error": "No content to write"}), ensure_ascii=False)
        return 1

    # Separate table blocks from regular blocks (tables need special handling)
    resume_from = getattr(args, 'resume_from', 0) or 0
    return _write_blocks_to_doc(client, args.doc, block_dicts, resume_from=resume_from)


def _clear_document(client, doc_id: str) -> int:
    """Clear all child blocks from a document (for overwrite mode).

    Returns 0 on success, 1 on failure.
    """
    # First, get the document's child blocks to know how many to delete
    request = ListDocumentBlockRequest.builder() \
        .document_id(doc_id) \
        .page_size(500) \
        .build()

    response = _sdk_call_with_retry(
        lambda: client.docx.v1.document_block.list(request),
        desc="list blocks for clear"
    )

    if not response.success():
        print(json.dumps({
            "success": False,
            "error": f"Failed to list blocks for clear: [{response.code}] {response.msg}"
        }, ensure_ascii=False))
        return 1

    if not response.data or not response.data.items:
        return 0  # Document already empty

    # Find the page block (block_type=1) — its children are the top-level blocks
    page_block = None
    for block in response.data.items:
        if block.block_type == 1:
            page_block = block
            break

    if not page_block or not page_block.children:
        return 0  # No children to delete

    child_count = len(page_block.children)
    if child_count == 0:
        return 0

    # Delete all children in batches (API may have limits)
    # BatchDelete uses start_index and end_index (exclusive)
    BATCH_SIZE = 50
    # Delete from the end to avoid index shifting issues
    remaining = child_count
    while remaining > 0:
        delete_count = min(BATCH_SIZE, remaining)
        del_request = BatchDeleteDocumentBlockChildrenRequest.builder() \
            .document_id(doc_id) \
            .block_id(doc_id) \
            .request_body(
                BatchDeleteDocumentBlockChildrenRequestBody.builder()
                .start_index(0)
                .end_index(delete_count)
                .build()
            ) \
            .build()

        del_response = _sdk_call_with_retry(
            lambda req=del_request: client.docx.v1.document_block_children.batch_delete(req),
            desc="batch_delete blocks for clear"
        )

        if not del_response.success():
            print(json.dumps({
                "success": False,
                "error": f"Failed to clear document: [{del_response.code}] {del_response.msg}"
            }, ensure_ascii=False))
            return 1

        remaining -= delete_count

    return 0


def _write_blocks_to_doc(client, doc_id: str, block_dicts: list,
                         resume_from: int = 0) -> int:
    """Write block dicts to a document, handling both regular blocks and tables.

    Automatically splits block_dicts into chunks at safe boundaries (before tables,
    before headings) and writes them with appropriate delays to avoid rate limiting.

    Tables need special two-step creation:
    1. Create empty table block with row_size/column_size
    2. Fill each cell with content via descendant API

    Args:
        client: Feishu API client
        doc_id: Document ID
        block_dicts: List of block dicts from markdown_to_blocks
        resume_from: Chunk index to resume from (0-based, default 0 = start from beginning)

    Returns exit code (0=success, 1=failure).
    """
    # Split into chunks at safe boundaries
    chunks = _split_into_chunks(block_dicts)
    total_chunks = len(chunks)

    if total_chunks == 0:
        print(json.dumps({"success": False, "error": "No content to write"}), ensure_ascii=False)
        return 1

    total_written = 0
    table_written = False  # Track if we've already written a table (for delay logic)

    for chunk_idx, chunk in enumerate(chunks):
        # P1-3: Skip chunks before resume_from
        if chunk_idx < resume_from:
            # Still track table_written for correct delay logic
            if chunk["type"] == "table":
                table_written = True
            continue

        chunk_type = chunk["type"]
        chunk_data = chunk["data"]

        # P1-2: Progress feedback to stderr
        if chunk_type == "table":
            desc = "table"
        else:
            desc = f"{len(chunk_data)} blocks"
        print(f"[{chunk_idx + 1}/{total_chunks}] Writing chunk {chunk_idx + 1} ({desc})...",
              file=sys.stderr)

        # Apply delays between chunks
        if chunk_idx > 0 and chunk_idx >= resume_from:
            if chunk_type == "table":
                # P0-3: Table delay — 3s if a table was already written
                if table_written:
                    time.sleep(3)
            else:
                # F9.3: Reduced from 1s to 0.5s to speed up large document writes
                if total_written > 0 or table_written:
                    time.sleep(0.5)

        # Write the chunk
        if chunk_type == "regular":
            count = _write_regular_blocks(client, doc_id, chunk_data)
            if count < 0:
                # F9.4: Improved resume hint — warn about potential duplication
                # from partial writes. Suggest checking the document.
                print(f"ERROR: Failed at chunk {chunk_idx + 1}/{total_chunks}. "
                      f"Blocks written before failure: {total_written}. "
                      f"Resume with: --resume-from {chunk_idx + 1} "
                      f"(skip failed chunk) or --resume-from {chunk_idx} "
                      f"(retry failed chunk — check doc for duplicates first)",
                      file=sys.stderr)
                return 1
            total_written += count
        elif chunk_type == "table":
            ok = _write_table_block(client, doc_id, chunk_data)
            if not ok:
                # F9.4: Improved resume hint
                print(f"ERROR: Failed at table chunk {chunk_idx + 1}/{total_chunks}. "
                      f"Blocks written before failure: {total_written}. "
                      f"Resume with: --resume-from {chunk_idx + 1} "
                      f"(skip failed chunk) or --resume-from {chunk_idx} "
                      f"(retry failed chunk — check doc for duplicates first)",
                      file=sys.stderr)
                return 1
            total_written += 1
            table_written = True

    # P1-2: Final progress message
    print(f"[{total_chunks}/{total_chunks}] All chunks written successfully.",
          file=sys.stderr)

    result = {
        "success": True,
        "document_id": doc_id,
        "blocks_written": total_written,
        "total_chunks": total_chunks,
        "url": f"https://feishu.cn/docx/{doc_id}"
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


# ── Chunk size constants ──────────────────────────────────────────────

# F9.3: Increased from 30 to 50 to reduce chunk count and inter-chunk delays
# for large documents. Matches the BATCH_SIZE in _write_regular_blocks.
CHUNK_MAX_BLOCKS = 50   # Max regular blocks per chunk
CHUNK_DELAY = 1         # Seconds between regular chunks
TABLE_DELAY = 3         # Seconds between table chunks (P0-3 compatible)

# Heading block types that serve as safe chunk boundaries
_HEADING_TYPES = {3, 4, 5, 6, 7, 8, 9, 10, 11}  # heading1..heading9


def _split_into_chunks(block_dicts: list) -> list:
    """Split block_dicts into chunks at safe boundaries.

    Safe boundaries:
    - Before/after each table block (each table is its own chunk)
    - Before heading blocks
    - When a regular chunk reaches CHUNK_MAX_BLOCKS

    Returns a list of chunk dicts:
        [{"type": "regular", "data": [block_dict, ...]},
         {"type": "table", "data": table_block_dict},
         ...]
    """
    chunks = []
    current_regular = []

    def _flush_regular():
        nonlocal current_regular
        if current_regular:
            chunks.append({"type": "regular", "data": current_regular})
            current_regular = []

    for bd in block_dicts:
        bt = bd.get("block_type")

        if bt == BLOCK_TYPE_TABLE:
            # Table is always its own chunk
            _flush_regular()
            chunks.append({"type": "table", "data": bd})

        elif bt in _HEADING_TYPES:
            # Heading starts a new chunk (safe boundary)
            _flush_regular()
            current_regular.append(bd)

        else:
            # Regular block — check if current chunk is full
            if len(current_regular) >= CHUNK_MAX_BLOCKS:
                _flush_regular()
            current_regular.append(bd)

    _flush_regular()

    return chunks


def _write_regular_blocks(client, doc_id: str, block_dicts: list) -> int:
    """Write regular (non-table) blocks to document. Returns count written or -1 on error.

    Supports nested blocks: if any block dict has a "children" key, the entire
    batch is written using the descendant API which supports parent-child block
    nesting for list indentation. Otherwise uses the simpler children API.
    """
    # Check if any blocks have nested children
    has_nested = any(_has_nested_children(bd) for bd in block_dicts)

    if has_nested:
        return _write_nested_blocks(client, doc_id, block_dicts)
    else:
        return _write_flat_blocks(client, doc_id, block_dicts)


def _has_nested_children(bd: dict) -> bool:
    """Check if a block dict or any of its descendants has nested children."""
    if "children" in bd and bd["children"]:
        return True
    return False


def _write_flat_blocks(client, doc_id: str, block_dicts: list) -> int:
    """Write flat (non-nested) blocks using the children API."""
    children = []
    for bd in block_dicts:
        block = _dict_to_block_simple(bd)
        children.append(block)

    BATCH_SIZE = 50
    total_written = 0

    for batch_start in range(0, len(children), BATCH_SIZE):
        batch = children[batch_start:batch_start + BATCH_SIZE]

        request = CreateDocumentBlockChildrenRequest.builder() \
            .document_id(doc_id) \
            .block_id(doc_id) \
            .request_body(
                CreateDocumentBlockChildrenRequestBody.builder()
                .children(batch)
                .index(-1)
                .build()
            ) \
            .build()

        response = _sdk_call_with_retry(
            lambda req=request: client.docx.v1.document_block_children.create(req),
            desc="write flat blocks"
        )

        if not response.success():
            print(json.dumps({
                "success": False,
                "error": f"[{response.code}] {response.msg}",
                "blocks_written": total_written
            }, ensure_ascii=False))
            return -1

        total_written += len(batch)

    return total_written


def _write_nested_blocks(client, doc_id: str, block_dicts: list) -> int:
    """Write blocks with nested children using the descendant API.

    The descendant API (POST .../blocks/:block_id/descendant) accepts:
    - children_id: list of block IDs that are direct children of the parent
    - descendants: flat list of ALL blocks with custom block_ids
    - Each block's children field contains IDs of its child blocks

    This enables proper list indentation through parent-child relationships.
    """
    # Flatten the nested tree into a flat list with ID references
    counter = [0]  # mutable counter for generating unique IDs
    top_level_ids = []
    all_descendants = []

    for bd in block_dicts:
        block_id = _flatten_block_tree(bd, counter, all_descendants)
        top_level_ids.append(block_id)

    # Convert all descendants to SDK Block objects with block_id and children as string IDs
    sdk_blocks = []
    for desc in all_descendants:
        block = _dict_to_block_simple(desc)
        block.block_id = desc["_id"]
        if "_child_ids" in desc and desc["_child_ids"]:
            block.children = desc["_child_ids"]
        sdk_blocks.append(block)

    # Use descendant API
    request = CreateDocumentBlockDescendantRequest.builder() \
        .document_id(doc_id) \
        .block_id(doc_id) \
        .request_body(
            CreateDocumentBlockDescendantRequestBody.builder()
            .children_id(top_level_ids)
            .index(-1)
            .descendants(sdk_blocks)
            .build()
        ) \
        .build()

    response = _sdk_call_with_retry(
        lambda: client.docx.v1.document_block_descendant.create(request),
        desc="write nested blocks"
    )

    if not response.success():
        print(json.dumps({
            "success": False,
            "error": f"[{response.code}] {response.msg}",
            "blocks_written": 0
        }, ensure_ascii=False))
        return -1

    return len(block_dicts)


def _flatten_block_tree(bd: dict, counter: list, result: list) -> str:
    """Flatten a nested block dict tree into a flat list with ID references.

    Each block gets a unique _id and _child_ids fields.
    Returns the block_id of this block.
    """
    counter[0] += 1
    block_id = f"blk_{counter[0]}"

    child_ids = []
    if "children" in bd and bd["children"]:
        for child_bd in bd["children"]:
            child_id = _flatten_block_tree(child_bd, counter, result)
            child_ids.append(child_id)

    # Create a copy without the nested "children" key, add _id and _child_ids
    flat_bd = {k: v for k, v in bd.items() if k != "children"}
    flat_bd["_id"] = block_id
    flat_bd["_child_ids"] = child_ids

    result.append(flat_bd)
    return block_id


def _dict_to_block_simple(bd: dict) -> "Block":
    """Convert a block dict (without nested children) to a lark-oapi Block object."""
    block = Block()
    block.block_type = bd["block_type"]

    for field_name in ["text", "heading1", "heading2", "heading3", "heading4",
                       "heading5", "heading6", "heading7", "heading8", "heading9",
                       "bullet", "ordered", "code", "quote", "todo", "divider"]:
        if field_name in bd:
            setattr(block, field_name, _dict_to_text(bd[field_name], field_name))
            break

    return block


def _write_table_block(client, doc_id: str, table_dict: dict, index: int = -1) -> bool:
    """Create a table block and fill cells with content via batch_update.

    Optimized flow (Phase 2):
    1. Create empty table block via SDK → get cell_ids
    2. List all blocks to get cell internal text block IDs
    3. Batch update all cell contents in one API call
    4. Set column widths via per-column PATCH (batch not supported)

    Falls back to legacy per-cell write if batch_update fails.

    Args:
        client: Feishu API client
        doc_id: Document ID
        table_dict: Table block dict from markdown_to_blocks
        index: Insert position (-1 = append to end)

    Returns True on success, False on failure.
    """
    table_data = table_dict.get("table", {})
    rows = table_data.get("rows", [])
    row_count = len(rows)
    col_count = table_data.get("column_size", 0)

    if row_count == 0 or col_count == 0:
        return True  # Skip empty tables

    # Step 1: Create empty table block via SDK (with retry)
    table_block = Block()
    table_block.block_type = BLOCK_TYPE_TABLE

    table_prop = TableProperty()
    table_prop.row_size = row_count
    table_prop.column_size = col_count

    table_obj = Table()
    table_obj.property = table_prop

    table_block.table = table_obj

    request = CreateDocumentBlockChildrenRequest.builder() \
        .document_id(doc_id) \
        .block_id(doc_id) \
        .request_body(
            CreateDocumentBlockChildrenRequestBody.builder()
            .children([table_block])
            .index(index)
            .build()
        ) \
        .build()

    response = _sdk_call_with_retry(
        lambda: client.docx.v1.document_block_children.create(request),
        desc=f"table create (rows={row_count}, cols={col_count})"
    )

    if not response or not response.success():
        err_msg = f"Table create failed (rows={row_count}, cols={col_count}): [{response.code}] {response.msg}"
        if response.code == 1770001 and row_count > 9:
            err_msg += " — NOTE: 飞书限制单次创建最多 9 行"
        print(json.dumps({
            "success": False,
            "error": err_msg
        }, ensure_ascii=False), file=sys.stderr)
        return False

    # Step 2: Get cell block IDs from response (handle empty/null data)
    if not response.data:
        print("Warning: Table created but response data is empty", file=sys.stderr)
        return True

    created_blocks = response.data.children if response.data.children else []
    if not created_blocks:
        print("Warning: Table created but no block data returned", file=sys.stderr)
        return True

    table_resp_block = None
    for cb in created_blocks:
        if cb.block_type == BLOCK_TYPE_TABLE:
            table_resp_block = cb
            break

    if not table_resp_block or not table_resp_block.table:
        print("Warning: Table block not found in response", file=sys.stderr)
        return True

    cell_ids = list(table_resp_block.table.cells or [])
    table_block_id = table_resp_block.block_id

    if len(cell_ids) != row_count * col_count:
        print(f"Warning: Expected {row_count * col_count} cells, got {len(cell_ids)}", file=sys.stderr)

    # Step 3: Get cell internal text block IDs via list blocks API
    time.sleep(0.5)  # Brief wait for server-side block generation
    cell_text_block_ids = _get_cell_text_block_ids(client, doc_id, cell_ids)

    if not cell_text_block_ids or len(cell_text_block_ids) != len(cell_ids):
        # Fallback to legacy per-cell write
        print(f"Warning: Cannot get cell text block IDs "
              f"(got {len(cell_text_block_ids) if cell_text_block_ids else 0}/{len(cell_ids)}), "
              f"falling back to per-cell write", file=sys.stderr)
        return _write_table_block_legacy(
            client, doc_id, table_dict, rows, row_count, col_count,
            table_resp_block, cell_ids)

    # Step 4: Build batch_update requests for all cell contents
    update_requests = []
    for row_idx, row in enumerate(rows):
        for col_idx, cell_content in enumerate(row):
            cell_flat_idx = row_idx * col_count + col_idx
            if cell_flat_idx >= len(cell_ids):
                break

            cell_id = cell_ids[cell_flat_idx]
            text_block_id = cell_text_block_ids.get(cell_id)
            if not text_block_id or not cell_content.strip():
                continue

            # Parse inline formatting and build SDK text elements
            elements = _parse_inline_simple_import(cell_content.strip())
            sdk_elements = _build_sdk_text_elements(elements)
            if not sdk_elements:
                continue

            update_req = UpdateBlockRequest.builder() \
                .block_id(text_block_id) \
                .update_text_elements(
                    UpdateTextElementsRequest.builder()
                    .elements(sdk_elements)
                    .build()
                ) \
                .build()
            update_requests.append(update_req)

    # Step 5: Execute batch_update
    batch_ok = False
    if update_requests:
        batch_req = BatchUpdateDocumentBlockRequest.builder() \
            .document_id(doc_id) \
            .request_body(
                BatchUpdateDocumentBlockRequestBody.builder()
                .requests(update_requests)
                .build()
            ) \
            .build()

        batch_resp = _sdk_call_with_retry(
            lambda: client.docx.v1.document_block.batch_update(batch_req),
            desc="batch_update cell contents"
        )
        if batch_resp.success():
            batch_ok = True
        else:
            print(f"Warning: batch_update failed: [{batch_resp.code}] {batch_resp.msg}",
                  file=sys.stderr)

        if not batch_ok:
            # Fallback to legacy per-cell write
            print("Warning: batch_update failed, falling back to per-cell write",
                  file=sys.stderr)
            return _write_table_block_legacy(
                client, doc_id, table_dict, rows, row_count, col_count,
                table_resp_block, cell_ids)
    else:
        batch_ok = True  # No content to write (all cells empty)

    # Step 6: Set column widths if available (still per-column PATCH)
    column_widths = table_data.get("column_widths", [])
    if column_widths and table_block_id:
        token = _get_tenant_token()
        _set_table_column_widths(doc_id, table_block_id, column_widths, token)

    return True


def _get_cell_text_block_ids(client, doc_id: str, cell_ids: list) -> dict:
    """Get the internal text block ID for each table cell.

    When Feishu creates a table, each cell automatically gets an empty text block.
    This function retrieves those text block IDs so we can update them via batch_update.

    Args:
        client: Feishu API client
        doc_id: Document ID
        cell_ids: List of cell block IDs from table creation response

    Returns:
        Dict mapping cell_id → text_block_id, or empty dict on failure.
    """
    # Use list blocks API with pagination to get all blocks in the document
    all_blocks = []
    page_token = None

    while True:
        req_builder = ListDocumentBlockRequest.builder() \
            .document_id(doc_id) \
            .page_size(500)
        if page_token:
            req_builder = req_builder.page_token(page_token)
        list_req = req_builder.build()

        list_resp = _sdk_call_with_retry(
            lambda req=list_req: client.docx.v1.document_block.list(req),
            desc="list blocks for cell text IDs"
        )
        if not list_resp.success():
            print(f"Warning: list blocks failed: [{list_resp.code}] {list_resp.msg}",
                  file=sys.stderr)
            return {}

        if list_resp.data and list_resp.data.items:
            all_blocks.extend(list_resp.data.items)

        # Check for more pages
        if list_resp.data and list_resp.data.has_more:
            page_token = list_resp.data.page_token
        else:
            break

    # Build block_id → block mapping
    block_map = {}
    for block in all_blocks:
        block_map[block.block_id] = block

    # Find each cell's internal text block
    result = {}
    for cell_id in cell_ids:
        cell_block = block_map.get(cell_id)
        if cell_block and cell_block.children:
            text_block_id = cell_block.children[0]
            text_block = block_map.get(text_block_id)
            if text_block and text_block.block_type == 2:  # text block
                result[cell_id] = text_block_id

    return result


def _build_sdk_text_elements(elements: list) -> list:
    """Convert parsed inline elements to SDK TextElement objects.

    Args:
        elements: List of element dicts from _parse_inline_simple

    Returns:
        List of SDK TextElement objects
    """
    sdk_elements = []
    for elem_dict in elements:
        if "text_run" not in elem_dict:
            continue

        te = TextElement()
        tr = TextRun()
        tr.content = elem_dict["text_run"]["content"]

        style_dict = elem_dict["text_run"].get("text_element_style", {})
        style = TextElementStyle()
        if style_dict.get("bold"):
            style.bold = True
        if style_dict.get("italic"):
            style.italic = True
        if style_dict.get("strikethrough"):
            style.strikethrough = True
        if style_dict.get("inline_code"):
            style.inline_code = True
        if "link" in style_dict and style_dict["link"]:
            link = Link()
            link.url = style_dict["link"].get("url", "")
            style.link = link

        tr.text_element_style = style
        te.text_run = tr
        sdk_elements.append(te)

    return sdk_elements


def _write_table_block_legacy(client, doc_id: str, table_dict: dict,
                               rows: list, row_count: int, col_count: int,
                               table_resp_block, cell_ids: list) -> bool:
    """Legacy per-cell table write — fallback when batch_update is unavailable.

    This is the original implementation that writes content to each cell individually
    via HTTP API (POST content + DELETE empty block per cell).

    Args:
        client: Feishu API client
        doc_id: Document ID
        table_dict: Table block dict from markdown_to_blocks
        rows: Table rows data
        row_count: Number of rows
        col_count: Number of columns
        table_resp_block: Table block from creation response
        cell_ids: List of cell block IDs

    Returns True on success, False on failure.
    """
    table_data = table_dict.get("table", {})
    token = _get_tenant_token()
    failed_cells = []

    for row_idx, row in enumerate(rows):
        for col_idx, cell_content in enumerate(row):
            cell_flat_idx = row_idx * col_count + col_idx
            if cell_flat_idx >= len(cell_ids):
                break

            cell_block_id = cell_ids[cell_flat_idx]

            if not cell_content.strip():
                continue  # Skip empty cells

            # Build text elements with inline formatting
            elements = _parse_inline_simple_import(cell_content.strip())
            api_elements = []
            for elem_dict in elements:
                if "text_run" in elem_dict:
                    style = elem_dict["text_run"].get("text_element_style", {})
                    # Only include non-empty style fields for API compatibility
                    clean_style = {}
                    for k, v in style.items():
                        if v:  # Skip False, None, empty values
                            clean_style[k] = v
                    api_elements.append({
                        "text_run": {
                            "content": elem_dict["text_run"]["content"],
                            "text_element_style": clean_style
                        }
                    })

            url = f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/blocks/{cell_block_id}/children"
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            body = {
                "children": [{
                    "block_type": 2,
                    "text": {
                        "elements": api_elements
                    }
                }],
                "index": 0
            }

            resp = _http_request_with_retry(
                "post", url, max_retries=5,
                desc=f"write cell [{row_idx},{col_idx}]",
                headers=headers, json=body
            )
            success = resp.status_code == 200 and resp.json().get("code") == 0
            if not success:
                if resp.status_code != 200:
                    print(f"Warning: HTTP {resp.status_code} writing cell [{row_idx},{col_idx}]",
                          file=sys.stderr)
                else:
                    data = resp.json()
                    print(f"Warning: Failed to write cell [{row_idx},{col_idx}]: "
                          f"[{data.get('code')}] {data.get('msg')}", file=sys.stderr)

            if success:
                # Delete the default empty text block that Feishu auto-creates in each cell.
                # We inserted our content at index 0, so the empty block is now at index 1.
                del_url = (f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}"
                           f"/blocks/{cell_block_id}/children/batch_delete")
                del_body = {"start_index": 1, "end_index": 2}
                _http_request_with_retry(
                    "delete", del_url, max_retries=5,
                    desc=f"delete empty cell block [{row_idx},{col_idx}]",
                    headers=headers, json=del_body
                )
            else:
                failed_cells.append(f"[{row_idx},{col_idx}]")

    if failed_cells:
        print(f"Warning: {len(failed_cells)} cells failed to write: {', '.join(failed_cells)}",
              file=sys.stderr)

    # Set column widths if available
    column_widths = table_data.get("column_widths", [])
    if column_widths and table_resp_block:
        table_block_id = table_resp_block.block_id
        _set_table_column_widths(doc_id, table_block_id, column_widths, token)

    return True


def _set_table_column_widths(doc_id: str, table_block_id: str,
                              column_widths: list, token: str) -> None:
    """Set column widths for a table block via PATCH API.

    Feishu's update_table_property API updates one column at a time:
    - column_index: which column to update (0-based)
    - column_width: width in px

    Args:
        doc_id: Document ID
        table_block_id: Table block ID
        column_widths: List of column widths (integers)
        token: Tenant access token
    """
    url = f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/blocks/{table_block_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    for col_idx, width in enumerate(column_widths):
        body = {
            "update_table_property": {
                "column_width": width,
                "column_index": col_idx,
            }
        }

        try:
            resp = _http_request_with_retry(
                "patch", url, desc=f"set column {col_idx} width",
                headers=headers, json=body
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") != 0:
                    print(f"Warning: Failed to set column {col_idx} width: "
                          f"[{data.get('code')}] {data.get('msg')}", file=sys.stderr)
            else:
                print(f"Warning: HTTP {resp.status_code} setting column {col_idx} width",
                      file=sys.stderr)
        except Exception as e:
            print(f"Warning: Exception setting column {col_idx} width: {e}",
                  file=sys.stderr)


def _parse_inline_simple_import(text: str):
    """Import and call _parse_inline_simple from md_to_blocks."""
    from md_to_blocks import _parse_inline_simple
    return _parse_inline_simple(text)


def cmd_read(args):
    """Read content from a Feishu document."""
    client = create_client(args.app)

    if args.format == "raw":
        return _read_raw(client, args.doc)
    else:
        return _read_blocks(client, args.doc)


def _read_raw(client, doc_id: str) -> int:
    """Read document as raw text."""
    request = RawContentDocumentRequest.builder() \
        .document_id(doc_id) \
        .build()

    response = _sdk_call_with_retry(
        lambda: client.docx.v1.document.raw_content(request),
        desc="read raw content"
    )

    if not response.success():
        print(json.dumps({
            "success": False,
            "error": f"[{response.code}] {response.msg}"
        }, ensure_ascii=False))
        return 1

    result = {
        "success": True,
        "document_id": doc_id,
        "content": response.data.content
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _read_blocks(client, doc_id: str) -> int:
    """Read document as block structure."""
    request = ListDocumentBlockRequest.builder() \
        .document_id(doc_id) \
        .page_size(500) \
        .build()

    response = _sdk_call_with_retry(
        lambda: client.docx.v1.document_block.list(request),
        desc="read blocks"
    )

    if not response.success():
        print(json.dumps({
            "success": False,
            "error": f"[{response.code}] {response.msg}"
        }, ensure_ascii=False))
        return 1

    # Serialize blocks to JSON-friendly format
    blocks_data = []
    if response.data and response.data.items:
        # Build a lookup from block_id → block dict for parent-child resolution
        all_block_dicts = {}
        for block in response.data.items:
            bd = _block_to_dict(block)
            all_block_dicts[bd["block_id"]] = bd

        # For table blocks, attach cell content by resolving children recursively
        for bid, bd in all_block_dicts.items():
            if bd.get("block_type") == 31 and "table" in bd:
                cell_ids = bd["table"].get("cells", [])
                cell_contents = []
                for cell_id in cell_ids:
                    cell_block = all_block_dicts.get(cell_id)
                    if cell_block and cell_block.get("children"):
                        # Collect text from child blocks of the cell
                        cell_text_parts = []
                        for child_id in cell_block["children"]:
                            child_block = all_block_dicts.get(child_id)
                            if child_block and "content" in child_block:
                                cell_text_parts.append(child_block["content"])
                        cell_contents.append("".join(cell_text_parts))
                    else:
                        cell_contents.append("")
                bd["table"]["cell_contents"] = cell_contents

        for block in response.data.items:
            blocks_data.append(all_block_dicts[block.block_id])

    result = {
        "success": True,
        "document_id": doc_id,
        "block_count": len(blocks_data),
        "blocks": blocks_data
    }
    try:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (TypeError, ValueError) as e:
        print(f"ERROR: JSON serialization failed: {e}", file=sys.stderr)
        # Fallback: try with _safe_serialize on the entire result
        try:
            print(json.dumps(_safe_serialize(result), ensure_ascii=False, indent=2))
        except Exception as e2:
            print(json.dumps({
                "success": False,
                "error": f"JSON serialization failed: {e2}"
            }, ensure_ascii=False))
            return 1
    return 0


def cmd_create_and_write(args):
    """Create a new document and write content to it."""
    client = create_client(args.app)

    # Get markdown content
    markdown = _get_markdown_content(args)
    if markdown is None:
        return 1

    # Step 1: Create document
    body_builder = CreateDocumentRequestBody.builder() \
        .title(args.title)

    if args.folder:
        body_builder = body_builder.folder_token(args.folder)

    create_request = CreateDocumentRequest.builder() \
        .request_body(body_builder.build()) \
        .build()

    create_response = _sdk_call_with_retry(
        lambda: client.docx.v1.document.create(create_request),
        desc="create document"
    )

    if not create_response.success():
        print(json.dumps({
            "success": False,
            "error": f"Create failed: [{create_response.code}] {create_response.msg}"
        }, ensure_ascii=False))
        return 1

    doc_id = create_response.data.document.document_id

    # P1-4: Auto add member after creating document (before writing content)
    add_member_id = getattr(args, 'add_member', None)
    if add_member_id:
        member_perm = getattr(args, 'member_perm', 'full_access') or 'full_access'
        member_ok = _add_member(client, doc_id, add_member_id, member_perm)
        if not member_ok:
            print(f"Warning: Failed to add member {add_member_id}, continuing with write...",
                  file=sys.stderr)

    # Step 2: Convert and write content (reuse shared write logic)
    block_dicts = markdown_to_blocks(markdown)
    if not block_dicts:
        result = {
            "success": True,
            "document_id": doc_id,
            "title": args.title,
            "blocks_written": 0,
            "url": f"https://feishu.cn/docx/{doc_id}",
            "note": "Document created but no content blocks generated"
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    resume_from = getattr(args, 'resume_from', 0) or 0
    return _write_blocks_to_doc(client, doc_id, block_dicts, resume_from=resume_from)


# ── Helper functions ──────────────────────────────────────────────────

def _get_markdown_content(args) -> Optional[str]:
    """Get markdown content from args (--markdown or --markdown-file)."""
    if hasattr(args, 'markdown_file') and args.markdown_file:
        if not os.path.exists(args.markdown_file):
            print(json.dumps({
                "success": False,
                "error": f"File not found: {args.markdown_file}"
            }, ensure_ascii=False))
            return None
        with open(args.markdown_file, 'r') as f:
            return f.read()
    elif hasattr(args, 'markdown') and args.markdown:
        return args.markdown
    else:
        # Try reading from stdin if no markdown args
        if not sys.stdin.isatty():
            return sys.stdin.read()
        print(json.dumps({
            "success": False,
            "error": "No content provided. Use --markdown, --markdown-file, or pipe via stdin"
        }, ensure_ascii=False))
        return None


def _dict_to_text(d: dict, field_name: str):
    """Convert a block content dict to the appropriate lark-oapi model.

    For most block types, this is a Text object with elements.
    For divider, it's a Divider object (empty).
    """
    if field_name == "divider":
        return Divider()

    text = Text()

    if "elements" in d:
        elements = []
        for elem_dict in d["elements"]:
            te = TextElement()
            if "text_run" in elem_dict:
                tr = TextRun()
                tr.content = elem_dict["text_run"]["content"]
                style_dict = elem_dict["text_run"].get("text_element_style", {})
                if style_dict:
                    style = TextElementStyle()
                    if style_dict.get("bold"):
                        style.bold = True
                    if style_dict.get("italic"):
                        style.italic = True
                    if style_dict.get("strikethrough"):
                        style.strikethrough = True
                    if style_dict.get("inline_code"):
                        style.inline_code = True
                    if "link" in style_dict:
                        link = Link()
                        link.url = style_dict["link"]["url"]
                        style.link = link
                    tr.text_element_style = style
                else:
                    tr.text_element_style = TextElementStyle()
                te.text_run = tr
            elements.append(te)
        text.elements = elements

    if "style" in d:
        style = TextStyle()
        if "language" in d["style"]:
            style.language = d["style"]["language"]
        if "done" in d["style"]:
            style.done = d["style"]["done"]
        text.style = style

    return text


def _block_to_dict(block) -> dict:
    """Convert a lark-oapi Block object to a JSON-serializable dict."""
    result = {
        "block_id": block.block_id,
        "block_type": block.block_type,
        "parent_id": block.parent_id,
    }

    if block.children:
        result["children"] = block.children

    # Extract text content from the appropriate field
    field_map = {
        2: "text", 3: "heading1", 4: "heading2", 5: "heading3",
        6: "heading4", 7: "heading5", 8: "heading6", 9: "heading7",
        10: "heading8", 11: "heading9", 12: "bullet", 13: "ordered",
        14: "code", 15: "quote", 17: "todo",
    }

    field_name = field_map.get(block.block_type)
    if field_name:
        text_obj = getattr(block, field_name, None)
        if text_obj and hasattr(text_obj, 'elements') and text_obj.elements:
            content_parts = []
            for elem in text_obj.elements:
                if hasattr(elem, 'text_run') and elem.text_run:
                    content_parts.append(elem.text_run.content or "")
            result["content"] = "".join(content_parts)

    # Handle table block (block_type=31)
    if block.block_type == 31:
        table_obj = getattr(block, 'table', None)
        if table_obj:
            table_info = {}
            prop = getattr(table_obj, 'property', None)
            if prop:
                table_info["row_size"] = getattr(prop, 'row_size', None)
                table_info["column_size"] = getattr(prop, 'column_size', None)
                table_info["header_row"] = getattr(prop, 'header_row', None)
            cells = getattr(table_obj, 'cells', None)
            if cells:
                table_info["cells"] = list(cells)
            result["table"] = table_info

    return _safe_serialize(result)


def _safe_serialize(obj):
    """Convert lark-oapi objects to JSON-serializable types.

    Ensures all values in the output are basic Python types
    (str/int/float/bool/list/dict/None) that json.dumps can handle.
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, list):
        return [_safe_serialize(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _safe_serialize(v) for k, v in obj.items()}
    # lark-oapi object: try to extract __dict__
    if hasattr(obj, '__dict__'):
        return {k: _safe_serialize(v) for k, v in obj.__dict__.items()
                if not k.startswith('_')}
    return str(obj)


# ── Comment operations ────────────────────────────────────────────────

def _get_tenant_token(app_name: str = "ST") -> str:
    """Get tenant_access_token via HTTP for APIs not covered by SDK."""
    app_id, app_secret = load_feishu_credentials(app_name)
    if http_requests is None:
        print("ERROR: 'requests' library not available", file=sys.stderr)
        sys.exit(1)
    resp = _http_request_with_retry(
        "post",
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        desc="get tenant token",
        json={"app_id": app_id, "app_secret": app_secret},
    )
    data = resp.json()
    if data.get("code") != 0:
        print(f"ERROR: Failed to get token: {data}", file=sys.stderr)
        sys.exit(1)
    return data["tenant_access_token"]


def cmd_list_comments(args):
    """List comments on a Feishu document."""
    client = create_client(args.app)

    request = ListFileCommentRequest.builder() \
        .file_token(args.doc) \
        .file_type("docx") \
        .build()

    response = _sdk_call_with_retry(
        lambda: client.drive.v1.file_comment.list(request),
        desc="list comments"
    )

    if not response.success():
        print(json.dumps({
            "success": False,
            "error": f"[{response.code}] {response.msg}"
        }, ensure_ascii=False))
        return 1

    comments = []
    if response.data and response.data.items:
        for item in response.data.items:
            comment = {
                "comment_id": item.comment_id,
                "is_solved": item.is_solved,
                "quote": item.quote or "",
                "replies": [],
            }
            if item.reply_list and item.reply_list.replies:
                for reply in item.reply_list.replies:
                    reply_text = ""
                    if reply.content and reply.content.elements:
                        parts = []
                        for elem in reply.content.elements:
                            if elem.type == "text_run" and elem.text_run:
                                parts.append(elem.text_run.text or "")
                        reply_text = "".join(parts)
                    comment["replies"].append({
                        "reply_id": reply.reply_id,
                        "text": reply_text,
                    })
            # Apply status filter
            if args.status == "solved" and not item.is_solved:
                continue
            if args.status == "unsolved" and item.is_solved:
                continue
            comments.append(comment)

    result = {
        "success": True,
        "document_id": args.doc,
        "comment_count": len(comments),
        "comments": comments,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_reply_comment(args):
    """Reply to a comment on a Feishu document.

    Uses HTTP API directly because lark-oapi SDK lacks CreateFileCommentReply.
    """
    token = _get_tenant_token(args.app)

    url = (
        f"https://open.feishu.cn/open-apis/drive/v1/files/{args.doc}"
        f"/comments/{args.comment}/replies"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = {
        "content": {
            "elements": [{
                "type": "text_run",
                "text_run": {"text": args.text},
            }]
        }
    }
    resp = _http_request_with_retry(
        "post", url, desc="reply comment",
        headers=headers, json=body, params={"file_type": "docx"}
    )
    data = resp.json()

    if data.get("code") == 0:
        reply_data = data.get("data", {})
        result = {
            "success": True,
            "document_id": args.doc,
            "comment_id": args.comment,
            "reply_id": reply_data.get("reply_id"),
            "text": args.text,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    else:
        print(json.dumps({
            "success": False,
            "error": f"[{data.get('code')}] {data.get('msg')}",
        }, ensure_ascii=False))
        return 1


def cmd_resolve_comment(args):
    """Mark a comment as resolved."""
    client = create_client(args.app)

    request = PatchFileCommentRequest.builder() \
        .file_token(args.doc) \
        .comment_id(args.comment) \
        .file_type("docx") \
        .request_body(DriveFileComment.builder().is_solved(True).build()) \
        .build()

    response = _sdk_call_with_retry(
        lambda: client.drive.v1.file_comment.patch(request),
        desc="resolve comment"
    )

    if response.success():
        print(json.dumps({
            "success": True,
            "document_id": args.doc,
            "comment_id": args.comment,
            "action": "resolved",
        }, ensure_ascii=False, indent=2))
        return 0
    else:
        print(json.dumps({
            "success": False,
            "error": f"[{response.code}] {response.msg}",
        }, ensure_ascii=False))
        return 1


def cmd_add_comment(args):
    """Add a new comment to a Feishu document.

    Uses HTTP API directly for reliable quote + content handling.
    """
    token = _get_tenant_token(args.app)

    url = f"https://open.feishu.cn/open-apis/drive/v1/files/{args.doc}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = {
        "reply_list": {
            "replies": [{
                "content": {
                    "elements": [{
                        "type": "text_run",
                        "text_run": {"text": args.text},
                    }]
                }
            }]
        },
    }
    if args.quote:
        body["quote"] = args.quote
    if args.is_whole:
        body["is_whole"] = True

    resp = _http_request_with_retry(
        "post", url, desc="add comment",
        headers=headers, json=body, params={"file_type": "docx"}
    )
    data = resp.json()

    if data.get("code") == 0:
        comment_data = data.get("data", {})
        result = {
            "success": True,
            "document_id": args.doc,
            "comment_id": comment_data.get("comment_id"),
            "quote": args.quote or "(whole document)",
            "text": args.text,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    else:
        print(json.dumps({
            "success": False,
            "error": f"[{data.get('code')}] {data.get('msg')}",
        }, ensure_ascii=False))
        return 1


def _add_member(client, doc_id: str, open_id: str, perm: str = "full_access") -> bool:
    """Add a collaborator to a Feishu document (internal function).

    Args:
        client: Feishu API client
        doc_id: Document ID
        open_id: User open_id (ou_xxx)
        perm: Permission level (full_access / edit / view)

    Returns:
        True on success, False on failure.
    """
    request = CreatePermissionMemberRequest.builder() \
        .token(doc_id) \
        .type("docx") \
        .request_body(BaseMember.builder()
            .member_type("openid")
            .member_id(open_id)
            .perm(perm)
            .build()) \
        .build()

    response = _sdk_call_with_retry(
        lambda: client.drive.v1.permission_member.create(request),
        desc="add member"
    )
    return response.success()


def cmd_add_member(args):
    """Add a collaborator to a Feishu document."""
    client = create_client(args.app)

    success = _add_member(client, args.doc, args.open_id, args.perm)

    if success:
        print(json.dumps({
            "success": True,
            "document_id": args.doc,
            "open_id": args.open_id,
            "perm": args.perm,
            "action": "member_added",
        }, ensure_ascii=False, indent=2))
        return 0
    else:
        print(json.dumps({
            "success": False,
            "error": "Failed to add member",
        }, ensure_ascii=False))
        return 1


# ── Block-level editing operations ────────────────────────────────────

def cmd_patch_block(args):
    """Update the text content of a specific block in-place.

    This is the preferred way to edit documents — it preserves edit history
    and only modifies the targeted block.
    """
    token = _get_tenant_token(args.app)

    # Build text elements from markdown content
    elements = _parse_inline_simple_import(args.text)
    api_elements = []
    for elem_dict in elements:
        if "text_run" in elem_dict:
            style = elem_dict["text_run"].get("text_element_style", {})
            clean_style = {}
            for k, v in style.items():
                if v:
                    clean_style[k] = v
            api_elements.append({
                "text_run": {
                    "content": elem_dict["text_run"]["content"],
                    "text_element_style": clean_style
                }
            })

    url = f"https://open.feishu.cn/open-apis/docx/v1/documents/{args.doc}/blocks/{args.block}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = {
        "update_text_elements": {
            "elements": api_elements
        }
    }

    try:
        resp = _http_request_with_retry(
            "patch", url, desc="patch block",
            headers=headers, json=body
        )
        data = resp.json()
    except Exception as e:
        print(json.dumps({
            "success": False,
            "error": f"HTTP request failed: {e}"
        }, ensure_ascii=False))
        return 1

    if data.get("code") == 0:
        block_data = data.get("data", {}).get("block", {})
        result = {
            "success": True,
            "document_id": args.doc,
            "block_id": args.block,
            "action": "patched",
            "revision_id": data.get("data", {}).get("document_revision_id"),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    else:
        print(json.dumps({
            "success": False,
            "error": f"[{data.get('code')}] {data.get('msg')}"
        }, ensure_ascii=False))
        return 1


def cmd_delete_blocks(args):
    """Delete a range of child blocks from a document.

    Uses start_index and end_index (exclusive) relative to the parent block's children.
    Typically the parent is the page block (doc_id itself).
    """
    client = create_client(args.app)

    parent_id = args.parent or args.doc  # Default parent is the page block

    del_request = BatchDeleteDocumentBlockChildrenRequest.builder() \
        .document_id(args.doc) \
        .block_id(parent_id) \
        .request_body(
            BatchDeleteDocumentBlockChildrenRequestBody.builder()
            .start_index(args.start)
            .end_index(args.end)
            .build()
        ) \
        .build()

    del_response = _sdk_call_with_retry(
        lambda: client.docx.v1.document_block_children.batch_delete(del_request),
        desc="delete blocks"
    )

    if del_response.success():
        result = {
            "success": True,
            "document_id": args.doc,
            "parent_id": parent_id,
            "deleted_range": f"[{args.start}, {args.end})",
            "action": "deleted",
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    else:
        print(json.dumps({
            "success": False,
            "error": f"[{del_response.code}] {del_response.msg}"
        }, ensure_ascii=False))
        return 1


def cmd_insert_blocks(args):
    """Insert markdown content at a specific index position in the document.

    This allows inserting new content between existing blocks without
    affecting the rest of the document.
    """
    client = create_client(args.app)

    # Get markdown content
    markdown = _get_markdown_content(args)
    if markdown is None:
        return 1

    block_dicts = markdown_to_blocks(markdown)
    if not block_dicts:
        print(json.dumps({"success": False, "error": "No content to insert"}, ensure_ascii=False))
        return 1

    parent_id = args.parent or args.doc
    index = args.index

    # Split block_dicts into segments: consecutive regular blocks vs table blocks
    segments = []
    current_regular = []

    for bd in block_dicts:
        if bd.get("block_type") == BLOCK_TYPE_TABLE:
            if current_regular:
                segments.append(("regular", current_regular))
                current_regular = []
            segments.append(("table", bd))
        else:
            current_regular.append(bd)

    if current_regular:
        segments.append(("regular", current_regular))

    total_written = 0
    current_index = index

    for seg_type, seg_data in segments:
        if seg_type == "regular":
            # Build Block objects
            children = []
            for bd in seg_data:
                block = Block()
                block.block_type = bd["block_type"]

                for field_name in ["text", "heading1", "heading2", "heading3", "heading4",
                                   "heading5", "heading6", "heading7", "heading8", "heading9",
                                   "bullet", "ordered", "code", "quote", "todo", "divider"]:
                    if field_name in bd:
                        setattr(block, field_name, _dict_to_text(bd[field_name], field_name))
                        break

                children.append(block)

            # Write in batches at the specified index
            BATCH_SIZE = 50
            for batch_start in range(0, len(children), BATCH_SIZE):
                batch = children[batch_start:batch_start + BATCH_SIZE]

                request = CreateDocumentBlockChildrenRequest.builder() \
                    .document_id(args.doc) \
                    .block_id(parent_id) \
                    .request_body(
                        CreateDocumentBlockChildrenRequestBody.builder()
                        .children(batch)
                        .index(current_index)
                        .build()
                    ) \
                    .build()

                response = _sdk_call_with_retry(
                    lambda req=request: client.docx.v1.document_block_children.create(req),
                    desc="insert blocks"
                )

                if not response.success():
                    print(json.dumps({
                        "success": False,
                        "error": f"[{response.code}] {response.msg}",
                        "blocks_written": total_written
                    }, ensure_ascii=False))
                    return 1

                total_written += len(batch)
                current_index += len(batch)

        elif seg_type == "table":
            # Table blocks are always appended at the end for now
            ok = _write_table_block(client, args.doc, seg_data, index=current_index)
            if not ok:
                return 1
            total_written += 1
            current_index += 1

    result = {
        "success": True,
        "document_id": args.doc,
        "blocks_inserted": total_written,
        "at_index": index,
        "url": f"https://feishu.cn/docx/{args.doc}"
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


# ── CLI argument parsing ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="飞书文档操作 CLI — nanobot feishu-docs skill",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--app", default="ST", help="Feishu app name in config (default: ST)")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # create
    create_parser = subparsers.add_parser("create", help="Create a new document")
    create_parser.add_argument("--title", required=True, help="Document title")
    create_parser.add_argument("--folder", help="Target folder token")

    # write
    write_parser = subparsers.add_parser("write", help="Write content to a document")
    write_parser.add_argument("--doc", required=True, help="Document ID")
    write_parser.add_argument("--markdown", help="Markdown content string")
    write_parser.add_argument("--markdown-file", help="Path to Markdown file")
    write_parser.add_argument("--mode", choices=["append", "overwrite"], default="append",
                              help="Write mode: append (default) or overwrite (clear first)")
    write_parser.add_argument("--resume-from", type=int, default=0,
                              help="Resume from chunk index (0-based, for continuing after failure)")

    # read
    read_parser = subparsers.add_parser("read", help="Read document content")
    read_parser.add_argument("--doc", required=True, help="Document ID")
    read_parser.add_argument("--format", choices=["raw", "blocks"], default="raw",
                             help="Output format (default: raw)")

    # create-and-write
    caw_parser = subparsers.add_parser("create-and-write",
                                        help="Create a document and write content")
    caw_parser.add_argument("--title", required=True, help="Document title")
    caw_parser.add_argument("--folder", help="Target folder token")
    caw_parser.add_argument("--markdown", help="Markdown content string")
    caw_parser.add_argument("--markdown-file", help="Path to Markdown file")
    caw_parser.add_argument("--resume-from", type=int, default=0,
                            help="Resume from chunk index (0-based, for continuing after failure)")
    caw_parser.add_argument("--add-member",
                            help="Auto-add collaborator by open_id (ou_xxx) after creating doc")
    caw_parser.add_argument("--member-perm", choices=["full_access", "edit", "view"],
                            default="full_access",
                            help="Permission for --add-member (default: full_access)")

    # list-comments
    lc_parser = subparsers.add_parser("list-comments", help="List comments on a document")
    lc_parser.add_argument("--doc", required=True, help="Document ID")
    lc_parser.add_argument("--status", choices=["all", "solved", "unsolved"], default="all",
                           help="Filter by status (default: all)")

    # reply-comment
    rc_parser = subparsers.add_parser("reply-comment", help="Reply to a comment")
    rc_parser.add_argument("--doc", required=True, help="Document ID")
    rc_parser.add_argument("--comment", required=True, help="Comment ID")
    rc_parser.add_argument("--text", required=True, help="Reply text")

    # resolve-comment
    rsc_parser = subparsers.add_parser("resolve-comment", help="Mark a comment as resolved")
    rsc_parser.add_argument("--doc", required=True, help="Document ID")
    rsc_parser.add_argument("--comment", required=True, help="Comment ID")

    # add-comment
    ac_parser = subparsers.add_parser("add-comment", help="Add a new comment to a document")
    ac_parser.add_argument("--doc", required=True, help="Document ID")
    ac_parser.add_argument("--text", required=True, help="Comment text")
    ac_parser.add_argument("--quote", default="", help="Quoted text from document")
    ac_parser.add_argument("--is-whole", action="store_true", help="Comment on whole document")

    # add-member
    am_parser = subparsers.add_parser("add-member", help="Add a collaborator to a document")
    am_parser.add_argument("--doc", required=True, help="Document ID")
    am_parser.add_argument("--open-id", required=True, help="User open_id (ou_xxx)")
    am_parser.add_argument("--perm", choices=["full_access", "edit", "view"],
                           default="full_access", help="Permission level (default: full_access)")

    # patch-block (局部编辑 — 原地更新 block 内容)
    pb_parser = subparsers.add_parser("patch-block",
                                       help="Update a block's text content in-place")
    pb_parser.add_argument("--doc", required=True, help="Document ID")
    pb_parser.add_argument("--block", required=True, help="Block ID to update")
    pb_parser.add_argument("--text", required=True, help="New text content (supports inline markdown)")

    # delete-blocks (局部编辑 — 删除指定范围的 block)
    db_parser = subparsers.add_parser("delete-blocks",
                                       help="Delete a range of child blocks")
    db_parser.add_argument("--doc", required=True, help="Document ID")
    db_parser.add_argument("--start", required=True, type=int, help="Start index (inclusive)")
    db_parser.add_argument("--end", required=True, type=int, help="End index (exclusive)")
    db_parser.add_argument("--parent", help="Parent block ID (default: page block = doc_id)")

    # insert-blocks (局部编辑 — 在指定位置插入内容)
    ib_parser = subparsers.add_parser("insert-blocks",
                                       help="Insert markdown content at a specific index")
    ib_parser.add_argument("--doc", required=True, help="Document ID")
    ib_parser.add_argument("--index", required=True, type=int, help="Insert position (0-based)")
    ib_parser.add_argument("--markdown", help="Markdown content string")
    ib_parser.add_argument("--markdown-file", help="Path to Markdown file")
    ib_parser.add_argument("--parent", help="Parent block ID (default: page block = doc_id)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Dispatch to command handler
    commands = {
        "create": cmd_create,
        "write": cmd_write,
        "read": cmd_read,
        "create-and-write": cmd_create_and_write,
        "list-comments": cmd_list_comments,
        "reply-comment": cmd_reply_comment,
        "resolve-comment": cmd_resolve_comment,
        "add-comment": cmd_add_comment,
        "add-member": cmd_add_member,
        "patch-block": cmd_patch_block,
        "delete-blocks": cmd_delete_blocks,
        "insert-blocks": cmd_insert_blocks,
    }

    handler = commands.get(args.command)
    if handler:
        sys.exit(handler(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
