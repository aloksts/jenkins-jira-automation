"""
Configuration loader for Jenkins-Jira Automation.

Loads and validates YAML configuration files, merges category defaults
with job-specific overrides, and resolves POC mappings.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

import yaml

from .models import JobConfig

logger = logging.getLogger(__name__)

# Project root — resolved relative to this file's location
_SRC_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SRC_DIR.parent
_CONFIG_DIR = _PROJECT_ROOT / "config"
_CATEGORIES_DIR = _CONFIG_DIR / "categories"


class ConfigError(Exception):
    """Raised when configuration is invalid or missing."""
    pass


def _validate_email(email: str) -> bool:
    """Basic email validation."""
    return bool(re.match(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$', email))


def _load_yaml(filepath: Path) -> dict:
    """Load and parse a YAML file."""
    if not filepath.exists():
        raise ConfigError(f"Configuration file not found: {filepath}")
    try:
        with open(filepath, 'r') as f:
            data = yaml.safe_load(f)
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ConfigError(f"Expected YAML dict in {filepath}, got {type(data).__name__}")
        return data
    except yaml.YAMLError as e:
        raise ConfigError(f"Failed to parse YAML file {filepath}: {e}")


def get_config_dir() -> Path:
    """Return the config directory path. Override with CONFIG_DIR env var."""
    override = os.environ.get("CONFIG_DIR")
    if override:
        return Path(override)
    return _CONFIG_DIR


def load_settings() -> dict:
    """
    Load global settings from config/settings.yaml.

    Returns the full settings dict with defaults applied for missing keys.
    """
    config_dir = get_config_dir()
    settings_path = config_dir / "settings.yaml"
    settings = _load_yaml(settings_path)

    # Apply defaults
    jira = settings.setdefault("jira", {})
    jira.setdefault("project_key", "JENKINS")
    jira.setdefault("issue_type", "Bug")
    jira.setdefault("default_priority", "High")
    jira.setdefault("base_labels", ["jenkins-automation", "auto-filed"])

    jenkins = settings.setdefault("jenkins", {})
    jenkins.setdefault("base_url", "https://jenkins.example.com")
    jenkins.setdefault("log_tail_lines", 50)
    jenkins.setdefault("max_retries", 3)
    jenkins.setdefault("retry_backoff_factor", 2)

    settings.setdefault("link_patterns", {})
    settings.setdefault("test_failure_patterns", [])
    settings.setdefault("ticket_templates", {})
    settings.setdefault("duplicate_check", {"enabled": True})

    return settings


def load_category(name: str) -> dict:
    """
    Load a single category configuration file.

    Args:
        name: Category name (matches filename without .yaml extension)

    Returns:
        Parsed category configuration dict.

    Raises:
        ConfigError: If the file doesn't exist or is invalid.
    """
    config_dir = get_config_dir()
    cat_path = config_dir / "categories" / f"{name}.yaml"
    cat_data = _load_yaml(cat_path)

    # Validate required fields
    if "category" not in cat_data:
        raise ConfigError(f"Category config {cat_path} missing 'category' field")
    if "default_stages" not in cat_data or not cat_data["default_stages"]:
        raise ConfigError(f"Category config {cat_path} missing or empty 'default_stages'")
    if "default_stage_poc" not in cat_data:
        raise ConfigError(f"Category config {cat_path} missing 'default_stage_poc'")
    if not _validate_email(cat_data["default_stage_poc"]):
        raise ConfigError(
            f"Invalid default_stage_poc email in {cat_path}: {cat_data['default_stage_poc']}"
        )

    # Ensure jobs is a dict
    cat_data.setdefault("jobs", {})
    if cat_data["jobs"] is None:
        cat_data["jobs"] = {}
    cat_data.setdefault("stage_poc_overrides", {})
    if cat_data["stage_poc_overrides"] is None:
        cat_data["stage_poc_overrides"] = {}
    cat_data.setdefault("pattern", "")

    return cat_data


def load_all_categories() -> dict[str, dict]:
    """
    Discover and load all category configuration files.

    Returns:
        Dict mapping category name → category config dict.
    """
    config_dir = get_config_dir()
    cat_dir = config_dir / "categories"

    if not cat_dir.exists():
        raise ConfigError(f"Categories directory not found: {cat_dir}")

    categories = {}
    for yaml_file in sorted(cat_dir.glob("*.yaml")):
        # Skip template files
        if yaml_file.stem.startswith("_"):
            continue
        try:
            cat_data = load_category(yaml_file.stem)
            cat_name = cat_data["category"]
            categories[cat_name] = cat_data
            logger.debug(f"Loaded category: {cat_name} ({len(cat_data.get('jobs', {}))} jobs)")
        except ConfigError as e:
            logger.warning(f"Skipping invalid category file {yaml_file.name}: {e}")

    if not categories:
        raise ConfigError(f"No valid category configurations found in {cat_dir}")

    logger.info(f"Loaded {len(categories)} categories: {list(categories.keys())}")
    return categories


def resolve_job_config(
    category_data: dict,
    job_name: str,
    settings: Optional[dict] = None,
) -> JobConfig:
    """
    Resolve the full configuration for a specific job by merging
    category defaults with job-specific overrides.

    Args:
        category_data: Parsed category config dict.
        job_name: Name of the job within this category.
        settings: Optional global settings dict.

    Returns:
        Fully resolved JobConfig with all stages and POC mappings.

    Raises:
        ConfigError: If the job is not found in the category config.
    """
    cat_name = category_data["category"]
    default_stages = list(category_data["default_stages"])
    default_poc = category_data["default_stage_poc"]
    stage_poc_overrides = category_data.get("stage_poc_overrides", {}) or {}

    jobs = category_data.get("jobs", {}) or {}
    job_data = jobs.get(job_name)

    if job_data is None:
        raise ConfigError(
            f"Job '{job_name}' not found in category '{cat_name}'. "
            f"Available jobs: {list(jobs.keys())}"
        )

    # Job-specific stages (appended after defaults)
    job_stages = job_data.get("stages", []) or []
    all_stages = default_stages + job_stages

    # Build POC map: every stage gets a POC
    stage_poc_map: dict[str, str] = {}

    # 1. Default POC for all default stages
    for stage in default_stages:
        stage_poc_map[stage] = default_poc

    # 2. Category-level stage overrides
    for stage, poc in stage_poc_overrides.items():
        if stage in stage_poc_map:
            stage_poc_map[stage] = poc

    # 3. Job-specific stages get the job's stage_poc
    job_poc = job_data.get("stage_poc", default_poc)
    for stage in job_stages:
        stage_poc_map[stage] = job_poc

    # Validate all POC emails
    for stage, poc in stage_poc_map.items():
        if not _validate_email(poc):
            logger.warning(f"Invalid POC email for stage '{stage}': {poc}")

    ramdump_required = job_data.get("ramdump_required", False)

    return JobConfig(
        category=cat_name,
        job_name=job_name,
        pattern=category_data.get("pattern", ""),
        all_stages=all_stages,
        default_stages=default_stages,
        job_stages=job_stages,
        stage_poc_map=stage_poc_map,
        ramdump_required=ramdump_required,
        default_poc=default_poc,
    )


def get_all_job_names(category_data: dict) -> list[str]:
    """Return all job names defined in a category."""
    jobs = category_data.get("jobs", {})
    return list(jobs.keys()) if jobs else []


def find_category_for_job(job_name: str, categories: Optional[dict] = None) -> Optional[str]:
    """
    Auto-detect which category a job belongs to based on job name patterns.

    Args:
        job_name: Jenkins job name.
        categories: Pre-loaded categories dict (if None, loads all).

    Returns:
        Category name or None if not found.
    """
    if categories is None:
        categories = load_all_categories()

    # First: exact match in jobs dict
    for cat_name, cat_data in categories.items():
        jobs = cat_data.get("jobs", {}) or {}
        if job_name in jobs:
            return cat_name

    # Second: pattern match on job name
    for cat_name, cat_data in categories.items():
        pattern = cat_data.get("pattern", "")
        if pattern and job_name.startswith(pattern):
            return cat_name

    return None
