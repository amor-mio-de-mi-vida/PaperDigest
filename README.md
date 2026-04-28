# Daily AI Papers Digest (Hugging Face + arXiv -> Slack)

用 GitHub Actions 定时抓取 Hugging Face Trending Papers 与 arXiv 最新 AI 论文，按配置文件中的研究领域并行分类、去重、总结，并分别推送到对应 Slack channel。

## 1) 本地运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
set -a && source .env && set +a
python scripts/daily_ai_digest.py
```

本地测试但不发送 Slack、不调用 LLM：

```bash
set -a && source .env && set +a
DRY_RUN=1 DISABLE_LLM=1 ARXIV_MAX_RESULTS=80 HF_MAX_RESULTS=8 HF_MAX_PER_DOMAIN=8 python scripts/daily_ai_digest.py
```

## 2) 配置领域

默认配置文件是 `config/domains.json`，也可以用环境变量指定：

```bash
DIGEST_CONFIG=config/my-domains.json python scripts/daily_ai_digest.py
```

配置结构：

```json
{
  "arxiv_categories": ["cs.AI", "cs.LG", "cs.CL", "cs.CV", "cs.RO", "stat.ML"],
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

新增领域只需要在 `domains` 里加一项：

- `id`：稳定 ID，建议英文小写短横线
- `name`：Slack 简报和报告中的显示名
- `filename`：上传到 Slack 的 Markdown 文件名前缀
- `slack_channel_env`：该领域对应的 Slack channel 环境变量
- `keywords`：用于从当天候选池中筛选该领域 paper 的关键词

`default_channel_env` 是 fallback channel。如果某个领域没有配置 `slack_channel_env` 或环境变量为空，会使用它。

## 3) Slack 配置

1. 在 [Slack API Apps](https://api.slack.com/apps) 创建 app。
2. 添加 `chat:write` 和 `files:write` scope。
3. 安装到 workspace，拿到 `Bot User OAuth Token`（`xoxb-...`）。
4. 把 bot 拉进目标 channel，复制 channel id。

`.env` 中常用配置：

```bash
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_CHANNEL_ID=C0123456789
SLACK_CHANNEL_AGENT=C0123456789
```

## 4) 输出格式

每个领域会发送两样东西：

- 一条 Slack mrkdwn 精简研究简报
- 一个上传到 Slack channel 的 Markdown 文件

Slack 简报包含：

- `Daily AI Research Brief - YYYY-MM-DD`
- `🔥 今日推荐精读 Top 3`
- `🧠 今日有价值研究点`：领域痛点、研究方法、研究结果
- `📎 全量论文链接与摘要`：提示已上传的 Markdown 文件名

Slack 简报不会包含大段原始摘要。Markdown 文件会写入 `REPORT_DIR/YYYY-MM-DD/*-papers.md` 并上传到对应 Slack channel；文件中只包含当天论文链接和原始摘要，不包含分析和排序。

脚本会先抓取最近 arXiv AI 大类论文并按 `TIMEZONE` 转换后的本地日期过滤，尽量覆盖当天全部论文。Hugging Face Trending Papers 会先转换为对应 arXiv 论文，使用 arXiv 链接和 arXiv 摘要，并与 arXiv New Papers 去重。

## 5) GitHub Secrets / Variables

Secrets：

- `SLACK_BOT_TOKEN`
- 每个领域配置中 `slack_channel_env` 对应的 channel secret，例如 `SLACK_CHANNEL_AGENT`
- `SLACK_CHANNEL_ID`：可选 fallback channel
- `DEEPSEEK_API_KEY`：推荐，用于研究总结
- `DEEPSEEK_MODEL`：可选，默认 `deepseek-chat`
- `DEEPSEEK_BASE_URL`：可选，默认 `https://api.deepseek.com/v1/chat/completions`
- `OPENAI_API_KEY`：可选回退方案
- `OPENAI_MODEL`：可选，默认 `gpt-4.1-mini`

Variables：

- `DIGEST_CONFIG`：默认 `config/domains.json`
- `TIMEZONE`：默认 `Asia/Singapore`
- `ARXIV_MAX_RESULTS`：默认 `2000`，用于抓取最近 arXiv AI 大类论文后按本地日期过滤，建议保持较高以覆盖当天全部论文
- `HF_MAX_RESULTS`：默认 `120`
- `HF_MAX_PER_DOMAIN`：默认 `100`
- `ANALYSIS_MAX_PAPERS`：默认 `40`，每个领域送入模型做简报分析的上限；Slack Markdown 文件仍保留该领域全量论文
- `DOMAIN_WORKERS`：默认 `4`
- `REPORT_DIR`：默认 `reports`
- `DISABLE_LLM`：设为 `1` 时跳过 LLM，用规则 fallback，适合测试抓取和分类

## 6) 定时任务

工作流文件：`.github/workflows/daily-ai-digest.yml`

- 默认每天新加坡/北京时间 09:00 触发
- 支持 `workflow_dispatch` 手动触发测试
- 生成的 `reports/` 会作为 GitHub Actions artifact 上传，同时每个领域的 `*-papers.md` 会上传到对应 Slack channel
