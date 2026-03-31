#!/usr/bin/env python3
"""
Tests for rule_loader.py — Worker execution rule loading module.

Tests cover:
  - Keyword parsing from HTML comments
  - Project detection from task metadata
  - Rule file parsing (L0/L1/L2 sections)
  - Rule rendering by level
  - collect_rules() integration
"""

import pytest
from pathlib import Path
from unittest.mock import patch

import rule_loader


# ──────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_keywords_cache():
    """Reset keyword cache before each test."""
    rule_loader._reset_keywords_cache()
    yield
    rule_loader._reset_keywords_cache()


# ──────────────────────────────────────────
# Test: Keyword loading
# ──────────────────────────────────────────

class TestKeywordLoading:
    def test_keywords_loaded_from_real_files(self):
        """Keywords should be parsed from actual rule files."""
        rule_loader._load_keywords()
        assert len(rule_loader._PROJECT_KEYWORDS) > 0
        # nanobot.md should have keywords
        assert "nanobot.md" in rule_loader._PROJECT_KEYWORDS
        assert "nanobot" in rule_loader._PROJECT_KEYWORDS["nanobot.md"]

    def test_web_chat_keywords(self):
        """web-chat.md should have frontend-related keywords."""
        rule_loader._load_keywords()
        assert "web-chat.md" in rule_loader._PROJECT_KEYWORDS
        assert "web-chat" in rule_loader._PROJECT_KEYWORDS["web-chat.md"]

    def test_keywords_cached(self):
        """Keywords should only be loaded once (cached)."""
        rule_loader._load_keywords()
        first = dict(rule_loader._PROJECT_KEYWORDS)
        rule_loader._load_keywords()  # Should use cache
        assert rule_loader._PROJECT_KEYWORDS == first

    def test_reset_cache(self):
        """_reset_keywords_cache should clear the cache."""
        rule_loader._load_keywords()
        assert rule_loader._KEYWORDS_LOADED is True
        rule_loader._reset_keywords_cache()
        assert rule_loader._KEYWORDS_LOADED is False
        assert rule_loader._PROJECT_KEYWORDS == {}


# ──────────────────────────────────────────
# Test: Project detection
# ──────────────────────────────────────────

class TestDetectProjects:
    def test_nanobot_task_detected(self):
        """Task mentioning nanobot should match nanobot.md."""
        task = {"title": "Fix scheduler bug", "description": "修复 nanobot scheduler 的 bug"}
        result = rule_loader.detect_projects(task)
        assert "nanobot.md" in result

    def test_webchat_task_detected(self):
        """Task mentioning web-chat should match web-chat.md."""
        task = {"title": "web-chat UI fix", "description": "修复前端样式"}
        result = rule_loader.detect_projects(task)
        assert "web-chat.md" in result

    def test_generic_task_no_match(self):
        """Generic task should not match any project."""
        task = {"title": "Write documentation", "description": "更新用户手册"}
        result = rule_loader.detect_projects(task)
        assert result == []

    def test_task_id_also_searched(self):
        """Task ID should also be searched for keywords."""
        task = {"id": "T-nanobot-001", "title": "Fix bug", "description": ""}
        result = rule_loader.detect_projects(task)
        assert "nanobot.md" in result

    def test_case_insensitive(self):
        """Detection should be case-insensitive."""
        task = {"title": "NANOBOT Gateway Fix", "description": ""}
        result = rule_loader.detect_projects(task)
        assert "nanobot.md" in result

    def test_multiple_projects_detected(self):
        """Task mentioning multiple projects should match all."""
        task = {"title": "nanobot web-chat integration", "description": ""}
        result = rule_loader.detect_projects(task)
        assert "nanobot.md" in result
        assert "web-chat.md" in result


# ──────────────────────────────────────────
# Test: Rule file parsing
# ──────────────────────────────────────────

class TestParseRuleFile:
    def test_parse_global_rules(self):
        """global.md should have 4 L0 rules."""
        filepath = rule_loader.RULES_DIR / "global.md"
        rules = rule_loader._parse_rule_file(filepath)
        assert len(rules) == 4
        assert all(r["level"] == "L0" for r in rules)
        ids = [r["id"] for r in rules]
        assert "G-001" in ids
        assert "G-002" in ids
        assert "G-003" in ids
        assert "G-004" in ids

    def test_parse_nanobot_rules(self):
        """nanobot.md should have L0 and L1 rules."""
        filepath = rule_loader.RULES_DIR / "nanobot.md"
        rules = rule_loader._parse_rule_file(filepath)
        assert len(rules) == 4
        l0 = [r for r in rules if r["level"] == "L0"]
        l1 = [r for r in rules if r["level"] == "L1"]
        assert len(l0) == 2
        assert len(l1) == 2

    def test_parse_standard_dev_rules(self):
        """standard-dev.md should have L1 and L2 rules."""
        filepath = rule_loader.RULES_DIR / "standard-dev.md"
        rules = rule_loader._parse_rule_file(filepath)
        assert len(rules) >= 8
        l1 = [r for r in rules if r["level"] == "L1"]
        l2 = [r for r in rules if r["level"] == "L2"]
        assert len(l1) >= 1
        assert len(l2) >= 7

    def test_parse_batch_dev_rules(self):
        """batch-dev.md should have L1 rules."""
        filepath = rule_loader.RULES_DIR / "batch-dev.md"
        rules = rule_loader._parse_rule_file(filepath)
        assert len(rules) == 3
        assert all(r["level"] == "L1" for r in rules)

    def test_parse_webchat_rules(self):
        """web-chat.md should have L1 rules."""
        filepath = rule_loader.RULES_DIR / "web-chat.md"
        rules = rule_loader._parse_rule_file(filepath)
        assert len(rules) == 2
        assert all(r["level"] == "L1" for r in rules)

    def test_parse_nonexistent_file(self):
        """Non-existent file should return empty list."""
        filepath = rule_loader.RULES_DIR / "nonexistent.md"
        rules = rule_loader._parse_rule_file(filepath)
        assert rules == []

    def test_rule_body_not_empty(self):
        """Rules should have non-empty body text."""
        filepath = rule_loader.RULES_DIR / "global.md"
        rules = rule_loader._parse_rule_file(filepath)
        for rule in rules:
            assert rule["body"].strip(), f"Rule {rule['id']} has empty body"

    def test_rule_title_not_empty(self):
        """Rules should have non-empty titles."""
        filepath = rule_loader.RULES_DIR / "global.md"
        rules = rule_loader._parse_rule_file(filepath)
        for rule in rules:
            assert rule["title"].strip(), f"Rule {rule['id']} has empty title"


# ──────────────────────────────────────────
# Test: Rule rendering
# ──────────────────────────────────────────

class TestRenderRules:
    def test_render_l0_full_text(self):
        """L0 rules should include full body text."""
        rules = [{"level": "L0", "id": "G-001", "title": "Test Rule", "body": "Full detail here.\nMore detail."}]
        result = rule_loader._render_rules(rules)
        assert "MUST" in result
        assert "G-001" in result
        assert "Full detail here." in result
        assert "More detail." in result

    def test_render_l1_summary_first_line(self):
        """L1 rules should include summary + first line of body."""
        rules = [{"level": "L1", "id": "NAV-001", "title": "Test L1", "body": "First line.\nSecond line."}]
        result = rule_loader._render_rules(rules)
        assert "REQUIRED" in result
        assert "NAV-001" in result
        assert "First line." in result
        assert "Second line." not in result

    def test_render_l2_summary_only(self):
        """L2 rules should only include id and title."""
        rules = [{"level": "L2", "id": "STD-001", "title": "Test L2", "body": "Detail not shown."}]
        result = rule_loader._render_rules(rules)
        assert "RECOMMENDED" in result
        assert "STD-001" in result
        assert "Test L2" in result
        assert "Detail not shown." not in result

    def test_render_empty_rules(self):
        """Empty rules list should return empty string."""
        result = rule_loader._render_rules([])
        assert result == ""

    def test_render_mixed_levels(self):
        """Mixed levels should be grouped correctly."""
        rules = [
            {"level": "L0", "id": "G-001", "title": "Must Rule", "body": "Must body"},
            {"level": "L1", "id": "N-001", "title": "Required Rule", "body": "Req body"},
            {"level": "L2", "id": "S-001", "title": "Recommended Rule", "body": "Rec body"},
        ]
        result = rule_loader._render_rules(rules)
        assert "MUST" in result
        assert "REQUIRED" in result
        assert "RECOMMENDED" in result
        # L0 body included
        assert "Must body" in result
        # L1 first line included
        assert "Req body" in result
        # L2 body NOT included
        assert "Rec body" not in result


# ──────────────────────────────────────────
# Test: collect_rules() integration
# ──────────────────────────────────────────

class TestCollectRules:
    def test_global_rules_always_included(self):
        """Global rules should be included for any task."""
        task = {"title": "Generic task", "description": "Nothing special"}
        result = rule_loader.collect_rules(task)
        assert "G-001" in result
        assert "G-002" in result
        assert "G-003" in result
        assert "G-004" in result

    def test_nanobot_task_includes_project_rules(self):
        """Nanobot task should include nanobot project rules."""
        task = {"title": "Fix nanobot scheduler", "description": "scheduler bug fix"}
        result = rule_loader.collect_rules(task)
        assert "NANO-001" in result
        assert "NANO-002" in result

    def test_standard_dev_includes_template_rules(self):
        """Standard-dev template should include standard dev rules."""
        task = {"title": "Some task", "description": "", "template": "standard-dev"}
        result = rule_loader.collect_rules(task)
        assert "STD-001" in result

    def test_batch_dev_includes_batch_rules(self):
        """Batch-dev template should include batch dev rules."""
        task = {"title": "Batch task", "description": "", "template": "batch-dev"}
        result = rule_loader.collect_rules(task)
        assert "BAT-001" in result

    def test_quick_task_only_global(self):
        """Quick task with no keywords should only have global rules."""
        task = {"title": "Quick question", "description": "answer this", "template": "quick"}
        result = rule_loader.collect_rules(task)
        assert "G-001" in result
        # Should NOT include template-specific rules (quick not in TEMPLATE_RULE_FILES)
        assert "STD-001" not in result
        assert "BAT-001" not in result

    def test_deduplication(self):
        """Rules should not be duplicated when matched by both project and template."""
        # nanobot.md has NANO-* rules, standard-dev.md has STD-* rules
        # No overlap expected, but let's verify dedup logic works
        task = {"title": "nanobot scheduler fix", "description": "", "template": "standard-dev"}
        result = rule_loader.collect_rules(task)
        # Count occurrences of G-001 (should appear exactly once)
        assert result.count("G-001:") == 1 or result.count("**G-001:") == 1

    def test_result_has_header(self):
        """Result should start with the rules header."""
        task = {"title": "Test", "description": ""}
        result = rule_loader.collect_rules(task)
        assert result.startswith("### 执行规则")

    def test_webchat_task_includes_web_rules(self):
        """Web-chat task should include web-chat rules."""
        task = {"title": "Fix web-chat UI", "description": "frontend issue"}
        result = rule_loader.collect_rules(task)
        assert "WEB-001" in result
        assert "WEB-002" in result

    def test_template_from_workgroup(self):
        """Template should be detected from workgroup.template fallback."""
        task = {"title": "Task", "description": "", "workgroup": {"template": "batch-dev"}}
        result = rule_loader.collect_rules(task)
        assert "BAT-001" in result

    def test_no_duplicate_loading_project_as_template(self):
        """If a rule file is loaded as project rule, don't load again as template rule."""
        # standard-dev.md has detection_keywords: standard-dev
        # If task has template=standard-dev AND mentions standard-dev in title,
        # rules should not be duplicated
        task = {"title": "standard-dev task", "description": "", "template": "standard-dev"}
        result = rule_loader.collect_rules(task)
        # STD-001 should appear only once
        count = result.count("STD-001")
        assert count == 1


# ──────────────────────────────────────────
# Test: Edge cases
# ──────────────────────────────────────────

class TestEdgeCases:
    def test_empty_task(self):
        """Empty task dict should still return global rules."""
        result = rule_loader.collect_rules({})
        assert "G-001" in result

    def test_task_with_none_values(self):
        """Task with None values should not crash."""
        task = {"title": None, "description": None, "template": None}
        # Should not raise - detect_projects handles None gracefully
        # title/description default to "" in detect_projects
        result = rule_loader.collect_rules(task)
        assert "G-001" in result
