"""
Data models for Jenkins-Jira Automation.

Dataclasses representing build information, stage results, failure context,
and Jira ticket data flowing through the pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class StageStatus(Enum):
    """Possible statuses for a Jenkins pipeline stage."""
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    UNSTABLE = "UNSTABLE"
    ABORTED = "ABORTED"
    NOT_EXECUTED = "NOT_EXECUTED"
    IN_PROGRESS = "IN_PROGRESS"
    PAUSED_PENDING_INPUT = "PAUSED_PENDING_INPUT"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def from_string(cls, value: str) -> "StageStatus":
        """Parse a status string, defaulting to UNKNOWN for unrecognized values."""
        try:
            return cls(value.upper())
        except ValueError:
            return cls.UNKNOWN

    @property
    def is_failure(self) -> bool:
        """Whether this status represents a failure condition."""
        return self in (StageStatus.FAILED, StageStatus.UNSTABLE)


@dataclass
class BuildInfo:
    """Information about a Jenkins build."""
    job_name: str
    build_number: int
    url: str
    status: str
    category: str
    timestamp: Optional[int] = None
    duration: Optional[int] = None
    # Full job path in Jenkins (e.g., "job/sandbox/job/test_job")
    job_path: str = ""

    @property
    def job_link(self) -> str:
        """Direct URL to the build."""
        return f"{self.url.rstrip('/')}/{self.build_number}"

    @property
    def is_failed(self) -> bool:
        return self.status in ("FAILURE", "FAILED", "UNSTABLE")


@dataclass
class StageResult:
    """Result of a single pipeline stage."""
    name: str
    stage_id: str
    status: StageStatus
    duration_ms: int = 0
    # Log content (populated on demand for the failing stage)
    log: str = ""

    @property
    def is_failure(self) -> bool:
        return self.status.is_failure


@dataclass
class ExtractedLinks:
    """Links extracted from stage logs."""
    ramdump: Optional[str] = None
    report: Optional[str] = None
    vm_link: Optional[str] = None
    artifact: Optional[str] = None
    # Any additional links found
    extra: dict = field(default_factory=dict)


@dataclass
class FailureContext:
    """Complete context about a build failure for ticket creation."""
    build_info: BuildInfo
    failed_stage: StageResult
    links: ExtractedLinks
    crashed_tests: list[str] = field(default_factory=list)
    log_tail: str = ""
    # The POC resolved for this failure
    assignee_email: str = ""
    # Whether this job requires ramdump
    ramdump_required: bool = False

    @property
    def title(self) -> str:
        """Generate a Jira ticket title."""
        safe_stage = re.sub(r'[^\w\s\-:]', '', self.failed_stage.name).strip()
        return (
            f"[{self.build_info.category.upper()}] "
            f"{self.build_info.job_name} "
            f"#{self.build_info.build_number} — "
            f"Failed at: {safe_stage}"
        )


@dataclass
class TicketData:
    """Data required to create a Jira issue."""
    project_key: str
    issue_type: str
    summary: str  # title
    description: str  # body
    assignee: str  # email or account ID
    priority: str = "High"
    labels: list[str] = field(default_factory=list)
    # After creation
    issue_key: Optional[str] = None
    issue_url: Optional[str] = None

    def sanitize_labels(self) -> None:
        """Sanitize labels to be valid Jira labels (no spaces, lowercase)."""
        self.labels = [
            re.sub(r'[^a-zA-Z0-9_\-]', '_', label.strip()).lower()
            for label in self.labels
            if label.strip()
        ]


@dataclass
class JobConfig:
    """Resolved configuration for a specific job."""
    category: str
    job_name: str
    pattern: str
    # Ordered list of all stages (defaults + job-specific)
    all_stages: list[str]
    # Default stages from category
    default_stages: list[str]
    # Job-specific stages
    job_stages: list[str]
    # Resolved POC mapping: stage_name → email
    stage_poc_map: dict[str, str] = field(default_factory=dict)
    ramdump_required: bool = False
    # Default POC fallback
    default_poc: str = ""
