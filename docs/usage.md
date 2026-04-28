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
DRY_RUN=1 DISABLE_LLM=1 UPDATE_README=1 ARXIV_MAX_RESULTS=80 HF_MAX_RESULTS=8 HF_MAX_PER_DOMAIN=8 python scripts/daily_ai_digest.py
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
      "slack_channel_env": "SLACK_CHANNEL_AGENT",
      "keywords": ["llm agent", "multi-agent", "tool use"]
    }
  ]
}
```

## GitHub Actions

Secrets:

- `SLACK_BOT_TOKEN`
- `SLACK_CHANNEL_ID` or per-domain channel secrets such as `SLACK_CHANNEL_AGENT`
- `DEEPSEEK_API_KEY` or `OPENAI_API_KEY`

Variables:

- `DIGEST_CONFIG=config/domains.json`
- `TIMEZONE=Asia/Singapore`
- `ARXIV_MAX_RESULTS=2000`
- `HF_MAX_RESULTS=120`
- `HF_MAX_PER_DOMAIN=100`
- `ANALYSIS_MAX_PAPERS=40`
- `UPDATE_README=1`
- `MAX_README_PAPERS_PER_DOMAIN=20`

The workflow writes `archive/YYYY-MM-DD.md`, updates `README.md`, uploads Slack Markdown files, sends Slack briefs, commits generated GitHub archive updates, and uploads `reports/` as an artifact.
