# Usage

## Local Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
set -a && source .env && set +a
python scripts/daily_ai_digest.py
```

Quick local test without Slack or LLM:

```bash
set -a && source .env && set +a
DRY_RUN=1 DISABLE_LLM=1 UPDATE_ARCHIVE=1 ARXIV_MAX_RESULTS=80 HF_MAX_RESULTS=8 HF_MAX_PER_DOMAIN=8 python scripts/daily_ai_digest.py
```

## Configure Topics

Edit `config/domains.json`:

```json
{
  "arxiv_categories": ["cs.AI", "cs.LG", "cs.CL", "cs.CV"],
  "default_channel_env": "SLACK_CHANNEL_ID",
  "domains": [
    {
      "id": "agent",
      "name": "Agent",
      "filename": "agent",
            "keywords": ["llm agent", "multi-agent", "tool use"]
    }
  ]
}
```

## GitHub Actions

Secrets:

- `SLACK_BOT_TOKEN`
- `SLACK_CHANNEL_ID`: unified Slack channel for the daily Canvas and all domain briefs
- `DEEPSEEK_API_KEY` or `OPENAI_API_KEY`

Variables:

- `DIGEST_CONFIG=config/domains.json`
- `TIMEZONE=Asia/Singapore`
- `ARXIV_MAX_RESULTS=2000`
- `ARXIV_PAGE_SIZE=500`
- `ARXIV_REQUEST_DELAY_SECONDS=3.5`
- `ARXIV_RETRIES=6`
- `ARXIV_RETRY_BASE_SECONDS=20`
- `ARXIV_RETRY_MAX_SECONDS=300`
- `HF_MAX_RESULTS=120`
- `HF_MAX_PER_DOMAIN=100`
- `ANALYSIS_MAX_PAPERS=40`
- `UPDATE_ARCHIVE=1`
- `DAILY_FILE_NAME=daily-ai-paper-digest.md`
- `SLACK_RETRIES=4`
- `SLACK_RETRY_BASE_SECONDS=3`
- `SLACK_RETRY_MAX_SECONDS=60`

The workflow writes one daily Markdown report under `reports/YYYY-MM-DD/` with navigation plus all paper links and complete original abstracts, creates one Slack Canvas from that report in the configured channel, sends per-domain Slack briefs to the channel, optionally copies the same content to `archive/YYYY-MM-DD.md`, commits archive updates, and uploads `reports/` as an artifact.
