"""Tests for cross_check_auditor.py — Layer 2 Independent Audit Process."""

import json
import sys
import os
import pytest
from pathlib import Path
from unittest.mock import patch

# Add scripts dir to path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cross_check_auditor as auditor


class TestDesignGateProbe:
    def test_skips_quick_tasks(self):
        probe = auditor.DesignGateProbe()
        tasks = [{"id": "T-001", "status": "done", "template": "quick"}]
        assert probe.scan(tasks, []) == []

    def test_skips_emergency(self):
        probe = auditor.DesignGateProbe()
        tasks = [{"id": "T-001", "status": "done", "template": "standard-dev", "emergency": True}]
        assert probe.scan(tasks, []) == []

    def test_detects_missing_design(self, tmp_path, monkeypatch):
        monkeypatch.setattr(auditor, "REPORTS_DIR", tmp_path)
        probe = auditor.DesignGateProbe()
        tasks = [{"id": "T-001", "status": "executing",
                  "workgroup": {"template": "standard-dev"},
                  "orchestration": {"history": []}}]
        issues = probe.scan(tasks, [])
        assert len(issues) == 1
        assert issues[0]["severity"] == "HIGH"

    def test_passes_with_design_ref(self):
        probe = auditor.DesignGateProbe()
        tasks = [{"id": "T-001", "status": "done", "template": "standard-dev",
                  "design_ref": "D-001"}]
        assert probe.scan(tasks, []) == []

    def test_passes_with_architect_history(self, tmp_path, monkeypatch):
        monkeypatch.setattr(auditor, "REPORTS_DIR", tmp_path)
        probe = auditor.DesignGateProbe()
        tasks = [{"id": "T-001", "status": "done", "template": "standard-dev",
                  "orchestration": {"history": [{"role": "architect"}]}}]
        assert probe.scan(tasks, []) == []


class TestDocTripletProbe:
    def test_skips_quick(self):
        probe = auditor.DocTripletProbe()
        tasks = [{"id": "T-001", "status": "done", "template": "quick"}]
        assert probe.scan(tasks, []) == []

    def test_emergency_without_doc_debt(self):
        probe = auditor.DocTripletProbe()
        tasks = [{"id": "T-001", "status": "done", "template": "standard-dev", "emergency": True}]
        issues = probe.scan(tasks, [])
        assert len(issues) == 1
        assert issues[0]["severity"] == "MEDIUM"

    def test_emergency_with_doc_debt_ok(self):
        probe = auditor.DocTripletProbe()
        tasks = [{"id": "T-001", "status": "done", "template": "standard-dev",
                  "emergency": True, "doc_debt": True}]
        assert probe.scan(tasks, []) == []


class TestTestEvidenceProbe:
    def test_detects_missing_evidence(self, tmp_path, monkeypatch):
        monkeypatch.setattr(auditor, "REPORTS_DIR", tmp_path)
        report_file = tmp_path / "T-001-tester-123.json"
        report_file.write_text(json.dumps({"verdict": "pass", "summary": "ok"}))
        probe = auditor.TestEvidenceProbe()
        tasks = [{"id": "T-001", "status": "done", "template": "standard-dev"}]
        issues = probe.scan(tasks, [])
        assert len(issues) == 1
        assert "test_evidence" in issues[0]["issue"]

    def test_passes_with_evidence(self, tmp_path, monkeypatch):
        monkeypatch.setattr(auditor, "REPORTS_DIR", tmp_path)
        report_file = tmp_path / "T-001-tester-123.json"
        report_file.write_text(json.dumps({
            "verdict": "pass", "test_evidence": [{"type": "cmd", "result": "ok"}]
        }))
        probe = auditor.TestEvidenceProbe()
        tasks = [{"id": "T-001", "status": "done", "template": "standard-dev"}]
        assert probe.scan(tasks, []) == []


class TestRunAudit:
    def test_returns_report_structure(self):
        report = auditor.run_audit(tasks=[], decisions=[])
        assert "timestamp" in report
        assert "total_issues" in report
        assert "compliance_rate" in report
        assert report["total_issues"] == 0

    def test_aggregates_issues(self, tmp_path, monkeypatch):
        monkeypatch.setattr(auditor, "REPORTS_DIR", tmp_path)
        tasks = [{"id": "T-001", "status": "executing",
                  "workgroup": {"template": "standard-dev"},
                  "orchestration": {"history": []}}]
        report = auditor.run_audit(tasks=tasks, decisions=[])
        assert report["total_issues"] >= 1


class TestFormatReport:
    def test_format_no_issues(self):
        audit = {"timestamp": "2026-04-01", "total_tasks_scanned": 0,
                 "done_tasks_scanned": 0, "total_issues": 0,
                 "high": 0, "medium": 0, "issues": [], "compliance_rate": 1.0}
        text = auditor.format_report(audit)
        assert "No issues" in text

    def test_format_with_issues(self):
        audit = {"timestamp": "2026-04-01", "total_tasks_scanned": 1,
                 "done_tasks_scanned": 1, "total_issues": 1,
                 "high": 1, "medium": 0, "compliance_rate": 0.0,
                 "issues": [{"severity": "HIGH", "probe": "test", "task_id": "T-001",
                             "issue": "problem", "recommendation": "fix it"}]}
        text = auditor.format_report(audit)
        assert "🔴" in text
        assert "T-001" in text


class TestCLI:
    def test_scan_command(self, monkeypatch, capsys):
        monkeypatch.setattr(auditor, "load_all_tasks", lambda: [])
        monkeypatch.setattr(auditor, "load_decisions", lambda days=7: [])
        monkeypatch.setattr(sys, "argv", ["auditor", "scan"])
        auditor.main()
        output = capsys.readouterr().out
        data = json.loads(output)
        assert "total_issues" in data

    def test_install_cron_command(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["auditor", "install-cron"])
        auditor.main()
        output = capsys.readouterr().out
        assert "crontab" in output.lower()
        assert "cross_check_auditor" in output


# Tests for new scheduler features (CROSS_CHECK_ENABLED, TEST_EVIDENCE_CHECK_ENABLED)
class TestSchedulerCrossCheckFlags:
    def test_cross_check_master_flag(self, monkeypatch):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import scheduler as sched
        # When CROSS_CHECK_ENABLED=0, test_evidence check should be bypassed
        monkeypatch.setattr(sched, "CROSS_CHECK_ENABLED", False)
        report = {"task_id": "T-001", "role": "tester", "verdict": "pass",
                  "summary": "ok"}  # No test_evidence
        task = {"id": "T-001", "priority": "P1",
                "workgroup": {"template": "standard-dev"},
                "orchestration": {"iteration": 1, "history": []}}
        decision = sched.make_decision(report, task)
        # Should NOT be sent back for evidence (flag disabled)
        assert decision.action in ("promote_to_review", "mark_done")
        monkeypatch.setattr(sched, "CROSS_CHECK_ENABLED", True)

    def test_test_evidence_flag_disabled(self, monkeypatch):
        import scheduler as sched
        monkeypatch.setattr(sched, "TEST_EVIDENCE_CHECK_ENABLED", False)
        report = {"task_id": "T-001", "role": "tester", "verdict": "pass",
                  "summary": "ok"}
        task = {"id": "T-001", "priority": "P1",
                "workgroup": {"template": "standard-dev"},
                "orchestration": {"iteration": 1, "history": []}}
        decision = sched.make_decision(report, task)
        assert decision.action in ("promote_to_review", "mark_done")
        monkeypatch.setattr(sched, "TEST_EVIDENCE_CHECK_ENABLED", True)

    def test_tester_pass_no_evidence_sends_back(self):
        import scheduler as sched
        report = {"task_id": "T-001", "role": "tester", "verdict": "pass",
                  "summary": "ok"}  # No test_evidence
        task = {"id": "T-001", "priority": "P1",
                "workgroup": {"template": "standard-dev"},
                "orchestration": {"iteration": 1, "history": []}}
        decision = sched.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert "test_evidence" in decision.reason

    def test_tester_pass_with_evidence_proceeds(self):
        import scheduler as sched
        report = {"task_id": "T-001", "role": "tester", "verdict": "pass",
                  "summary": "ok",
                  "test_evidence": [{"type": "cmd", "command": "pytest", "result": "ok"}]}
        task = {"id": "T-001", "priority": "P1",
                "workgroup": {"template": "standard-dev"},
                "orchestration": {"iteration": 1, "history": []}}
        decision = sched.make_decision(report, task)
        assert decision.action in ("promote_to_review", "mark_done")

    def test_tester_evidence_retry_escalation(self):
        import scheduler as sched
        report = {"task_id": "T-001", "role": "tester", "verdict": "pass",
                  "summary": "ok"}
        task = {"id": "T-001", "priority": "P1",
                "workgroup": {"template": "standard-dev"},
                "orchestration": {"iteration": 3, "history": [
                    {"role": "developer", "verdict": "pass", "reason": "dev done"},
                    {"role": "tester", "reason": "tester passed but no test_evidence (retry 1/2)"},
                    {"role": "tester", "reason": "tester passed but no test_evidence (retry 2/2)"},
                ]}}
        decision = sched.make_decision(report, task)
        # After max retries, should escalate to review (not send back again)
        assert decision.action == "promote_to_review"

    def test_quick_template_skips_evidence_check(self):
        import scheduler as sched
        report = {"task_id": "T-001", "role": "tester", "verdict": "pass",
                  "summary": "ok"}  # No test_evidence, but quick template
        task = {"id": "T-001", "priority": "P2",
                "workgroup": {"template": "quick"},
                "orchestration": {"iteration": 1, "history": []}}
        decision = sched.make_decision(report, task)
        assert decision.action == "mark_done"

    def test_count_evidence_retries(self):
        import scheduler as sched
        task = {"orchestration": {"history": [
            {"role": "tester", "reason": "tester passed but no test_evidence (retry 1/2)"},
            {"role": "developer", "reason": "something else"},
            {"role": "tester", "reason": "missing test_evidence again"},
        ]}}
        assert sched._count_evidence_retries(task) == 2

    def test_tester_guidance_includes_evidence(self):
        import scheduler as sched
        task = {"id": "T-001"}
        guidance = sched._generate_tester_guidance(task)
        assert "test_evidence" in guidance
        assert "MUST" in guidance
