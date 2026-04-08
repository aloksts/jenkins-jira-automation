"""
Jenkins log parser.

Analyzes pipeline stage results to find the first failure, extracts
links (ramdump, report, VM), identifies crashed tests, and provides
log tail for ticket bodies.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from .models import ExtractedLinks, StageResult, StageStatus

logger = logging.getLogger(__name__)

# Default patterns if not provided via config
DEFAULT_LINK_PATTERNS = {
    "ramdump": r'(?i)ramdump[^\n]*?(?P<url>https?://\S+)',
    "report": r'(?i)report[^\n]*?(?P<url>https?://\S+)',
    "vm_link": r'(?i)vm[^\n]*?link[^\n]*?(?P<url>https?://\S+)',
    "artifact": r'(?i)artifact[^\n]*?(?P<url>https?://\S+)',
}

DEFAULT_TEST_FAILURE_PATTERNS = [
    r'(?i)FAIL(?:ED)?:\s*(?P<test>\S+)',
    r'(?i)CRASH(?:ED)?:\s*(?P<test>\S+)',
    r'(?i)ERROR\s+in\s+(?P<test>\S+)',
    r'(?i)Test\s+(?P<test>\S+)\s+FAILED',
]


def find_first_failure(stages: list[StageResult]) -> Optional[StageResult]:
    """
    Find the first failing stage in pipeline order.

    Jenkins pipelines may continue running after a failure (depending on
    configuration). This function identifies the FIRST stage that has a
    FAILED or UNSTABLE status, which is the root cause stage.

    Args:
        stages: List of StageResult objects in pipeline execution order.

    Returns:
        The first failing StageResult, or None if no failure found.
    """
    for stage in stages:
        if stage.is_failure:
            logger.info(
                f"First failure found at stage: '{stage.name}' "
                f"(status: {stage.status.value})"
            )
            return stage

    logger.info("No failing stage found in pipeline")
    return None


def find_all_failures(stages: list[StageResult]) -> list[StageResult]:
    """
    Find all failing stages in the pipeline.

    Useful for generating a summary of all failures in the ticket.

    Args:
        stages: List of StageResult objects in pipeline execution order.

    Returns:
        List of all failing StageResult objects.
    """
    failures = [s for s in stages if s.is_failure]
    logger.info(f"Found {len(failures)} failing stages out of {len(stages)} total")
    return failures


def extract_links(
    log_text: str,
    patterns: Optional[dict[str, str]] = None,
) -> ExtractedLinks:
    """
    Extract links (ramdump, report, VM, artifact) from log text using regex.

    Searches the log text for each pattern and extracts the URL from the
    named 'url' group. If multiple matches are found, takes the LAST match
    (most recent/relevant).

    Args:
        log_text: Full stage log content.
        patterns: Dict of link_type → regex pattern. Uses defaults if None.

    Returns:
        ExtractedLinks with found URLs.
    """
    if patterns is None:
        patterns = DEFAULT_LINK_PATTERNS

    links = ExtractedLinks()
    extra_links = {}

    for link_type, pattern in patterns.items():
        try:
            matches = list(re.finditer(pattern, log_text))
            if matches:
                # Take the last match (most recent)
                url = matches[-1].group("url")
                # Clean trailing punctuation from URLs
                url = re.sub(r'[)\]},;\'\"]+$', '', url)

                if hasattr(links, link_type):
                    setattr(links, link_type, url)
                else:
                    extra_links[link_type] = url

                logger.debug(f"Extracted {link_type} link: {url}")
        except re.error as e:
            logger.error(f"Invalid regex pattern for {link_type}: {pattern} — {e}")

    links.extra = extra_links
    return links


def extract_crashed_tests(
    log_text: str,
    patterns: Optional[list[str]] = None,
) -> list[str]:
    """
    Extract names of crashed/failed test cases from log text.

    Searches using multiple regex patterns and deduplicates results
    while preserving order.

    Args:
        log_text: Stage log content.
        patterns: List of regex patterns with named group 'test'.

    Returns:
        Unique list of crashed test names, in order of first appearance.
    """
    if patterns is None:
        patterns = DEFAULT_TEST_FAILURE_PATTERNS

    seen = set()
    crashed = []

    for pattern in patterns:
        try:
            for match in re.finditer(pattern, log_text):
                test_name = match.group("test").strip()
                if test_name and test_name not in seen:
                    seen.add(test_name)
                    crashed.append(test_name)
        except re.error as e:
            logger.error(f"Invalid test failure pattern: {pattern} — {e}")

    logger.info(f"Found {len(crashed)} crashed/failed tests")
    return crashed


def get_log_tail(log_text: str, n: int = 50) -> str:
    """
    Get the last N lines of a log.

    Efficiently handles very large logs (50,000+ lines) without
    loading the entire content into a list at once. Uses reverse
    scanning for performance.

    Args:
        log_text: Full log text.
        n: Number of tail lines to return.

    Returns:
        Last N lines as a single string.
    """
    if not log_text:
        return ""

    # For moderate sizes, simple split is fine
    lines = log_text.splitlines()
    tail_lines = lines[-n:] if len(lines) > n else lines
    return "\n".join(tail_lines)


def get_log_head(log_text: str, n: int = 20) -> str:
    """Get the first N lines of a log."""
    if not log_text:
        return ""
    lines = log_text.splitlines()
    head_lines = lines[:n]
    return "\n".join(head_lines)


def extract_stage_summary(
    log_text: str,
    tail_lines: int = 50,
    head_lines: int = 10,
) -> str:
    """
    Extract a meaningful summary from a stage log.

    Combines the first few lines (context/setup) with the last lines
    (where failures typically appear), separated by an ellipsis marker.

    Args:
        log_text: Full stage log.
        tail_lines: Number of lines from the end.
        head_lines: Number of lines from the start.

    Returns:
        Summarized log content.
    """
    if not log_text:
        return ""

    lines = log_text.splitlines()
    total = len(lines)

    if total <= (head_lines + tail_lines):
        return log_text

    head = "\n".join(lines[:head_lines])
    tail = "\n".join(lines[-tail_lines:])
    skipped = total - head_lines - tail_lines

    return f"{head}\n\n... ({skipped} lines omitted) ...\n\n{tail}"


def detect_failure_reason(log_text: str) -> Optional[str]:
    """
    Attempt to detect the root cause of failure from the log.

    Searches for common error patterns and returns a brief description.

    Args:
        log_text: Stage log content.

    Returns:
        Brief failure reason string, or None if not detected.
    """
    # Check last 200 lines for error markers
    tail = get_log_tail(log_text, 200)

    # Common error patterns, ordered by specificity
    error_patterns = [
        (r'(?i)fatal:\s*(.+)', "Fatal error"),
        (r'(?i)error:\s*(.+)', "Error"),
        (r'(?i)exception:\s*(.+)', "Exception"),
        (r'(?i)FAILURE:\s*(.+)', "Failure"),
        (r'(?i)command\s+not\s+found:\s*(\S+)', "Command not found"),
        (r'(?i)No\s+such\s+file\s+or\s+directory:\s*(.+)', "File not found"),
        (r'(?i)Permission\s+denied:\s*(.+)', "Permission denied"),
        (r'(?i)timeout\s+(?:expired|exceeded)', "Timeout"),
        (r'(?i)out\s+of\s+memory', "Out of memory"),
        (r'(?i)device\s+(?:not\s+found|offline|disconnected)', "Device issue"),
        (r'(?i)build\s+failed', "Build failure"),
        (r'(?i)compilation\s+(?:error|failed)', "Compilation failure"),
        (r'(?i)segmentation\s+fault', "Segmentation fault"),
    ]

    for pattern, label in error_patterns:
        match = re.search(pattern, tail)
        if match:
            detail = match.group(1).strip()[:100] if match.lastindex else ""
            reason = f"{label}: {detail}" if detail else label
            logger.debug(f"Detected failure reason: {reason}")
            return reason

    return None


def parse_stage_log(
    log_text: str,
    link_patterns: Optional[dict[str, str]] = None,
    test_failure_patterns: Optional[list[str]] = None,
    tail_lines: int = 50,
) -> dict:
    """
    Full parsing of a stage log — extracts all relevant information.

    This is the main entry point for log analysis, combining all
    extraction functions into a single call.

    Args:
        log_text: Full stage log content.
        link_patterns: Regex patterns for link extraction.
        test_failure_patterns: Regex patterns for test failure detection.
        tail_lines: Number of tail lines for the ticket body.

    Returns:
        Dict containing:
            - links: ExtractedLinks
            - crashed_tests: list[str]
            - log_tail: str
            - log_summary: str
            - failure_reason: Optional[str]
            - total_lines: int
    """
    links = extract_links(log_text, link_patterns)
    crashed_tests = extract_crashed_tests(log_text, test_failure_patterns)
    log_tail = get_log_tail(log_text, tail_lines)
    log_summary = extract_stage_summary(log_text, tail_lines=tail_lines)
    failure_reason = detect_failure_reason(log_text)
    total_lines = len(log_text.splitlines()) if log_text else 0

    logger.info(
        f"Parsed stage log: {total_lines} lines, "
        f"links={sum(1 for v in [links.ramdump, links.report, links.vm_link, links.artifact] if v)}, "
        f"crashed_tests={len(crashed_tests)}, "
        f"failure_reason={failure_reason or 'unknown'}"
    )

    return {
        "links": links,
        "crashed_tests": crashed_tests,
        "log_tail": log_tail,
        "log_summary": log_summary,
        "failure_reason": failure_reason,
        "total_lines": total_lines,
    }
