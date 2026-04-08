"""
Jira REST API client.

Creates issues, checks for duplicates, and manages tickets via the
Jira REST API v2.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .models import TicketData

logger = logging.getLogger(__name__)


class JiraError(Exception):
    """Raised when Jira API calls fail."""
    pass


class JiraClient:
    """
    Client for interacting with Jira REST API v2.

    Reads credentials from environment variables:
        JIRA_URL       — Base URL of Jira instance (e.g., https://jira.example.com)
        JIRA_USER      — Username/email for Basic Auth
        JIRA_API_TOKEN — API token for Basic Auth
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        api_token: Optional[str] = None,
        max_retries: int = 3,
        timeout: int = 30,
    ):
        self.base_url = (
            base_url or os.environ.get("JIRA_URL", "")
        ).rstrip("/")
        self.username = username or os.environ.get("JIRA_USER", "")
        self.api_token = api_token or os.environ.get("JIRA_API_TOKEN", "")
        self.timeout = timeout

        if not self.base_url:
            raise JiraError(
                "Jira URL not configured. Set JIRA_URL environment variable "
                "or pass base_url parameter."
            )

        # Set up session with retry
        self._session = requests.Session()
        if self.username and self.api_token:
            self._session.auth = (self.username, self.api_token)
        else:
            logger.warning(
                "Jira credentials not fully configured. "
                "Set JIRA_USER and JIRA_API_TOKEN."
            )

        retry_strategy = Retry(
            total=max_retries,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

        self._session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _api_url(self, path: str) -> str:
        """Build full API URL."""
        return f"{self.base_url}/rest/api/2/{path.lstrip('/')}"

    def _request(
        self,
        method: str,
        path: str,
        json_data: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> dict:
        """Make an API request with error handling."""
        url = self._api_url(path)
        try:
            logger.debug(f"{method.upper()} {url}")
            resp = self._session.request(
                method=method,
                url=url,
                json=json_data,
                params=params,
                timeout=self.timeout,
            )
            resp.raise_for_status()

            if resp.status_code == 204:
                return {}
            return resp.json()

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "unknown"
            body = ""
            try:
                body = e.response.json() if e.response is not None else ""
            except Exception:
                body = e.response.text if e.response is not None else ""
            raise JiraError(
                f"Jira API error (HTTP {status}) for {url}: {body}"
            ) from e
        except requests.exceptions.RequestException as e:
            raise JiraError(f"Jira API request failed for {url}: {e}") from e

    def _resolve_account_id(self, email: str) -> Optional[str]:
        """
        Resolve a user email to a Jira account ID.

        Required for Jira Cloud which uses accountId for assignment.
        Jira Server/Data Center may use 'name' instead.

        Args:
            email: User email address.

        Returns:
            Account ID string or None if not found.
        """
        try:
            # Jira Cloud: user search by email
            users = self._request(
                "GET",
                "user/search",
                params={"query": email, "maxResults": 1},
            )
            if isinstance(users, list) and users:
                account_id = users[0].get("accountId")
                logger.debug(f"Resolved {email} → accountId: {account_id}")
                return account_id

            # Fallback: try user/search with username
            users = self._request(
                "GET",
                "user/search",
                params={"username": email, "maxResults": 1},
            )
            if isinstance(users, list) and users:
                return users[0].get("accountId") or users[0].get("name")

        except JiraError as e:
            logger.warning(f"Failed to resolve user {email}: {e}")

        return None

    def create_issue(self, ticket_data: TicketData) -> TicketData:
        """
        Create a new Jira issue.

        Args:
            ticket_data: TicketData with all fields populated.

        Returns:
            Updated TicketData with issue_key and issue_url set.

        Raises:
            JiraError: If issue creation fails.
        """
        # Sanitize labels
        ticket_data.sanitize_labels()

        # Build issue payload
        fields = {
            "project": {"key": ticket_data.project_key},
            "summary": ticket_data.summary[:255],  # Jira summary limit
            "description": ticket_data.description,
            "issuetype": {"name": ticket_data.issue_type},
            "priority": {"name": ticket_data.priority},
        }

        # Add labels if present
        if ticket_data.labels:
            fields["labels"] = ticket_data.labels

        # Resolve and set assignee
        if ticket_data.assignee:
            account_id = self._resolve_account_id(ticket_data.assignee)
            if account_id:
                # Try Jira Cloud format first
                fields["assignee"] = {"accountId": account_id}
            else:
                # Fallback: Jira Server format
                fields["assignee"] = {"name": ticket_data.assignee}
                logger.warning(
                    f"Could not resolve account ID for {ticket_data.assignee}, "
                    f"using name-based assignment (Jira Server format)"
                )

        payload = {"fields": fields}

        logger.info(
            f"Creating Jira issue: {ticket_data.summary} "
            f"→ assignee: {ticket_data.assignee}"
        )
        result = self._request("POST", "issue", json_data=payload)

        ticket_data.issue_key = result.get("key", "")
        ticket_data.issue_url = f"{self.base_url}/browse/{ticket_data.issue_key}"

        logger.info(
            f"Created Jira issue: {ticket_data.issue_key} "
            f"({ticket_data.issue_url})"
        )
        return ticket_data

    def check_duplicate(
        self,
        project_key: str,
        job_name: str,
        build_number: int,
        jql_template: Optional[str] = None,
    ) -> Optional[str]:
        """
        Check if a Jira ticket already exists for this build.

        Args:
            project_key: Jira project key.
            job_name: Jenkins job name.
            build_number: Build number.
            jql_template: Custom JQL template (optional).

        Returns:
            Existing issue key if found, None otherwise.
        """
        if jql_template:
            jql = jql_template.format(
                project_key=project_key,
                job_name=job_name,
                build_number=build_number,
            )
        else:
            jql = (
                f'project = {project_key} AND '
                f'summary ~ "{job_name}" AND '
                f'summary ~ "#{build_number}" AND '
                f'status not in (Closed, Done, Resolved)'
            )

        try:
            result = self._request(
                "GET",
                "search",
                params={"jql": jql, "maxResults": 1, "fields": "key,summary"},
            )
            issues = result.get("issues", [])
            if issues:
                existing_key = issues[0].get("key", "")
                logger.info(
                    f"Duplicate ticket found: {existing_key} for "
                    f"{job_name} #{build_number}"
                )
                return existing_key
        except JiraError as e:
            logger.warning(f"Duplicate check failed (proceeding anyway): {e}")

        return None

    def add_comment(self, issue_key: str, comment: str) -> None:
        """
        Add a comment to an existing Jira issue.

        Args:
            issue_key: Jira issue key (e.g., "JENKINS-123").
            comment: Comment body text.
        """
        self._request(
            "POST",
            f"issue/{issue_key}/comment",
            json_data={"body": comment},
        )
        logger.info(f"Added comment to {issue_key}")

    def transition_issue(self, issue_key: str, transition_name: str) -> None:
        """
        Transition an issue to a new status.

        Args:
            issue_key: Jira issue key.
            transition_name: Name of the transition (e.g., "In Progress").
        """
        # First get available transitions
        result = self._request("GET", f"issue/{issue_key}/transitions")
        transitions = result.get("transitions", [])

        target_transition = None
        for t in transitions:
            if t.get("name", "").lower() == transition_name.lower():
                target_transition = t
                break

        if not target_transition:
            available = [t.get("name") for t in transitions]
            logger.warning(
                f"Transition '{transition_name}' not found for {issue_key}. "
                f"Available: {available}"
            )
            return

        self._request(
            "POST",
            f"issue/{issue_key}/transitions",
            json_data={"transition": {"id": target_transition["id"]}},
        )
        logger.info(f"Transitioned {issue_key} to '{transition_name}'")

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
