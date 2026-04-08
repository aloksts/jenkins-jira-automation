"""
Tests for log_parser module.

Validates first-failure detection, link extraction, crashed test parsing,
log tail/head generation, and failure reason detection.
"""

import os
import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.log_parser import (
    find_first_failure,
    find_all_failures,
    extract_links,
    extract_crashed_tests,
    get_log_tail,
    get_log_head,
    extract_stage_summary,
    detect_failure_reason,
    parse_stage_log,
)
from src.models import StageResult, StageStatus


# ─── Fixtures ────────────────────────────────────────────────────────────────

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> str:
    path = FIXTURES_DIR / name
    return path.read_text()


def _make_stage(name: str, status: str, stage_id: str = "1") -> StageResult:
    return StageResult(
        name=name,
        stage_id=stage_id,
        status=StageStatus.from_string(status),
    )


# ─── Tests: find_first_failure ───────────────────────────────────────────────

class TestFindFirstFailure:
    def test_single_failure(self):
        stages = [
            _make_stage("Setup", "SUCCESS", "1"),
            _make_stage("Build", "FAILED", "2"),
            _make_stage("Test", "SUCCESS", "3"),
        ]
        result = find_first_failure(stages)
        assert result is not None
        assert result.name == "Build"

    def test_multiple_failures_returns_first(self):
        stages = [
            _make_stage("Setup", "SUCCESS", "1"),
            _make_stage("Build", "FAILED", "2"),
            _make_stage("Test", "FAILED", "3"),
            _make_stage("Deploy", "FAILED", "4"),
        ]
        result = find_first_failure(stages)
        assert result.name == "Build"

    def test_no_failure(self):
        stages = [
            _make_stage("Setup", "SUCCESS", "1"),
            _make_stage("Build", "SUCCESS", "2"),
            _make_stage("Test", "SUCCESS", "3"),
        ]
        result = find_first_failure(stages)
        assert result is None

    def test_unstable_is_failure(self):
        stages = [
            _make_stage("Setup", "SUCCESS", "1"),
            _make_stage("Build", "UNSTABLE", "2"),
            _make_stage("Test", "FAILED", "3"),
        ]
        result = find_first_failure(stages)
        assert result.name == "Build"

    def test_empty_stages(self):
        result = find_first_failure([])
        assert result is None

    def test_first_stage_fails(self):
        stages = [
            _make_stage("Setup", "FAILED", "1"),
            _make_stage("Build", "NOT_EXECUTED", "2"),
        ]
        result = find_first_failure(stages)
        assert result.name == "Setup"

    def test_aborted_is_not_failure(self):
        stages = [
            _make_stage("Setup", "SUCCESS", "1"),
            _make_stage("Build", "ABORTED", "2"),
        ]
        result = find_first_failure(stages)
        assert result is None


class TestFindAllFailures:
    def test_returns_all(self):
        stages = [
            _make_stage("S1", "SUCCESS", "1"),
            _make_stage("S2", "FAILED", "2"),
            _make_stage("S3", "SUCCESS", "3"),
            _make_stage("S4", "FAILED", "4"),
        ]
        failures = find_all_failures(stages)
        assert len(failures) == 2
        assert failures[0].name == "S2"
        assert failures[1].name == "S4"


# ─── Tests: extract_links ───────────────────────────────────────────────────

class TestExtractLinks:
    def test_extract_from_ramdump_fixture(self):
        log = _load_fixture("sample_stage_log_ramdump.txt")
        links = extract_links(log)
        assert links.ramdump is not None
        assert "ramdump" in links.ramdump.lower() or "storage.example.com" in links.ramdump
        assert links.vm_link is not None
        assert "vm-manager.example.com" in links.vm_link

    def test_extract_from_no_ramdump_fixture(self):
        log = _load_fixture("sample_stage_log_no_ramdump.txt")
        links = extract_links(log)
        assert links.ramdump is None  # No ramdump in this log
        assert links.report is not None
        assert "reports.example.com" in links.report

    def test_no_links_in_empty_log(self):
        links = extract_links("")
        assert links.ramdump is None
        assert links.report is None
        assert links.vm_link is None

    def test_custom_patterns(self):
        log = "Custom link: https://custom.example.com/data"
        patterns = {
            "custom": r'Custom\s+link:\s+(?P<url>https?://\S+)',
        }
        links = extract_links(log, patterns)
        assert links.extra.get("custom") == "https://custom.example.com/data"

    def test_multiple_matches_takes_last(self):
        log = """
        report link: https://old-report.example.com
        report link: https://new-report.example.com
        """
        links = extract_links(log)
        assert links.report == "https://new-report.example.com"


# ─── Tests: extract_crashed_tests ────────────────────────────────────────────

class TestExtractCrashedTests:
    def test_from_ramdump_fixture(self):
        log = _load_fixture("sample_stage_log_ramdump.txt")
        crashed = extract_crashed_tests(log)
        assert "test_gpu_memory_allocation" in crashed
        assert "test_texture_load" in crashed

    def test_from_no_ramdump_fixture(self):
        log = _load_fixture("sample_stage_log_no_ramdump.txt")
        crashed = extract_crashed_tests(log)
        assert "test_game_render" in crashed
        assert len(crashed) >= 2

    def test_no_crashes(self):
        log = "All tests passed successfully!"
        crashed = extract_crashed_tests(log)
        assert crashed == []

    def test_deduplication(self):
        log = """
FAILED: test_abc
FAILED: test_abc
CRASHED: test_abc
        """
        crashed = extract_crashed_tests(log)
        assert crashed.count("test_abc") == 1

    def test_preserves_order(self):
        log = """
FAILED: test_z
FAILED: test_a
FAILED: test_m
        """
        crashed = extract_crashed_tests(log)
        assert crashed == ["test_z", "test_a", "test_m"]


# ─── Tests: get_log_tail / get_log_head ──────────────────────────────────────

class TestLogTailHead:
    def test_tail_short_log(self):
        log = "line1\nline2\nline3"
        tail = get_log_tail(log, n=5)
        assert tail == "line1\nline2\nline3"

    def test_tail_long_log(self):
        lines = [f"line{i}" for i in range(100)]
        log = "\n".join(lines)
        tail = get_log_tail(log, n=10)
        assert tail.count("\n") == 9  # 10 lines = 9 newlines
        assert "line99" in tail
        assert "line0" not in tail

    def test_tail_empty(self):
        assert get_log_tail("") == ""

    def test_head(self):
        lines = [f"line{i}" for i in range(100)]
        log = "\n".join(lines)
        head = get_log_head(log, n=5)
        assert "line0" in head
        assert "line4" in head
        assert "line5" not in head


# ─── Tests: detect_failure_reason ────────────────────────────────────────────

class TestDetectFailureReason:
    def test_segfault(self):
        log = "Some output\nError: Segmentation fault in func()\nMore output"
        reason = detect_failure_reason(log)
        assert reason is not None
        assert "Segmentation fault" in reason

    def test_build_failed(self):
        log = "Compiling...\nbuild failed\nDone"
        reason = detect_failure_reason(log)
        assert "Build failure" in reason

    def test_no_error(self):
        log = "Everything went fine\nAll good"
        reason = detect_failure_reason(log)
        assert reason is None

    def test_out_of_memory(self):
        log = "Processing...\nout of memory\n"
        reason = detect_failure_reason(log)
        assert "Out of memory" in reason


# ─── Tests: parse_stage_log ──────────────────────────────────────────────────

class TestParseStageLog:
    def test_full_parse_ramdump(self):
        log = _load_fixture("sample_stage_log_ramdump.txt")
        result = parse_stage_log(log)

        assert result["links"].ramdump is not None
        assert result["links"].vm_link is not None
        assert len(result["crashed_tests"]) >= 2
        assert result["log_tail"] != ""
        assert result["total_lines"] > 0

    def test_full_parse_no_ramdump(self):
        log = _load_fixture("sample_stage_log_no_ramdump.txt")
        result = parse_stage_log(log)

        assert result["links"].ramdump is None
        assert result["links"].report is not None
        assert len(result["crashed_tests"]) >= 2
        assert result["log_tail"] != ""

    def test_empty_log(self):
        result = parse_stage_log("")
        assert result["links"].ramdump is None
        assert result["crashed_tests"] == []
        assert result["log_tail"] == ""
        assert result["total_lines"] == 0
