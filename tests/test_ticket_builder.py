"""
Tests for ticket_builder module.

Validates template selection, assignee resolution, title formatting,
and label generation.
"""

import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ticket_builder import build_ticket, build_duplicate_comment
from src.models import (
    BuildInfo,
    ExtractedLinks,
    FailureContext,
    JobConfig,
    StageResult,
    StageStatus,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_settings(ramdump_template=None, no_ramdump_template=None):
    return {
        "jira": {
            "project_key": "TEST",
            "issue_type": "Bug",
            "default_priority": "High",
            "base_labels": ["auto-test"],
        },
        "jenkins": {"log_tail_lines": 50},
        "ticket_templates": {
            "with_ramdump": ramdump_template or "Ramdump: {ramdump_link}\nJob: {job_name}",
            "without_ramdump": no_ramdump_template or "Report: {report_link}\nCrashed: {crashed_tests}",
        },
    }


def _make_failure_context(
    ramdump=None,
    report=None,
    vm_link=None,
    crashed_tests=None,
    stage_name="Test Execution",
    job_name="test_job",
    build_number=42,
    category="sandbox",
):
    build_info = BuildInfo(
        job_name=job_name,
        build_number=build_number,
        url="https://jenkins.test.com/job/test",
        status="FAILURE",
        category=category,
        job_path=f"{category}/{job_name}",
    )
    failed_stage = StageResult(
        name=stage_name,
        stage_id="5",
        status=StageStatus.FAILED,
    )
    links = ExtractedLinks(
        ramdump=ramdump,
        report=report,
        vm_link=vm_link,
    )
    return FailureContext(
        build_info=build_info,
        failed_stage=failed_stage,
        links=links,
        crashed_tests=crashed_tests or [],
        log_tail="some log tail content",
    )


def _make_job_config(
    ramdump_required=False,
    default_poc="default@test.com",
    stage_poc_map=None,
):
    return JobConfig(
        category="sandbox",
        job_name="test_job",
        pattern="test_",
        all_stages=["Setup", "Build", "Test Execution", "Custom Stage"],
        default_stages=["Setup", "Build", "Test Execution"],
        job_stages=["Custom Stage"],
        stage_poc_map=stage_poc_map or {
            "Setup": "default@test.com",
            "Build": "build@test.com",
            "Test Execution": "default@test.com",
            "Custom Stage": "custom@test.com",
        },
        ramdump_required=ramdump_required,
        default_poc=default_poc,
    )


# ─── Tests ───────────────────────────────────────────────────────────────────

class TestBuildTicket:
    def test_ramdump_template_selected(self):
        fc = _make_failure_context(ramdump="https://dump.example.com/dump.bin")
        jc = _make_job_config(ramdump_required=True)
        settings = _make_settings()

        ticket = build_ticket(fc, jc, settings)
        assert "Ramdump:" in ticket.description
        assert "dump.example.com" in ticket.description

    def test_no_ramdump_template_selected(self):
        fc = _make_failure_context(report="https://report.example.com")
        jc = _make_job_config(ramdump_required=False)
        settings = _make_settings()

        ticket = build_ticket(fc, jc, settings)
        assert "Report:" in ticket.description
        assert "report.example.com" in ticket.description

    def test_assignee_from_stage_poc_map(self):
        fc = _make_failure_context(stage_name="Build")
        jc = _make_job_config()
        settings = _make_settings()

        ticket = build_ticket(fc, jc, settings)
        assert ticket.assignee == "build@test.com"

    def test_assignee_for_custom_stage(self):
        fc = _make_failure_context(stage_name="Custom Stage")
        jc = _make_job_config()
        settings = _make_settings()

        ticket = build_ticket(fc, jc, settings)
        assert ticket.assignee == "custom@test.com"

    def test_assignee_fallback_to_default(self):
        fc = _make_failure_context(stage_name="Unknown Stage")
        jc = _make_job_config()
        settings = _make_settings()

        ticket = build_ticket(fc, jc, settings)
        assert ticket.assignee == "default@test.com"

    def test_title_format(self):
        fc = _make_failure_context(
            stage_name="Test Execution",
            job_name="my_test_job",
            build_number=99,
            category="sandbox",
        )
        jc = _make_job_config()
        settings = _make_settings()

        ticket = build_ticket(fc, jc, settings)
        assert "[SANDBOX]" in ticket.summary
        assert "my_test_job" in ticket.summary
        assert "#99" in ticket.summary
        assert "Test Execution" in ticket.summary

    def test_labels_generated(self):
        fc = _make_failure_context()
        jc = _make_job_config()
        settings = _make_settings()

        ticket = build_ticket(fc, jc, settings)
        assert "auto-test" in ticket.labels
        assert "sandbox" in ticket.labels

    def test_crashed_tests_in_body(self):
        fc = _make_failure_context(
            crashed_tests=["test_a", "test_b", "test_c"],
        )
        jc = _make_job_config(ramdump_required=False)
        settings = _make_settings(
            no_ramdump_template="Crashed:\n{crashed_tests}"
        )

        ticket = build_ticket(fc, jc, settings)
        assert "test_a" in ticket.description
        assert "test_b" in ticket.description
        assert "test_c" in ticket.description

    def test_project_key_from_settings(self):
        fc = _make_failure_context()
        jc = _make_job_config()
        settings = _make_settings()

        ticket = build_ticket(fc, jc, settings)
        assert ticket.project_key == "TEST"
        assert ticket.issue_type == "Bug"
        assert ticket.priority == "High"


class TestBuildDuplicateComment:
    def test_comment_format(self):
        fc = _make_failure_context(
            job_name="test_job",
            build_number=50,
            stage_name="Build",
        )
        jc = _make_job_config()

        comment = build_duplicate_comment(fc, jc)
        assert "New failure detected" in comment
        assert "#50" in comment
        assert "Build" in comment
