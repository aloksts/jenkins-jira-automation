"""
Tests for config_loader module.

Validates YAML loading, default merging, POC resolution, and error handling.
"""

import os
import pytest
import tempfile
import shutil
from pathlib import Path

# Ensure project root is in path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config_loader import (
    ConfigError,
    load_settings,
    load_category,
    load_all_categories,
    resolve_job_config,
    find_category_for_job,
    get_all_job_names,
)


@pytest.fixture
def temp_config_dir(tmp_path):
    """Create a temporary config directory with test fixtures."""
    config_dir = tmp_path / "config"
    categories_dir = config_dir / "categories"
    categories_dir.mkdir(parents=True)

    # Create settings.yaml
    settings = config_dir / "settings.yaml"
    settings.write_text("""
jira:
  project_key: "TEST"
  issue_type: "Bug"
  default_priority: "High"
  base_labels: ["auto-test"]

jenkins:
  base_url: "https://jenkins.test.com"
  log_tail_lines: 50
  max_retries: 2
  retry_backoff_factor: 1
""")

    # Create sandbox category
    sandbox = categories_dir / "sandbox.yaml"
    sandbox.write_text("""
category: sandbox
pattern: "test_"

default_stages:
  - Environment Setup
  - Build
  - Test Execution
  - Post Actions

default_stage_poc: "default@test.com"

stage_poc_overrides:
  "Build": "build-team@test.com"

jobs:
  test_job_1:
    stages:
      - "Custom Test"
      - "Report Gen"
    stage_poc: "custom@test.com"
    ramdump_required: true

  test_job_2:
    stages:
      - "Another Test"
    stage_poc: "another@test.com"
    ramdump_required: false
""")

    # Create _template (should be skipped)
    template = categories_dir / "_template.yaml"
    template.write_text("""
category: template
default_stages: [Stage1]
default_stage_poc: "template@test.com"
""")

    # Set env var so loader uses our temp dir
    os.environ["CONFIG_DIR"] = str(config_dir)
    yield config_dir
    del os.environ["CONFIG_DIR"]


class TestLoadSettings:
    def test_loads_settings(self, temp_config_dir):
        settings = load_settings()
        assert settings["jira"]["project_key"] == "TEST"
        assert settings["jira"]["issue_type"] == "Bug"
        assert settings["jenkins"]["log_tail_lines"] == 50

    def test_applies_defaults(self, temp_config_dir):
        settings = load_settings()
        # These should have defaults applied
        assert "link_patterns" in settings
        assert "ticket_templates" in settings
        assert "duplicate_check" in settings

    def test_missing_settings_file(self, tmp_path):
        os.environ["CONFIG_DIR"] = str(tmp_path)
        try:
            with pytest.raises(ConfigError, match="not found"):
                load_settings()
        finally:
            del os.environ["CONFIG_DIR"]


class TestLoadCategory:
    def test_loads_sandbox(self, temp_config_dir):
        cat = load_category("sandbox")
        assert cat["category"] == "sandbox"
        assert cat["pattern"] == "test_"
        assert len(cat["default_stages"]) == 4
        assert "test_job_1" in cat["jobs"]

    def test_missing_category(self, temp_config_dir):
        with pytest.raises(ConfigError, match="not found"):
            load_category("nonexistent")

    def test_skips_template(self, temp_config_dir):
        categories = load_all_categories()
        assert "_template" not in categories
        assert "template" not in categories

    def test_loads_all_categories(self, temp_config_dir):
        categories = load_all_categories()
        assert "sandbox" in categories
        assert len(categories) >= 1


class TestResolveJobConfig:
    def test_resolves_job_with_overrides(self, temp_config_dir):
        cat = load_category("sandbox")
        config = resolve_job_config(cat, "test_job_1")

        assert config.category == "sandbox"
        assert config.job_name == "test_job_1"
        assert config.ramdump_required is True
        # Should have default stages + job stages
        assert len(config.all_stages) == 6  # 4 defaults + 2 custom
        assert "Custom Test" in config.all_stages
        assert "Report Gen" in config.all_stages

    def test_poc_resolution_priority(self, temp_config_dir):
        cat = load_category("sandbox")
        config = resolve_job_config(cat, "test_job_1")

        # Default stages get default POC
        assert config.stage_poc_map["Environment Setup"] == "default@test.com"
        # Build has a category-level override
        assert config.stage_poc_map["Build"] == "build-team@test.com"
        # Custom stages get job-level POC
        assert config.stage_poc_map["Custom Test"] == "custom@test.com"
        assert config.stage_poc_map["Report Gen"] == "custom@test.com"

    def test_nonexistent_job(self, temp_config_dir):
        cat = load_category("sandbox")
        with pytest.raises(ConfigError, match="not found"):
            resolve_job_config(cat, "nonexistent_job")

    def test_job_without_ramdump(self, temp_config_dir):
        cat = load_category("sandbox")
        config = resolve_job_config(cat, "test_job_2")
        assert config.ramdump_required is False


class TestFindCategoryForJob:
    def test_exact_match(self, temp_config_dir):
        categories = load_all_categories()
        result = find_category_for_job("test_job_1", categories)
        assert result == "sandbox"

    def test_pattern_match(self, temp_config_dir):
        categories = load_all_categories()
        # "test_" pattern should match any job starting with test_
        result = find_category_for_job("test_unknown_job", categories)
        assert result == "sandbox"

    def test_no_match(self, temp_config_dir):
        categories = load_all_categories()
        result = find_category_for_job("completely_different", categories)
        assert result is None


class TestGetAllJobNames:
    def test_returns_job_names(self, temp_config_dir):
        cat = load_category("sandbox")
        names = get_all_job_names(cat)
        assert "test_job_1" in names
        assert "test_job_2" in names
        assert len(names) == 2
