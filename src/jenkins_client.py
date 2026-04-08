"""
Jenkins REST API client.

Fetches build information, pipeline stage results, and stage logs
from Jenkins using the Pipeline REST API and Blue Ocean API.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional
from urllib.parse import quote as url_quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .models import BuildInfo, StageResult, StageStatus

logger = logging.getLogger(__name__)


class JenkinsError(Exception):
    """Raised when Jenkins API calls fail."""
    pass


class JenkinsClient:
    """
    Client for interacting with Jenkins REST API.

    Reads credentials from environment variables:
        JENKINS_URL   — Base URL of Jenkins instance
        JENKINS_USER  — Username for Basic Auth
        JENKINS_API_TOKEN — API token for Basic Auth

    Supports retry with exponential backoff for transient failures.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        api_token: Optional[str] = None,
        max_retries: int = 3,
        backoff_factor: float = 2.0,
        timeout: int = 30,
    ):
        self.base_url = (
            base_url or os.environ.get("JENKINS_URL", "")
        ).rstrip("/")
        self.username = username or os.environ.get("JENKINS_USER", "")
        self.api_token = api_token or os.environ.get("JENKINS_API_TOKEN", "")
        self.timeout = timeout

        if not self.base_url:
            raise JenkinsError(
                "Jenkins URL not configured. Set JENKINS_URL environment variable "
                "or pass base_url parameter."
            )

        # Set up session with retry logic
        self._session = requests.Session()

        if self.username and self.api_token:
            self._session.auth = (self.username, self.api_token)
        else:
            logger.warning(
                "Jenkins credentials not fully configured. "
                "Set JENKINS_USER and JENKINS_API_TOKEN for authenticated access."
            )

        retry_strategy = Retry(
            total=max_retries,
            backoff_factor=backoff_factor,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

        # Common headers
        self._session.headers.update({
            "Accept": "application/json",
        })

    def _request(self, url: str, params: Optional[dict] = None) -> dict | list | str:
        """
        Make a GET request to Jenkins API with error handling.

        Returns parsed JSON or raw text depending on content type.
        """
        try:
            logger.debug(f"GET {url}")
            resp = self._session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()

            content_type = resp.headers.get("Content-Type", "")
            if "application/json" in content_type:
                return resp.json()
            return resp.text

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "unknown"
            raise JenkinsError(
                f"Jenkins API error (HTTP {status}) for {url}: {e}"
            ) from e
        except requests.exceptions.ConnectionError as e:
            raise JenkinsError(
                f"Cannot connect to Jenkins at {self.base_url}: {e}"
            ) from e
        except requests.exceptions.Timeout as e:
            raise JenkinsError(
                f"Jenkins API request timed out for {url}: {e}"
            ) from e
        except requests.exceptions.RequestException as e:
            raise JenkinsError(
                f"Jenkins API request failed for {url}: {e}"
            ) from e

    def _build_job_url(self, job_path: str) -> str:
        """
        Build the full Jenkins URL for a job.

        Handles both flat and folder-based job paths:
            "my-job"           → {base}/job/my-job
            "folder/my-job"    → {base}/job/folder/job/my-job
        """
        parts = job_path.strip("/").split("/")
        encoded_parts = [url_quote(part, safe="") for part in parts]
        path = "/job/".join(encoded_parts)
        return f"{self.base_url}/job/{path}"

    def get_build_info(
        self,
        job_path: str,
        build_number: int | str,
        category: str = "",
    ) -> BuildInfo:
        """
        Fetch build information from Jenkins.

        Args:
            job_path: Job path (e.g., "sandbox/test_job" or "test_job")
            build_number: Build number or "lastBuild"/"lastFailedBuild"
            category: Category name for the BuildInfo model

        Returns:
            BuildInfo with job details and build status.
        """
        job_url = self._build_job_url(job_path)
        url = f"{job_url}/{build_number}/api/json"
        params = {
            "tree": "number,url,result,timestamp,duration,fullDisplayName"
        }

        data = self._request(url, params)

        if not isinstance(data, dict):
            raise JenkinsError(f"Unexpected response type for build info: {type(data)}")

        # Extract job name from full display name or path
        full_name = data.get("fullDisplayName", "")
        job_name = full_name.split(" #")[0] if " #" in full_name else job_path.split("/")[-1]

        return BuildInfo(
            job_name=job_name,
            build_number=data.get("number", build_number),
            url=data.get("url", job_url),
            status=data.get("result", "UNKNOWN") or "IN_PROGRESS",
            category=category,
            timestamp=data.get("timestamp"),
            duration=data.get("duration"),
            job_path=job_path,
        )

    def get_stage_results(
        self,
        job_path: str,
        build_number: int | str,
    ) -> list[StageResult]:
        """
        Fetch pipeline stage results using the Pipeline Steps (Workflow) API.

        Uses the /wfapi/describe endpoint which provides stage-level status
        for pipeline builds.

        Args:
            job_path: Job path in Jenkins
            build_number: Build number

        Returns:
            List of StageResult in pipeline execution order.
        """
        job_url = self._build_job_url(job_path)
        url = f"{job_url}/{build_number}/wfapi/describe"

        data = self._request(url)
        if not isinstance(data, dict):
            raise JenkinsError(f"Unexpected wfapi response type: {type(data)}")

        stages = data.get("stages", [])
        results = []

        for stage_data in stages:
            status_str = stage_data.get("status", "UNKNOWN")
            results.append(StageResult(
                name=stage_data.get("name", "Unknown Stage"),
                stage_id=str(stage_data.get("id", "")),
                status=StageStatus.from_string(status_str),
                duration_ms=stage_data.get("durationMillis", 0),
            ))

        logger.info(
            f"Retrieved {len(results)} stages for {job_path} #{build_number}: "
            f"{[s.name for s in results]}"
        )
        return results

    def get_stage_log(
        self,
        job_path: str,
        build_number: int | str,
        stage_id: str,
    ) -> str:
        """
        Fetch the full log for a specific pipeline stage.

        Uses the Pipeline Steps API to get the log for a given node (stage).

        Args:
            job_path: Job path in Jenkins
            build_number: Build number
            stage_id: Stage ID from wfapi

        Returns:
            Full stage log as a string.
        """
        job_url = self._build_job_url(job_path)

        # Attempt the execution/node API first (more reliable for large logs)
        url = f"{job_url}/{build_number}/execution/node/{stage_id}/wfapi/log"

        try:
            data = self._request(url)
            if isinstance(data, dict):
                # The wfapi/log endpoint returns {"nodeId": ..., "text": "..."}
                log_text = data.get("text", "")
            else:
                log_text = str(data)
        except JenkinsError:
            # Fallback: try consoleText for the whole build (less ideal)
            logger.warning(
                f"Stage-level log not available for stage {stage_id}, "
                f"falling back to full build log"
            )
            url = f"{job_url}/{build_number}/consoleText"
            log_text = str(self._request(url))

        logger.debug(f"Retrieved {len(log_text)} chars of log for stage {stage_id}")
        return log_text

    def get_full_build_log(
        self,
        job_path: str,
        build_number: int | str,
    ) -> str:
        """
        Fetch the full console log for a build.

        Warning: This can be very large (50,000+ lines). Use get_stage_log
        when possible to reduce data transfer.
        """
        job_url = self._build_job_url(job_path)
        url = f"{job_url}/{build_number}/consoleText"
        return str(self._request(url))

    def get_latest_build_number(
        self,
        job_path: str,
        status_filter: Optional[str] = None,
    ) -> int:
        """
        Get the latest (or latest failed) build number for a job.

        Args:
            job_path: Job path in Jenkins
            status_filter: If "FAILURE", returns lastFailedBuild number

        Returns:
            Build number.
        """
        job_url = self._build_job_url(job_path)

        if status_filter == "FAILURE":
            url = f"{job_url}/lastFailedBuild/api/json"
        else:
            url = f"{job_url}/lastBuild/api/json"

        data = self._request(url, params={"tree": "number"})
        if not isinstance(data, dict):
            raise JenkinsError(f"Unexpected response for latest build: {type(data)}")

        build_num = data.get("number")
        if build_num is None:
            raise JenkinsError(f"No build found for {job_path}")
        return int(build_num)

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
