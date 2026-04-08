"""
Ticket builder for Jenkins-Jira Automation.

Builds Jira ticket data from failure context, resolving templates,
assignees, priorities, and labels based on job configuration.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from .models import (
    BuildInfo,
    ExtractedLinks,
    FailureContext,
    JobConfig,
    StageResult,
    TicketData,
)

logger = logging.getLogger(__name__)


def _resolve_assignee(
    failed_stage: StageResult,
    job_config: JobConfig,
) -> str:
    """
    Resolve the assignee email for a failed stage.

    Priority order:
        1. Job-specific stage POC (stage_poc_map for job stages)
        2. Category stage POC override (stage_poc_overrides)
        3. Category default POC (default_stage_poc)

    Args:
        failed_stage: The stage that failed.
        job_config: Resolved job configuration.

    Returns:
        Assignee email address.
    """
    stage_name = failed_stage.name

    # Look up in the pre-resolved POC map
    if stage_name in job_config.stage_poc_map:
        poc = job_config.stage_poc_map[stage_name]
        logger.debug(f"Resolved POC for stage '{stage_name}': {poc}")
        return poc

    # Fuzzy match: try matching stage name case-insensitively
    for mapped_stage, poc in job_config.stage_poc_map.items():
        if mapped_stage.lower().strip() == stage_name.lower().strip():
            logger.debug(f"Fuzzy-matched POC for stage '{stage_name}': {poc}")
            return poc

    # Fallback to default
    logger.info(
        f"No specific POC for stage '{stage_name}', "
        f"using default: {job_config.default_poc}"
    )
    return job_config.default_poc


def _format_crashed_tests(crashed_tests: list[str]) -> str:
    """Format crashed test list for Jira ticket body."""
    if not crashed_tests:
        return "No crashed tests detected."

    lines = [f"  {i+1}. {test}" for i, test in enumerate(crashed_tests[:50])]
    result = "\n".join(lines)
    if len(crashed_tests) > 50:
        result += f"\n  ... and {len(crashed_tests) - 50} more"
    return result


def _sanitize_label(text: str) -> str:
    """Sanitize a string to be a valid Jira label."""
    return re.sub(r'[^a-zA-Z0-9_\-]', '_', text.strip()).lower()


def build_ticket(
    failure_context: FailureContext,
    job_config: JobConfig,
    settings: dict,
) -> TicketData:
    """
    Build a complete Jira ticket from failure context.

    Selects the appropriate template (ramdump vs non-ramdump),
    fills in all placeholders, resolves the assignee, and
    generates appropriate labels.

    Args:
        failure_context: Complete failure information.
        job_config: Resolved job configuration.
        settings: Global settings dict.

    Returns:
        TicketData ready for creation via Jira API.
    """
    jira_settings = settings.get("jira", {})
    templates = settings.get("ticket_templates", {})
    log_tail_lines = settings.get("jenkins", {}).get("log_tail_lines", 50)

    # Resolve assignee
    assignee = _resolve_assignee(failure_context.failed_stage, job_config)
    failure_context.assignee_email = assignee

    # Select template based on ramdump requirement
    if job_config.ramdump_required:
        template = templates.get("with_ramdump", _DEFAULT_RAMDUMP_TEMPLATE)
    else:
        template = templates.get("without_ramdump", _DEFAULT_NO_RAMDUMP_TEMPLATE)

    # Build replacement values
    build_info = failure_context.build_info
    links = failure_context.links

    replacements = {
        "job_name": build_info.job_name,
        "build_number": str(build_info.build_number),
        "job_link": build_info.job_link,
        "category": build_info.category,
        "failed_stage": failure_context.failed_stage.name,
        "log_tail_lines": str(log_tail_lines),
        "stage_log_tail": failure_context.log_tail or "No log available",
        # Links (use "N/A" if not found)
        "ramdump_link": links.ramdump or "N/A",
        "report_link": links.report or "N/A",
        "vm_link": links.vm_link or "N/A",
        "artifact_link": links.artifact or "N/A",
        # Crashed tests
        "crashed_tests": _format_crashed_tests(failure_context.crashed_tests),
        # Jira formatting
        "noformat": "{noformat}",
    }

    # Format the description
    description = template
    for key, value in replacements.items():
        description = description.replace(f"{{{key}}}", value)

    # Generate labels
    base_labels = list(jira_settings.get("base_labels", []))
    labels = base_labels + [
        _sanitize_label(build_info.category),
        _sanitize_label(build_info.job_name),
        _sanitize_label(failure_context.failed_stage.name),
    ]
    # Remove empty/duplicate labels
    labels = list(dict.fromkeys(label for label in labels if label))

    # Build ticket
    ticket = TicketData(
        project_key=jira_settings.get("project_key", "JENKINS"),
        issue_type=jira_settings.get("issue_type", "Bug"),
        summary=failure_context.title,
        description=description,
        assignee=assignee,
        priority=jira_settings.get("default_priority", "High"),
        labels=labels,
    )

    logger.info(
        f"Built ticket: '{ticket.summary}' → assignee: {ticket.assignee}, "
        f"labels: {ticket.labels}"
    )
    return ticket


def build_duplicate_comment(
    failure_context: FailureContext,
    job_config: JobConfig,
) -> str:
    """
    Build a comment to add to an existing duplicate ticket.

    When a duplicate ticket already exists, add a comment with the
    new build's failure information.
    """
    build_info = failure_context.build_info
    return (
        f"*New failure detected for the same issue.*\n\n"
        f"Build: [#{build_info.build_number}|{build_info.job_link}]\n"
        f"Failed Stage: {failure_context.failed_stage.name}\n"
        f"Status: {failure_context.failed_stage.status.value}\n\n"
        f"*Stage Log (last lines):*\n"
        f"{{noformat}}{failure_context.log_tail}{{noformat}}"
    )


# ─── Default templates (used if not defined in settings.yaml) ──────────────

_DEFAULT_RAMDUMP_TEMPLATE = """Hi team,

A Jenkins job has failed and requires investigation.

*Job Details:*
||Field||Value||
|Job|{job_name}|
|Build|[#{build_number}|{job_link}]|
|Category|{category}|
|Failed Stage|{failed_stage}|
|Status|FAILED|

*Links:*
- Job Link: {job_link}
- Ramdump Link: {ramdump_link}
- VM Link: {vm_link}

*Stage Log (last {log_tail_lines} lines):*
{noformat}{stage_log_tail}{noformat}
"""

_DEFAULT_NO_RAMDUMP_TEMPLATE = """Hi team,

A Jenkins job has failed and requires investigation.

*Job Details:*
||Field||Value||
|Job|{job_name}|
|Build|[#{build_number}|{job_link}]|
|Category|{category}|
|Failed Stage|{failed_stage}|
|Status|FAILED|

*Links:*
- Job Link: {job_link}
- Report Link: {report_link}

*Testcases Crashed:*
{crashed_tests}

*Stage Log (last {log_tail_lines} lines):*
{noformat}{stage_log_tail}{noformat}
"""
