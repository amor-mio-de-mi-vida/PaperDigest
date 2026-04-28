# PaperDigest

Configurable daily AI paper digest for arXiv + Hugging Face Trending Papers.

PaperDigest can:

- Fetch recent arXiv papers and filter them by local date.
- Convert Hugging Face Trending Papers to arXiv papers when possible.
- Deduplicate by arXiv ID, title, and URL.
- Classify papers into configurable research domains using `config/domains.json`.
- Generate one daily Markdown file containing all domain briefs and all paper links/abstracts.
- Upload that daily file to Slack once.
- Send separate concise Slack briefs for each configured domain.

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
set -a && source .env && set +a
python scripts/daily_ai_digest.py
```

Fast local test without Slack or LLM:

```bash
DRY_RUN=1 DISABLE_LLM=1 ARXIV_MAX_RESULTS=80 HF_MAX_RESULTS=8 python scripts/daily_ai_digest.py
```

## Configure Domains

Edit [`config/domains.json`](config/domains.json):

```json
{
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

## Documentation

See [docs/usage.md](docs/usage.md) for GitHub Actions setup, environment variables, and operation notes.
