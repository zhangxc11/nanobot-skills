#!/usr/bin/env python3
"""
cross_check_auditor.py — Layer 2 Independent Audit Process

Runs independently of the scheduler (triggered by system crontab, NOT scheduler prompt).
Scans decisions.jsonl + filesystem for compliance issues.
Sends alerts directly via feishu API (not through scheduler).

Usage:
    python3 cross_check_auditor.py scan          # Full scan, JSON report to stdout
    python3 cross_check_auditor.py report         # Human-readable report
    python3 cross_check_auditor.py alert          # HIGH severity only + feishu alert
    python3 cross_check_auditor.py install-cron   # Print crontab entry for installation

Independence guarantees (MF-1):
  - Triggered by system crontab, not scheduler prompt
  - Reads decisions.jsonl but also scans filesystem directly (git log, file existence)
  - Sends feishu alerts directly, not via scheduler
"""

import json
import sys
import os
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

# Resolve paths independently of scheduler
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
WORKSPACE = Path(os.environ.get("NANOBOT_WORKSPACE",
    SKILL_DIR.parent.parent))  # ~/.nanobot/workspace
BRAIN_DIR = WORKSPACE / "data" / "brain"
DECISIONS_LOG = BRAIN_DIR / "decisions.jsonl"
REPORTS_DIR = BRAIN_DIR / "reports"
TASKS_DIR = BRAIN_DIR / "tasks"


# ──────────────────────────────────────────
# Data loading (independent of scheduler)
# ──────────────────────────────────────────

def load_all_tasks() -> list[dict]:
    """Load all tasks from YAML files. Independent of brain_manager decision logic."""
    tasks = []
    if not TASKS_DIR.exists():
        return tasks
    try:
        import yaml
    except ImportError:
        # Fallback: try to parse basic fields from YAML manually
        return tasks
    for yf in TASKS_DIR.glob("*.yaml"):
        try:
            with open(yf, "r", encoding="utf-8") as f:
                task = yaml.safe_load(f)
                if task and isinstance(task, dict):
                    tasks.append(task)
        except Exception:
            pass
    return tasks


def load_decisions(days: int = 7) -> list[dict]:
    """Load recent decisions from decisions.jsonl."""
    decisions = []
    if not DECISIONS_LOG.exists():
        return decisions
    cutoff = datetime.now() - timedelta(days=days)
    try:
        with open(DECISIONS_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    ts = d.get("timestamp", "")
                    if ts and ts >= cutoff.isoformat():
                        decisions.append(d)
                    elif not ts:
                        decisions.append(d)
                except json.JSONDecodeError:
                    pass
    except Exception:
        pass
    return decisions


# ──────────────────────────────────────────
# Audit Probes
# ──────────────────────────────────────────

class AuditProbe:
    """Base class for audit probes."""
    name: str = ""

    def scan(self, tasks: list[dict], decisions: list[dict]) -> list[dict]:
        """Return list of issues: [{"task_id", "issue", "severity", "recommendation"}]"""
        raise NotImplementedError


class DesignGateProbe(AuditProbe):
    """Check standard-dev tasks have design documentation. Uses filesystem directly."""
    name = "design_gate"

    def scan(self, tasks, decisions):
        issues = []
        for t in tasks:
            if t.get("status") not in ("executing", "done"):
                continue
            tpl = t.get("template", t.get("workgroup", {}).get("template", ""))
            if tpl in ("quick", "cron-auto"):
                continue
            if t.get("emergency"):
                continue

            task_id = t.get("id", "")
            has_design = bool(t.get("design_ref") or t.get("design_doc"))

            # Independent check: scan for architect reports on filesystem
            if not has_design:
                arch_reports = list(REPORTS_DIR.glob(f"{task_id}-architect-*.json"))
                if arch_reports:
                    has_design = True

            # Check orchestration history
            if not has_design:
                history = t.get("orchestration", {}).get("history", [])
                has_design = any(h.get("role") == "architect" for h in history)

            if not has_design:
                issues.append({
                    "task_id": task_id,
                    "issue": "standard-dev task without design documentation",
                    "severity": "HIGH",
                    "recommendation": "Dispatch architect or add design_ref",
                })
        return issues


class DocTripletProbe(AuditProbe):
    """Check completed tasks have DEVLOG/ARCHITECTURE. Scans filesystem directly."""
    name = "doc_triplet"

    def scan(self, tasks, decisions):
        issues = []
        for t in tasks:
            if t.get("status") != "done":
                continue
            tpl = t.get("template", t.get("workgroup", {}).get("template", ""))
            if tpl in ("quick", "cron-auto"):
                continue
            if t.get("emergency"):
                if not t.get("doc_debt"):
                    issues.append({
                        "task_id": t.get("id", ""),
                        "issue": "emergency task completed without doc_debt marker",
                        "severity": "MEDIUM",
                        "recommendation": "Mark doc_debt=true",
                    })
                continue

            task_id = t.get("id", "")
            has_devlog = False
            has_design = bool(t.get("design_ref"))

            # Scan reports AND filesystem
            reports = list(REPORTS_DIR.glob(f"{task_id}-*.json"))
            project_dirs = set()
            for rp in reports:
                try:
                    data = json.loads(rp.read_text(encoding="utf-8"))
                    for f in data.get("files_changed", []):
                        fu = f.upper()
                        if "DEVLOG" in fu:
                            has_devlog = True
                        if "ARCHITECTURE" in fu or "REQUIREMENTS" in fu:
                            has_design = True
                        # Collect project dirs for filesystem scan
                        p = Path(f)
                        for parent in p.parents:
                            if (parent / ".git").exists():
                                project_dirs.add(parent)
                                break
                except Exception:
                    pass

            # Direct filesystem scan (independent of report data)
            for proj_dir in project_dirs:
                if not has_devlog:
                    for pat in ["DEVLOG.md", "docs/DEVLOG.md"]:
                        if (proj_dir / pat).exists():
                            has_devlog = True
                            break
                if not has_design:
                    for pat in ["ARCHITECTURE.md", "docs/ARCHITECTURE.md"]:
                        if (proj_dir / pat).exists():
                            has_design = True
                            break

            missing = []
            if not has_devlog:
                missing.append("DEVLOG.md")
            if not has_design:
                missing.append("ARCHITECTURE.md")
            if missing:
                issues.append({
                    "task_id": task_id,
                    "issue": f"Doc triplet incomplete: missing {', '.join(missing)}",
                    "severity": "HIGH",
                    "recommendation": "Create doc completion task",
                })
        return issues


class TestEvidenceProbe(AuditProbe):
    """Check tester reports have test_evidence field."""
    name = "test_evidence"

    def scan(self, tasks, decisions):
        issues = []
        for t in tasks:
            if t.get("status") != "done":
                continue
            tpl = t.get("template", t.get("workgroup", {}).get("template", ""))
            if tpl in ("quick", "cron-auto"):
                continue

            task_id = t.get("id", "")
            tester_reports = list(REPORTS_DIR.glob(f"{task_id}-tester-*.json"))
            for rp in tester_reports:
                try:
                    data = json.loads(rp.read_text(encoding="utf-8"))
                    if data.get("verdict") == "pass" and not data.get("test_evidence"):
                        issues.append({
                            "task_id": task_id,
                            "issue": f"Tester report {rp.name} passed without test_evidence",
                            "severity": "MEDIUM",
                            "recommendation": "Ensure tester provides evidence",
                        })
                except Exception:
                    pass
        return issues


class GitCommitProbe(AuditProbe):
    """Check git commits for Task ID in commit messages. Scans git log directly."""
    name = "git_commit"

    def scan(self, tasks, decisions):
        issues = []
        # Scan recent git log for commits without Task ID
        git_dirs_to_check = set()
        # Check the workspace itself and common project locations
        ws_git = WORKSPACE / ".git"
        if ws_git.exists():
            git_dirs_to_check.add(WORKSPACE)
        skill_git = SKILL_DIR / ".git"
        if skill_git.exists():
            git_dirs_to_check.add(SKILL_DIR)

        for git_dir in git_dirs_to_check:
            try:
                result = subprocess.run(
                    ["git", "log", "--oneline", "--since=7 days ago", "--format=%s"],
                    cwd=str(git_dir), capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0:
                    for line in result.stdout.strip().split("\n"):
                        if not line:
                            continue
                        # Skip merge/revert commits
                        if line.startswith(("Merge ", "Revert ")):
                            continue
                        # Check for Task ID pattern
                        import re
                        if not re.search(r"T-\d{8}-\d{1,3}", line):
                            issues.append({
                                "task_id": "SYSTEM",
                                "issue": f"Commit without Task ID: '{line[:60]}'",
                                "severity": "MEDIUM",
                                "recommendation": "Use format: feat(T-YYYYMMDD-NNN): description",
                            })
            except Exception:
                pass
        return issues


class L0ApprovalProbe(AuditProbe):
    """Check decisions.jsonl for suspicious auto-approvals."""
    name = "l0_approval"

    def scan(self, tasks, decisions):
        issues = []
        for d in decisions:
            dtype = d.get("type", "")
            if dtype == "status_change" and d.get("new_status") == "done":
                task_id = d.get("task_id", "")
                note = d.get("note", "")
                # Find the task to check template
                task = next((t for t in tasks if t.get("id") == task_id), None)
                if task:
                    tpl = task.get("template", task.get("workgroup", {}).get("template", ""))
                    if tpl not in ("quick", "cron-auto"):
                        # Check if there's a tester report
                        tester_reports = list(REPORTS_DIR.glob(f"{task_id}-tester-*.json"))
                        if not tester_reports:
                            issues.append({
                                "task_id": task_id,
                                "issue": "Task marked done without tester report",
                                "severity": "HIGH",
                                "recommendation": "Investigate if tester was skipped",
                            })
        return issues


# All probes
ALL_PROBES = [
    DesignGateProbe(),
    DocTripletProbe(),
    TestEvidenceProbe(),
    GitCommitProbe(),
    L0ApprovalProbe(),
]


# ──────────────────────────────────────────
# Audit execution
# ──────────────────────────────────────────

def run_audit(tasks: list[dict] = None, decisions: list[dict] = None) -> dict:
    """Run all probes and return audit report."""
    if tasks is None:
        tasks = load_all_tasks()
    if decisions is None:
        decisions = load_decisions()

    all_issues = []
    for probe in ALL_PROBES:
        try:
            probe_issues = probe.scan(tasks, decisions)
            for issue in probe_issues:
                issue["probe"] = probe.name
            all_issues.extend(probe_issues)
        except Exception as e:
            all_issues.append({
                "task_id": "SYSTEM",
                "issue": f"Probe {probe.name} failed: {e}",
                "severity": "MEDIUM",
                "probe": probe.name,
                "recommendation": "Fix probe logic",
            })

    high = [i for i in all_issues if i.get("severity") == "HIGH"]
    medium = [i for i in all_issues if i.get("severity") == "MEDIUM"]
    total_tasks = len([t for t in tasks if t.get("status") == "done"])

    return {
        "timestamp": datetime.now().isoformat(),
        "total_tasks_scanned": len(tasks),
        "done_tasks_scanned": total_tasks,
        "total_issues": len(all_issues),
        "high": len(high),
        "medium": len(medium),
        "issues": all_issues,
        "compliance_rate": round(1.0 - len(high) / max(total_tasks, 1), 3),
    }


def format_report(audit: dict) -> str:
    """Format audit report as human-readable text."""
    lines = [
        f"# Cross-Check Audit Report",
        f"**Time**: {audit['timestamp']}",
        f"**Tasks scanned**: {audit['total_tasks_scanned']} (done: {audit['done_tasks_scanned']})",
        f"**Issues**: {audit['total_issues']} (HIGH: {audit['high']}, MEDIUM: {audit['medium']})",
        f"**Compliance rate**: {audit['compliance_rate']:.1%}",
        "",
    ]
    if audit["issues"]:
        lines.append("## Issues")
        for i, issue in enumerate(audit["issues"], 1):
            sev = issue.get("severity", "?")
            marker = "🔴" if sev == "HIGH" else "🟡"
            lines.append(f"{i}. {marker} [{sev}] [{issue.get('probe', '?')}] "
                         f"{issue.get('task_id', '?')}: {issue.get('issue', '')}")
            lines.append(f"   → {issue.get('recommendation', '')}")
    else:
        lines.append("✅ No issues found.")
    return "\n".join(lines)


def send_feishu_alert(audit: dict):
    """Send HIGH severity issues directly via feishu API (independent of scheduler)."""
    high_issues = [i for i in audit["issues"] if i.get("severity") == "HIGH"]
    if not high_issues:
        return

    text = f"🔴 Cross-Check Audit Alert\n\n"
    text += f"发现 {len(high_issues)} 个高风险问题:\n\n"
    for i, issue in enumerate(high_issues, 1):
        text += f"{i}. [{issue.get('probe')}] {issue.get('task_id')}: {issue.get('issue')}\n"
        text += f"   建议: {issue.get('recommendation')}\n"

    # Try to send via feishu_notify.py (direct call, not via scheduler)
    try:
        feishu_script = SCRIPT_DIR / "feishu_notify.py"
        if feishu_script.exists():
            subprocess.run(
                [sys.executable, str(feishu_script), "send-text", text],
                timeout=30, capture_output=True
            )
    except Exception:
        # Fallback: just print to stderr
        print(f"[audit-alert] Failed to send feishu alert, printing to stderr:", file=sys.stderr)
        print(text, file=sys.stderr)


def print_cron_entry():
    """Print crontab entry for installation."""
    script_path = Path(__file__).resolve()
    python = sys.executable
    log_dir = WORKSPACE / "data" / "brain" / "audit_logs"
    print(f"# Cross-check auditor — runs daily at 23:30 (independent of scheduler)")
    print(f"# Install: crontab -e and add this line")
    print(f"30 23 * * * {python} {script_path} alert >> {log_dir}/audit.log 2>&1")
    print(f"# To create log dir: mkdir -p {log_dir}")


# ──────────────────────────────────────────
# CLI
# ──────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: cross_check_auditor.py <scan|report|alert|install-cron>")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "scan":
        audit = run_audit()
        print(json.dumps(audit, ensure_ascii=False, indent=2))
    elif cmd == "report":
        audit = run_audit()
        print(format_report(audit))
    elif cmd == "alert":
        audit = run_audit()
        print(format_report(audit))
        send_feishu_alert(audit)
        # Save report to audit_logs
        log_dir = BRAIN_DIR / "audit_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"audit-{datetime.now().strftime('%Y%m%d')}.json"
        with open(log_file, "w", encoding="utf-8") as f:
            json.dump(audit, f, ensure_ascii=False, indent=2)
    elif cmd == "install-cron":
        print_cron_entry()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
