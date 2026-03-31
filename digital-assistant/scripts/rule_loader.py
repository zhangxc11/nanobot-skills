#!/usr/bin/env python3
"""
rule_loader.py — Worker 执行规则的按需加载模块

从 skills/digital-assistant/rules/ 下的 Markdown 文件加载规则，
根据任务上下文（项目、模板）选择适用的规则，并按层级渲染。

渲染策略：
  - L0 (MUST): 注入完整 detail（全文）
  - L1 (REQUIRED): 注入 summary + 第一行 detail
  - L2 (RECOMMENDED): 只注入 summary（标题行）
"""

import re
from pathlib import Path

# ──────────────────────────────────────────
# Constants
# ──────────────────────────────────────────

RULES_DIR = Path(__file__).resolve().parent.parent / "rules"

# Template → rule file mapping (template-based rules)
TEMPLATE_RULE_FILES = {
    "standard-dev": "standard-dev.md",
    "long-task": "standard-dev.md",
    "batch-dev": "batch-dev.md",
}

# Project rule files (keyword-detected)
PROJECT_RULE_FILES = ["nanobot.md", "web-chat.md"]

# ──────────────────────────────────────────
# Keyword cache (parsed from HTML comments)
# ──────────────────────────────────────────

_PROJECT_KEYWORDS: dict[str, list[str]] = {}
_KEYWORDS_LOADED = False


def _load_keywords():
    """Parse detection_keywords from HTML comments in rule files. Cached."""
    global _PROJECT_KEYWORDS, _KEYWORDS_LOADED
    if _KEYWORDS_LOADED:
        return

    for filename in PROJECT_RULE_FILES:
        filepath = RULES_DIR / filename
        if not filepath.exists():
            continue
        text = filepath.read_text(encoding="utf-8")
        match = re.search(r"<!--\s*detection_keywords:\s*(.+?)\s*-->", text)
        if match:
            keywords = [kw.strip().lower() for kw in match.group(1).split(",") if kw.strip()]
            _PROJECT_KEYWORDS[filename] = keywords

    _KEYWORDS_LOADED = True


def _reset_keywords_cache():
    """Reset keyword cache (for testing)."""
    global _PROJECT_KEYWORDS, _KEYWORDS_LOADED
    _PROJECT_KEYWORDS = {}
    _KEYWORDS_LOADED = False


# ──────────────────────────────────────────
# Project detection
# ──────────────────────────────────────────

def detect_projects(task: dict) -> list[str]:
    """Detect which project rule files apply to this task.

    Scans task title, description, and id for detection_keywords.

    Returns:
        List of matching rule filenames (e.g., ["nanobot.md"])
    """
    _load_keywords()

    # Build searchable text from task
    parts = [
        task.get("title", "") or "",
        task.get("description", "") or "",
        task.get("id", "") or "",
    ]
    search_text = " ".join(parts).lower()

    matched = []
    for filename, keywords in _PROJECT_KEYWORDS.items():
        if any(kw in search_text for kw in keywords):
            matched.append(filename)

    return matched


# ──────────────────────────────────────────
# Rule parsing
# ──────────────────────────────────────────

def _parse_rule_file(filepath: Path) -> list[dict]:
    """Parse a rule Markdown file into structured sections.

    Returns list of dicts:
        [{"level": "L0"|"L1"|"L2", "id": "G-001", "title": "...", "body": "..."}]
    """
    if not filepath.exists():
        return []

    text = filepath.read_text(encoding="utf-8")
    rules = []
    current_level = None

    # Detect level sections
    for line in text.split("\n"):
        # Level headers: ## 🔴 L0 MUST / ## 🟡 L1 REQUIRED / ## 🟢 L2 RECOMMENDED
        if line.startswith("## "):
            if "L0" in line or "MUST" in line:
                current_level = "L0"
            elif "L1" in line or "REQUIRED" in line:
                current_level = "L1"
            elif "L2" in line or "RECOMMENDED" in line:
                current_level = "L2"
            continue

        # Rule headers: ### RULE-ID: Title
        match = re.match(r"^### ([A-Z]+-\d+):\s*(.+)", line)
        if match and current_level:
            rules.append({
                "level": current_level,
                "id": match.group(1),
                "title": match.group(2).strip(),
                "body": "",
            })
            continue

        # Body lines (append to current rule)
        if rules and current_level:
            rules[-1]["body"] += line + "\n"

    # Clean up trailing whitespace in body
    for rule in rules:
        rule["body"] = rule["body"].strip()

    return rules


# ──────────────────────────────────────────
# Rendering
# ──────────────────────────────────────────

def _render_rules(rules: list[dict]) -> str:
    """Render rules into worker-readable text.

    Rendering strategy by level:
      - L0: Full text (id + title + complete body)
      - L1: Summary + first line of body
      - L2: Summary only (id + title)
    """
    if not rules:
        return ""

    lines = []

    # Group by level
    by_level = {"L0": [], "L1": [], "L2": []}
    for rule in rules:
        level = rule.get("level", "L2")
        by_level.setdefault(level, []).append(rule)

    # L0 — full detail
    if by_level["L0"]:
        lines.append("#### 🔴 MUST（违反即失败）")
        for r in by_level["L0"]:
            lines.append(f"- **{r['id']}: {r['title']}**")
            if r["body"]:
                lines.append(f"  {r['body']}")
        lines.append("")

    # L1 — summary + first line
    if by_level["L1"]:
        lines.append("#### 🟡 REQUIRED（项目要求）")
        for r in by_level["L1"]:
            first_line = r["body"].split("\n")[0].strip() if r["body"] else ""
            if first_line:
                lines.append(f"- **{r['id']}: {r['title']}** — {first_line}")
            else:
                lines.append(f"- **{r['id']}: {r['title']}**")
        lines.append("")

    # L2 — summary only
    if by_level["L2"]:
        lines.append("#### 🟢 RECOMMENDED（最佳实践）")
        for r in by_level["L2"]:
            lines.append(f"- {r['id']}: {r['title']}")
        lines.append("")

    return "\n".join(lines)


# ──────────────────────────────────────────
# Main interface
# ──────────────────────────────────────────

def collect_rules(task: dict) -> str:
    """Main entry: collect and render applicable rules for a task.

    1. Always load global.md (L0 rules)
    2. Detect projects by keywords, load project rules
    3. Load template-specific rules
    4. Render all rules by level

    Args:
        task: Task dict with id, title, description, template, etc.

    Returns:
        Rendered rule text for injection into worker prompt.
        Empty string if no rules found.
    """
    all_rules = []

    # 1. Global rules (always loaded)
    global_rules = _parse_rule_file(RULES_DIR / "global.md")
    all_rules.extend(global_rules)

    # 2. Project-specific rules (keyword detection)
    matched_projects = detect_projects(task)
    for filename in matched_projects:
        project_rules = _parse_rule_file(RULES_DIR / filename)
        all_rules.extend(project_rules)

    # 3. Template-specific rules
    template = task.get("template",
                task.get("workgroup", {}).get("template", ""))
    rule_file = TEMPLATE_RULE_FILES.get(template)
    if rule_file:
        # Avoid duplicate loading if already loaded as project rule
        if rule_file not in matched_projects:
            template_rules = _parse_rule_file(RULES_DIR / rule_file)
            all_rules.extend(template_rules)

    if not all_rules:
        return ""

    # Deduplicate by rule ID (keep first occurrence)
    seen_ids = set()
    deduped = []
    for rule in all_rules:
        if rule["id"] not in seen_ids:
            seen_ids.add(rule["id"])
            deduped.append(rule)

    rendered = _render_rules(deduped)
    if not rendered.strip():
        return ""

    return f"### 执行规则\n\n{rendered}"
