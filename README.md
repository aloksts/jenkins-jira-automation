# Jenkins-Jira Automation

Automatically create Jira tickets when Jenkins pipeline jobs fail. Detects the first failing stage, extracts relevant links (ramdump, report, VM), and assigns the ticket to the correct POC based on YAML configuration.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set environment variables
export JENKINS_URL=https://your-jenkins.com
export JENKINS_USER=your-user
export JENKINS_API_TOKEN=your-token
export JIRA_URL=https://your-jira.atlassian.net
export JIRA_USER=your-email@example.com
export JIRA_API_TOKEN=your-jira-token

# 3. Validate your config
python main.py --validate-config

# 4. Dry run (preview without creating tickets)
python main.py --dry-run --category sandbox --job ngkmd_410_game_custom_test --build 123

# 5. Create ticket for real
python main.py --category sandbox --job ngkmd_410_game_custom_test --build 123
```

## Usage

```bash
# Process a specific build
python main.py --category sandbox --job my_job --build 123

# Process latest failed build for a job
python main.py --category sandbox --job my_job

# Process all jobs in a category
python main.py --category sandbox

# Scan all categories and all jobs
python main.py --scan-all

# Dry run with verbose logging
python main.py --dry-run -v --category sandbox --job my_job --build 123

# JSON output (for piping to other tools)
python main.py --json --category sandbox --job my_job --build 123
```

## Configuration

### Global Settings — `config/settings.yaml`

Contains Jira project key, issue type, ticket templates, link extraction patterns, and duplicate detection settings.

### Category Files — `config/categories/<name>.yaml`

Each category defines:
- **`default_stages`**: Common stages for all jobs in this category
- **`default_stage_poc`**: Default assignee email 
- **`stage_poc_overrides`**: Override assignee for specific stages
- **`jobs`**: Per-job config with extra stages, POC, and ramdump flag

### Adding a New Category

```bash
cp config/categories/_template.yaml config/categories/my_category.yaml
# Edit the file with your stages, POCs, and jobs
python main.py --validate-config  # Verify it's valid
```

### POC Resolution Priority

When a stage fails, the assignee is resolved in this order:
1. **Job-specific `stage_poc`** (for job-defined stages)
2. **Category `stage_poc_overrides`** (for default stages)
3. **Category `default_stage_poc`** (fallback)

## Jenkins Integration

Add to your pipeline's `post` block:

```groovy
post {
    failure {
        sh """
            cd /opt/jenkins-jira-automation
            python3 main.py \
                --category ${env.JIRA_CATEGORY} \
                --job ${env.JOB_NAME} \
                --build ${env.BUILD_NUMBER}
        """
    }
}
```

See `Jenkinsfile.snippet` for more options including shared library usage.

## Running Tests

```bash
pip install pytest
pytest tests/ -v
```

## Project Structure

```
├── config/
│   ├── settings.yaml              # Global settings
│   └── categories/
│       ├── sandbox.yaml           # Category configs
│       ├── kmdx.yaml
│       └── _template.yaml         # Template for new categories
├── src/
│   ├── models.py                  # Data classes
│   ├── config_loader.py           # YAML config loading & validation
│   ├── jenkins_client.py          # Jenkins REST API client
│   ├── log_parser.py              # Log analysis & link extraction
│   ├── jira_client.py             # Jira REST API client
│   ├── ticket_builder.py          # Ticket content generation
│   └── orchestrator.py            # Main workflow engine
├── tests/                         # Pytest test suite
├── main.py                        # CLI entry point
├── Jenkinsfile.snippet            # Jenkins integration examples
└── requirements.txt
```
