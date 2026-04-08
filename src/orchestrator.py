"""
Orchestrator for Jenkins-Jira Automation.

Main workflow that ties together configuration loading, Jenkins API calls,
log parsing, and Jira ticket creation into a single pipeline.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict
from typing import Optional

from .config_loader import (
    ConfigError,
    find_category_for_job,
    get_all_job_names,
    load_all_categories,
    load_category,
    load_settings,
    resolve_job_config,
)
from .jenkins_client import JenkinsClient, JenkinsError
from .jira_client import JiraClient, JiraError
from .log_parser import find_first_failure, parse_stage_log
from .models import (
    BuildInfo,
    FailureContext,
    JobConfig,
    TicketData,
)
from .ticket_builder import build_duplicate_comment, build_ticket

logger = logging.getLogger(__name__)


class AutomationError(Exception):
    """Raised when the automation workflow encounters an unrecoverable error."""
    pass


class Orchestrator:
    """
    Main orchestrator that drives the Jenkins → Parse → Jira pipeline.

    Usage:
        orchestrator = Orchestrator()
        result = orchestrator.process_build("sandbox", "my_job", 42)
    """

    def __init__(
        self,
        settings: Optional[dict] = None,
        jenkins_client: Optional[JenkinsClient] = None,
        jira_client: Optional[JiraClient] = None,
        dry_run: bool = False,
    ):
        """
        Initialize the orchestrator.

        Args:
            settings: Pre-loaded settings dict. Loads from file if None.
            jenkins_client: Pre-configured Jenkins client. Creates new if None.
            jira_client: Pre-configured Jira client. Creates new if None.
            dry_run: If True, print ticket preview without creating in Jira.
        """
        self.dry_run = dry_run

        # Load settings
        try:
            self.settings = settings or load_settings()
        except ConfigError as e:
            raise AutomationError(f"Failed to load settings: {e}") from e

        # Initialize clients
        jenkins_config = self.settings.get("jenkins", {})
        self._jenkins = jenkins_client
        self._jira = jira_client

        # Lazy init — only create when first needed
        self._jenkins_initialized = jenkins_client is not None
        self._jira_initialized = jira_client is not None

    def _get_jenkins(self) -> JenkinsClient:
        """Lazy-initialize Jenkins client."""
        if not self._jenkins_initialized:
            jenkins_config = self.settings.get("jenkins", {})
            self._jenkins = JenkinsClient(
                base_url=jenkins_config.get("base_url"),
                max_retries=jenkins_config.get("max_retries", 3),
                backoff_factor=jenkins_config.get("retry_backoff_factor", 2),
            )
            self._jenkins_initialized = True
        return self._jenkins

    def _get_jira(self) -> JiraClient:
        """Lazy-initialize Jira client."""
        if not self._jira_initialized:
            self._jira = JiraClient()
            self._jira_initialized = True
        return self._jira

    def process_build(
        self,
        category: str,
        job_name: str,
        build_number: int | str,
    ) -> Optional[TicketData]:
        """
        Process a single build — the main workflow.

        Steps:
            1. Load configuration for category + job
            2. Fetch build info from Jenkins
            3. Skip if build is successful
            4. Fetch stage results
            5. Find first failure stage
            6. Fetch and parse the failing stage log
            7. Check for duplicate Jira tickets
            8. Build and create the Jira ticket

        Args:
            category: Category name (e.g., "sandbox")
            job_name: Job name within the category
            build_number: Build number or "latest"/"lastFailedBuild"

        Returns:
            Created TicketData (with issue_key) or None if skipped.
        """
        logger.info(
            f"{'[DRY RUN] ' if self.dry_run else ''}"
            f"Processing: {category}/{job_name} #{build_number}"
        )

        # ── Step 1: Load configuration ──────────────────────────────────
        try:
            cat_data = load_category(category)
            job_config = resolve_job_config(cat_data, job_name, self.settings)
        except ConfigError as e:
            logger.error(f"Configuration error: {e}")
            raise AutomationError(f"Configuration error: {e}") from e

        logger.info(
            f"Config loaded: {len(job_config.all_stages)} stages, "
            f"ramdump={job_config.ramdump_required}, "
            f"default_poc={job_config.default_poc}"
        )

        # ── Step 2: Fetch build info ────────────────────────────────────
        jenkins = self._get_jenkins()
        job_path = f"{category}/{job_name}"

        try:
            # Resolve "latest" or "lastFailedBuild" to actual number
            if isinstance(build_number, str) and not build_number.isdigit():
                if build_number in ("latest", "last"):
                    build_number = jenkins.get_latest_build_number(job_path)
                elif build_number == "lastFailed":
                    build_number = jenkins.get_latest_build_number(
                        job_path, status_filter="FAILURE"
                    )
                else:
                    build_number = int(build_number)

            build_info = jenkins.get_build_info(
                job_path, build_number, category=category
            )
        except JenkinsError as e:
            raise AutomationError(f"Failed to fetch build info: {e}") from e

        # ── Step 3: Check if build actually failed ──────────────────────
        if not build_info.is_failed:
            logger.info(
                f"Build {job_name} #{build_info.build_number} is "
                f"{build_info.status} — skipping ticket creation."
            )
            return None

        logger.info(
            f"Build FAILED: {build_info.job_name} #{build_info.build_number} "
            f"({build_info.status})"
        )

        # ── Step 4: Fetch stage results ─────────────────────────────────
        try:
            stages = jenkins.get_stage_results(job_path, build_info.build_number)
        except JenkinsError as e:
            raise AutomationError(f"Failed to fetch stage results: {e}") from e

        if not stages:
            raise AutomationError(
                f"No pipeline stages found for {job_name} #{build_info.build_number}. "
                f"Is this a pipeline job?"
            )

        # ── Step 5: Find first failure ──────────────────────────────────
        failed_stage = find_first_failure(stages)
        if not failed_stage:
            # Build is marked failed but no individual stage failed
            # Use the last stage as a fallback
            logger.warning(
                "Build is FAILED but no individual stage has FAILED status. "
                "Using last stage as reference."
            )
            failed_stage = stages[-1]

        logger.info(
            f"First failure at stage: '{failed_stage.name}' "
            f"(stage_id: {failed_stage.stage_id})"
        )

        # ── Step 6: Fetch and parse stage log ───────────────────────────
        try:
            stage_log = jenkins.get_stage_log(
                job_path, build_info.build_number, failed_stage.stage_id
            )
            failed_stage.log = stage_log
        except JenkinsError as e:
            logger.warning(f"Failed to fetch stage log: {e}")
            stage_log = ""

        # Parse the log
        link_patterns = self.settings.get("link_patterns")
        test_patterns = self.settings.get("test_failure_patterns")
        log_tail_lines = self.settings.get("jenkins", {}).get("log_tail_lines", 50)

        parsed = parse_stage_log(
            stage_log,
            link_patterns=link_patterns,
            test_failure_patterns=test_patterns,
            tail_lines=log_tail_lines,
        )

        # ── Build failure context ───────────────────────────────────────
        failure_context = FailureContext(
            build_info=build_info,
            failed_stage=failed_stage,
            links=parsed["links"],
            crashed_tests=parsed["crashed_tests"],
            log_tail=parsed["log_tail"],
            ramdump_required=job_config.ramdump_required,
        )

        # ── Step 7: Check for duplicates ────────────────────────────────
        duplicate_config = self.settings.get("duplicate_check", {})
        existing_key = None

        if duplicate_config.get("enabled", True) and not self.dry_run:
            try:
                jira = self._get_jira()
                jira_settings = self.settings.get("jira", {})
                existing_key = jira.check_duplicate(
                    project_key=jira_settings.get("project_key", "JENKINS"),
                    job_name=build_info.job_name,
                    build_number=build_info.build_number,
                    jql_template=duplicate_config.get("jql_template"),
                )
            except JiraError as e:
                logger.warning(f"Duplicate check failed: {e}")

        if existing_key:
            logger.info(f"Duplicate ticket found: {existing_key}")
            # Add a comment to the existing ticket instead
            if not self.dry_run:
                try:
                    comment = build_duplicate_comment(failure_context, job_config)
                    jira.add_comment(existing_key, comment)
                    logger.info(f"Added comment to existing ticket {existing_key}")
                except JiraError as e:
                    logger.error(f"Failed to add comment to {existing_key}: {e}")

            return TicketData(
                project_key="",
                issue_type="",
                summary=f"Duplicate — see {existing_key}",
                description="",
                assignee="",
                issue_key=existing_key,
            )

        # ── Step 8: Build ticket ────────────────────────────────────────
        ticket = build_ticket(failure_context, job_config, self.settings)

        # ── Step 9: Create or preview ───────────────────────────────────
        if self.dry_run:
            self._print_dry_run(ticket, failure_context)
            return ticket

        try:
            jira = self._get_jira()
            ticket = jira.create_issue(ticket)
            logger.info(
                f"✅ Jira ticket created: {ticket.issue_key} "
                f"({ticket.issue_url})"
            )
        except JiraError as e:
            raise AutomationError(f"Failed to create Jira ticket: {e}") from e

        return ticket

    def process_job_latest(
        self,
        category: str,
        job_name: str,
    ) -> Optional[TicketData]:
        """Process the latest failed build for a specific job."""
        jenkins = self._get_jenkins()
        job_path = f"{category}/{job_name}"
        try:
            build_number = jenkins.get_latest_build_number(
                job_path, status_filter="FAILURE"
            )
        except JenkinsError as e:
            logger.info(f"No failed builds for {job_name}: {e}")
            return None

        return self.process_build(category, job_name, build_number)

    def process_category(self, category: str) -> list[TicketData]:
        """
        Process all jobs in a category, creating tickets for latest failures.

        Args:
            category: Category name.

        Returns:
            List of created TicketData objects.
        """
        try:
            cat_data = load_category(category)
        except ConfigError as e:
            logger.error(f"Failed to load category '{category}': {e}")
            return []

        job_names = get_all_job_names(cat_data)
        if not job_names:
            logger.warning(f"No jobs defined in category '{category}'")
            return []

        logger.info(f"Processing category '{category}': {len(job_names)} jobs")
        results = []

        for job_name in job_names:
            try:
                result = self.process_job_latest(category, job_name)
                if result:
                    results.append(result)
            except AutomationError as e:
                logger.error(f"Error processing {job_name}: {e}")
                continue

        logger.info(
            f"Category '{category}' complete: "
            f"{len(results)} tickets created/updated"
        )
        return results

    def process_all(self) -> dict[str, list[TicketData]]:
        """
        Process all categories and all jobs.

        Returns:
            Dict mapping category name → list of TicketData.
        """
        categories = load_all_categories()
        all_results = {}

        for cat_name in categories:
            results = self.process_category(cat_name)
            all_results[cat_name] = results

        total = sum(len(v) for v in all_results.values())
        logger.info(f"Scan complete: {total} tickets across {len(all_results)} categories")
        return all_results

    def _print_dry_run(
        self,
        ticket: TicketData,
        failure_context: FailureContext,
    ) -> None:
        """Print a formatted preview of the ticket that would be created."""
        separator = "=" * 72
        print(f"\n{separator}")
        print("  🔍 DRY RUN — Ticket Preview (not created)")
        print(separator)
        print(f"\n  Project:    {ticket.project_key}")
        print(f"  Type:       {ticket.issue_type}")
        print(f"  Priority:   {ticket.priority}")
        print(f"  Assignee:   {ticket.assignee}")
        print(f"  Labels:     {', '.join(ticket.labels)}")
        print(f"\n  Summary:")
        print(f"    {ticket.summary}")
        print(f"\n  Failure Info:")
        print(f"    Stage:    {failure_context.failed_stage.name}")
        print(f"    Status:   {failure_context.failed_stage.status.value}")
        print(f"    Ramdump:  {failure_context.links.ramdump or 'N/A'}")
        print(f"    Report:   {failure_context.links.report or 'N/A'}")
        print(f"    VM Link:  {failure_context.links.vm_link or 'N/A'}")
        if failure_context.crashed_tests:
            print(f"    Crashed:  {len(failure_context.crashed_tests)} tests")
            for t in failure_context.crashed_tests[:5]:
                print(f"      - {t}")
        print(f"\n  Description (first 500 chars):")
        print(f"    {ticket.description[:500]}...")
        print(f"\n{separator}\n")

    def close(self) -> None:
        """Close all clients."""
        if self._jenkins and self._jenkins_initialized:
            self._jenkins.close()
        if self._jira and self._jira_initialized:
            self._jira.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
