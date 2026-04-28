import hashlib
import html
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup


ARXIV_URL = "https://export.arxiv.org/api/query"
HF_PAPERS_URL = "https://huggingface.co/papers"
SLACK_API_URL = "https://slack.com/api/chat.postMessage"
SLACK_UPLOAD_URL_API = "https://slack.com/api/files.getUploadURLExternal"
SLACK_COMPLETE_UPLOAD_API = "https://slack.com/api/files.completeUploadExternal"
VALUE_LABELS = {"值得精读", "值得扫读", "可以忽略"}


@dataclass(frozen=True)
class DomainConfig:
    id: str
    name: str
    keywords: List[str]
    slack_channel_env: str = ""
    filename: str = ""


@dataclass(frozen=True)
class DigestConfig:
    arxiv_categories: List[str]
    default_channel_env: str
    domains: List[DomainConfig]


@dataclass
class RuntimeConfig:
    config_path: Path
    timezone: str
    date_str: str
    arxiv_max_results: int
    hf_max_results: int
    hf_max_per_domain: int
    analysis_max_papers: int
    domain_workers: int
    report_dir: Path
    dry_run: bool
    disable_llm: bool
    update_archive: bool
    archive_dir: Path
    daily_file_name: str


@dataclass
class Paper:
    source: str
    title: str
    abstract: str
    url: str
    published: str = ""
    arxiv_id: str = ""


def clean_text(text: str) -> str:
    text = html.unescape(text or "")
    return re.sub(r"\s+", " ", text).strip()


def normalize_title(title: str) -> str:
    title = clean_text(title).lower()
    title = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", title)
    return re.sub(r"\s+", " ", title).strip()


def safe_filename(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", text).strip("-").lower() or "domain"


def truthy_env(name: str) -> bool:
    return os.getenv(name, "").lower() in {"1", "true", "yes", "on"}


def load_digest_config(path: Path) -> DigestConfig:
    data = json.loads(path.read_text(encoding="utf-8"))
    domains = []
    for item in data.get("domains", []):
        keywords = [clean_text(keyword).lower() for keyword in item.get("keywords", []) if clean_text(keyword)]
        if not item.get("name") or not keywords:
            raise ValueError("Each domain config needs name and at least one keyword.")
        domain_id = item.get("id") or safe_filename(item["name"])
        domains.append(
            DomainConfig(
                id=domain_id,
                name=item["name"],
                keywords=keywords,
                slack_channel_env=item.get("slack_channel_env", ""),
                filename=item.get("filename") or domain_id,
            )
        )
    if not domains:
        raise ValueError("Config must define at least one domain.")

    categories = data.get("arxiv_categories") or ["cs.AI", "cs.LG", "cs.CL", "cs.CV", "stat.ML"]
    return DigestConfig(
        arxiv_categories=categories,
        default_channel_env=data.get("default_channel_env", "SLACK_CHANNEL_ID"),
        domains=domains,
    )


def load_runtime_config() -> RuntimeConfig:
    timezone = os.getenv("TIMEZONE", "Asia/Singapore")
    date_str = os.getenv("DIGEST_DATE") or datetime.now(ZoneInfo(timezone)).strftime("%Y-%m-%d")
    return RuntimeConfig(
        config_path=Path(os.getenv("DIGEST_CONFIG", "config/domains.json")),
        timezone=timezone,
        date_str=date_str,
        arxiv_max_results=int(os.getenv("ARXIV_MAX_RESULTS", "2000")),
        hf_max_results=int(os.getenv("HF_MAX_RESULTS", "120")),
        hf_max_per_domain=int(os.getenv("HF_MAX_PER_DOMAIN", "100")),
        analysis_max_papers=int(os.getenv("ANALYSIS_MAX_PAPERS", "40")),
        domain_workers=int(os.getenv("DOMAIN_WORKERS", "4")),
        report_dir=Path(os.getenv("REPORT_DIR", "reports")),
        dry_run=truthy_env("DRY_RUN"),
        disable_llm=truthy_env("DISABLE_LLM"),
        update_archive=truthy_env("UPDATE_ARCHIVE") or truthy_env("UPDATE_README"),
        archive_dir=Path(os.getenv("ARCHIVE_DIR", "archive")),
        daily_file_name=os.getenv("DAILY_FILE_NAME", "daily-ai-paper-digest.md"),
    )


def arxiv_category_query(categories: Iterable[str]) -> str:
    return "(" + " OR ".join(f"cat:{category}" for category in categories) + ")"


def extract_arxiv_id(url: str) -> str:
    arxiv_match = re.search(r"arxiv\.org/(?:abs|pdf)/([^?#]+)", url)
    if arxiv_match:
        return normalize_arxiv_id(arxiv_match.group(1).replace(".pdf", ""))
    hf_match = re.search(r"huggingface\.co/papers/([^/?#]+)", url)
    if hf_match:
        return normalize_arxiv_id(hf_match.group(1))
    return ""


def normalize_arxiv_id(raw_id: str) -> str:
    paper_id = clean_text(raw_id).strip().rstrip("/")
    paper_id = paper_id.removesuffix(".pdf")
    modern = r"\d{4}\.\d{4,5}(?:v\d+)?"
    legacy = r"[a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?"
    if re.fullmatch(modern, paper_id) or re.fullmatch(legacy, paper_id):
        return paper_id
    return ""


def arxiv_id_base(paper_id: str) -> str:
    return re.sub(r"v\d+$", "", paper_id)


def parse_arxiv_entry(entry, source: str = "arXiv") -> Paper:
    url = entry.get("id", "").replace("http://", "https://")
    return Paper(
        source=source,
        title=clean_text(entry.get("title", "")),
        abstract=clean_text(entry.get("summary", "")),
        url=url,
        published=entry.get("published", ""),
        arxiv_id=extract_arxiv_id(url),
    )


def fetch_recent_arxiv(categories: Iterable[str], max_results: int) -> List[Paper]:
    resp = requests.get(
        ARXIV_URL,
        params={
            "search_query": arxiv_category_query(categories),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "start": "0",
            "max_results": str(max_results),
        },
        timeout=60,
    )
    resp.raise_for_status()
    feed = feedparser.parse(resp.text)
    return [parse_arxiv_entry(entry) for entry in feed.entries]


def paper_is_on_date(paper: Paper, date_str: str, timezone: str) -> bool:
    if not paper.published:
        return True
    try:
        published = datetime.fromisoformat(paper.published.replace("Z", "+00:00"))
        return published.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d") == date_str
    except ValueError:
        return True


def fetch_arxiv_for_date(config: DigestConfig, runtime: RuntimeConfig) -> List[Paper]:
    recent = fetch_recent_arxiv(config.arxiv_categories, runtime.arxiv_max_results)
    today = [paper for paper in recent if paper_is_on_date(paper, runtime.date_str, runtime.timezone)]
    if len(today) == len(recent):
        print(
            f"Warning: all {len(recent)} fetched arXiv papers fall on {runtime.date_str}; "
            "increase ARXIV_MAX_RESULTS to avoid missing older same-day papers."
        )
    return today


def fetch_arxiv_by_ids(arxiv_ids: Iterable[str]) -> Dict[str, Paper]:
    ids = [normalize_arxiv_id(paper_id) for paper_id in arxiv_ids]
    ids = [paper_id for paper_id in dict.fromkeys(ids) if paper_id]
    out: Dict[str, Paper] = {}

    def fetch_chunk(chunk: List[str]) -> None:
        resp = requests.get(ARXIV_URL, params={"id_list": ",".join(chunk)}, timeout=40)
        if resp.status_code == 400 and len(chunk) > 1:
            print(f"Warning: arXiv rejected id batch of {len(chunk)} ids; retrying one by one.")
            for paper_id in chunk:
                fetch_chunk([paper_id])
            return
        if resp.status_code == 400:
            print(f"Warning: skipping invalid or unavailable arXiv id from HuggingFace: {chunk[0]}")
            return
        resp.raise_for_status()
        feed = feedparser.parse(resp.text)
        for entry in feed.entries:
            paper = parse_arxiv_entry(entry, source="HuggingFace/arXiv")
            if paper.arxiv_id:
                out[arxiv_id_base(paper.arxiv_id)] = paper

    for start in range(0, len(ids), 20):
        fetch_chunk(ids[start:start + 20])
    return out


def fetch_huggingface(max_results: int) -> List[Paper]:
    resp = requests.get(HF_PAPERS_URL, timeout=40)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    candidates: List[Paper] = []
    seen = set()
    for link in soup.select("a[href^='/papers/']"):
        href = link.get("href", "")
        title = clean_text(link.get_text(" ", strip=True))
        if not href.startswith("/papers/") or not title:
            continue

        url = f"https://huggingface.co{href}"
        arxiv_id = extract_arxiv_id(url)
        key = arxiv_id or normalize_title(title)
        if key in seen:
            continue
        seen.add(key)

        card = link.find_parent(["article", "div", "li"]) or link
        abs_node = card.select_one("p") if hasattr(card, "select_one") else None
        abstract = clean_text(abs_node.get_text(" ", strip=True) if abs_node else "")
        candidates.append(Paper("HuggingFace", title, abstract, url, arxiv_id=arxiv_id))
        if len(candidates) >= max_results:
            break

    arxiv_by_id = fetch_arxiv_by_ids(paper.arxiv_id for paper in candidates if paper.arxiv_id)
    papers = []
    for paper in candidates:
        arxiv_key = arxiv_id_base(paper.arxiv_id) if paper.arxiv_id else ""
        papers.append(arxiv_by_id.get(arxiv_key) or paper)
    return papers


def keyword_matches(blob: str, keyword: str) -> bool:
    if len(keyword) <= 3 and keyword.isalpha():
        return re.search(rf"\b{re.escape(keyword)}\b", blob) is not None
    return keyword in blob


def paper_matches_domain(paper: Paper, domain: DomainConfig) -> bool:
    blob = f"{paper.title} {paper.abstract}".lower()
    return any(keyword_matches(blob, keyword) for keyword in domain.keywords)


def dedupe(papers: Iterable[Paper]) -> List[Paper]:
    seen = set()
    out: List[Paper] = []
    for paper in papers:
        keys = [normalize_title(paper.title)]
        if paper.arxiv_id:
            keys.append(f"arxiv:{arxiv_id_base(paper.arxiv_id.lower())}")
        keys.append(hashlib.sha1(paper.url.split("?")[0].encode("utf-8")).hexdigest())
        if any(key in seen for key in keys):
            continue
        seen.update(keys)
        out.append(paper)
    return out


def get_llm_config() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    if os.getenv("DEEPSEEK_API_KEY"):
        return (
            os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1/chat/completions"),
            os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
            os.getenv("DEEPSEEK_API_KEY"),
        )
    if os.getenv("OPENAI_API_KEY"):
        return (
            "https://api.openai.com/v1/chat/completions",
            os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            os.getenv("OPENAI_API_KEY"),
        )
    return None, None, None


def call_llm(system: str, user: str, endpoint: str, model: str, api_key: str, timeout: int = 180) -> str:
    resp = requests.post(
        endpoint,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()


def parse_json_object(content: str) -> dict:
    content = re.sub(r"^```(?:json)?\s*", "", content.strip())
    content = re.sub(r"\s*```$", "", content)
    return json.loads(content)


def fallback_digest(domain: DomainConfig, papers: List[Paper]) -> dict:
    items = []
    for paper in papers:
        items.append(
            {
                "title": paper.title,
                "classification": domain.name,
                "summary_zh": f"未配置 LLM，暂以规则模式保留这篇论文。它与 {domain.name} 的关键词匹配，但需要打开原文确认方法、实验和结论。",
                "pain_point": "未配置 LLM，无法可靠抽取领域痛点；请查看原始摘要。",
                "method": "未配置 LLM，无法可靠抽取研究方法；请查看原始摘要。",
                "result": "未配置 LLM，无法可靠抽取研究结果；请查看原始摘要。",
                "value": "值得扫读",
                "relation": f"和 {domain.name} 今日主题相关。",
            }
        )
    return {
        "top_picks": items[:3],
        "papers": items,
        "domain_summary": {
            "pain_points": [f"{domain.name} 今日有 {len(papers)} 篇去重后的候选论文。"],
            "methods": ["当前为规则降级模式，接入模型后会归纳主流研究方法。"],
            "results": ["当前为规则降级模式，接入模型后会总结实验结果和趋势信号。"],
        },
    }


def build_digest_with_llm(domain: DomainConfig, papers: List[Paper], endpoint: str, model: str, api_key: str) -> dict:
    system = "你是AI研究员，负责筛选每日论文并写给研究团队的Slack研究简报。严格输出JSON对象，不输出任何额外文字。"
    user = {
        "domain": domain.name,
        "requirements": [
            "对每篇论文保留原始标题，不要翻译标题。",
            "按照研究价值从高到低排序输出全部论文。",
            "为每篇论文给出领域分类，可以是更细粒度的英文标签，例如 Video Generation / MoE。",
            "为每篇论文写中文3句话摘要，必须正好3句话。",
            "为每篇论文分别抽取：领域痛点、研究方法、研究结果，每项用中文1到2句话。",
            "为每篇论文判断研究价值，只能使用：值得精读、值得扫读、可以忽略。",
            "选出3篇Top Picks；如果候选论文少于3篇则全部推荐。",
            "领域级总结必须重点总结今天有价值的研究点，分成 pain_points、methods、results 三组，每组3到5条中文bullet。",
            "领域级总结每条bullet可以适当详细，建议1到3句话，重点写清楚痛点、方法或结果。",
        ],
        "output_schema": {
            "top_picks": [{"title": "原英文标题", "classification": "领域分类", "value": "值得精读", "relation": "关联说明"}],
            "papers": [{"title": "原英文标题", "classification": "领域分类", "summary_zh": "三句话中文摘要。", "pain_point": "领域痛点。", "method": "研究方法。", "result": "研究结果。", "value": "值得精读|值得扫读|可以忽略", "relation": "关联说明"}],
            "domain_summary": {"pain_points": ["领域痛点bullet"], "methods": ["研究方法bullet"], "results": ["研究结果bullet"]},
        },
        "papers": [asdict(paper) for paper in papers],
    }
    try:
        content = call_llm(system, json.dumps(user, ensure_ascii=False), endpoint, model, api_key)
        return normalize_digest_result(domain, parse_json_object(content), papers)
    except (requests.RequestException, json.JSONDecodeError, TypeError, AttributeError, KeyError) as exc:
        print(f"  [{domain.name}] LLM failed, using fallback digest: {exc}")
        return fallback_digest(domain, papers)


def normalize_digest_result(domain: DomainConfig, result: dict, papers: List[Paper]) -> dict:
    title_set = {paper.title for paper in papers}

    def fix_item(item: dict) -> dict:
        value = clean_text(item.get("value", "值得扫读"))
        return {
            "title": clean_text(item.get("title", "")),
            "classification": clean_text(item.get("classification", domain.name)) or domain.name,
            "summary_zh": clean_text(item.get("summary_zh", "")) or "摘要生成失败，请打开原文查看。",
            "pain_point": clean_text(item.get("pain_point", "")) or "痛点提取失败，请查看原始摘要。",
            "method": clean_text(item.get("method", "")) or "方法提取失败，请查看原始摘要。",
            "result": clean_text(item.get("result", "")) or "结果提取失败，请查看原始摘要。",
            "value": value if value in VALUE_LABELS else "值得扫读",
            "relation": clean_text(item.get("relation", "")) or f"和 {domain.name} 今日主题相关。",
        }

    paper_items = [fix_item(item) for item in result.get("papers", []) if item.get("title") in title_set]
    if not paper_items:
        return fallback_digest(domain, papers)

    by_title = {item["title"]: item for item in paper_items}
    top_picks = []
    for item in result.get("top_picks", []):
        title = item.get("title", "")
        if title in by_title:
            top_picks.append({**by_title[title], **fix_item({**by_title[title], **item})})
        if len(top_picks) >= 3:
            break
    if len(top_picks) < min(3, len(paper_items)):
        used = {item["title"] for item in top_picks}
        for item in paper_items:
            if item["title"] not in used:
                top_picks.append(item)
                used.add(item["title"])
            if len(top_picks) >= min(3, len(paper_items)):
                break

    domain_summary = result.get("domain_summary", {}) or {}

    def clean_bullets(key: str, fallback: str) -> List[str]:
        values = domain_summary.get(key, [])
        if isinstance(values, str):
            values = [values]
        bullets = [clean_text(str(item)) for item in values if clean_text(str(item))]
        return bullets[:5] or [fallback]

    return {
        "top_picks": top_picks[:3],
        "papers": paper_items,
        "domain_summary": {
            "pain_points": clean_bullets("pain_points", f"{domain.name} 今日的主要痛点集中在模型可靠性、效率和可扩展评估。"),
            "methods": clean_bullets("methods", f"{domain.name} 今日的方法主要围绕模型结构、训练策略和数据构造展开。"),
            "results": clean_bullets("results", f"{domain.name} 今日结果显示若干方法在质量、效率或泛化上有改进。"),
        },
    }


def build_slack_message(domain: DomainConfig, papers: List[Paper], result: dict, date_str: str, filename: str) -> str:
    by_title = {paper.title: paper for paper in papers}
    lines = [f"*Daily AI Research Brief - {date_str}*", f"*领域：{domain.name}*", "", "*🔥 今日推荐精读 Top 3*", ""]
    for index, item in enumerate(result["top_picks"][:3], 1):
        paper = by_title.get(item["title"])
        if not paper:
            continue
        lines.extend([
            f"{index}. <{paper.url}|{paper.title}>",
            f"   分类：{item['classification']}",
            f"   研究价值：{item['value']}",
            f"   关联：{item['relation']}",
            "",
        ])

    summary = result["domain_summary"]
    lines.extend(["*🧠 今日有价值研究点*", "", "*领域痛点*"])
    lines.extend([f"- {item}" for item in summary["pain_points"]])
    lines.extend(["", "*研究方法*"])
    lines.extend([f"- {item}" for item in summary["methods"]])
    lines.extend(["", "*研究结果*"])
    lines.extend([f"- {item}" for item in summary["results"]])
    lines.extend(["", "*📎 全量论文链接与摘要*", f"已上传今日总 Markdown 文件：`{filename}`。文件中包含 last updated、各领域简报和全量论文链接/摘要。"])
    return "\n".join(lines)[:39000]


def domain_anchor(domain: DomainConfig) -> str:
    return domain.id


def markdown_link_abstract_block(index: int, paper: Paper) -> List[str]:
    return [
        f"### {index}. [{paper.title}]({paper.url})",
        "",
        f"- 来源：{paper.source}",
        f"- 链接：<{paper.url}>",
        "",
        "**原始摘要**",
        "",
        paper.abstract or "Hugging Face 页面未提供摘要。",
        "",
    ]


def build_domain_brief_markdown(domain: DomainConfig, papers: List[Paper], result: dict) -> List[str]:
    by_title = {paper.title: paper for paper in papers}
    lines = [f"## {domain.name}", "", "### 推荐精读 Top 3", ""]
    for index, item in enumerate(result["top_picks"][:3], 1):
        paper = by_title.get(item["title"])
        if not paper:
            continue
        lines.extend([
            f"{index}. [{paper.title}]({paper.url})",
            f"   - 分类：{item['classification']}",
            f"   - 研究价值：{item['value']}",
            f"   - 关联：{item['relation']}",
            "",
        ])

    summary = result["domain_summary"]
    lines.extend(["### 领域痛点", ""])
    lines.extend([f"- {item}" for item in summary["pain_points"]])
    lines.extend(["", "### 研究方法", ""])
    lines.extend([f"- {item}" for item in summary["methods"]])
    lines.extend(["", "### 研究结果", ""])
    lines.extend([f"- {item}" for item in summary["results"]])
    lines.extend(["", "### 全量论文链接与摘要", ""])
    if papers:
        for index, paper in enumerate(papers, 1):
            lines.extend(markdown_link_abstract_block(index, paper))
    else:
        lines.extend(["今日未抓取到论文。", ""])
    return lines


def write_daily_digest_file(config: DigestConfig, runtime: RuntimeConfig, domain_outputs: Dict[str, dict]) -> Path:
    report_root = runtime.report_dir / runtime.date_str
    report_root.mkdir(parents=True, exist_ok=True)
    path = report_root / runtime.daily_file_name
    lines = [
        f"# Daily AI Paper Digest - {runtime.date_str}",
        "",
        f"**Last updated:** {datetime.now(ZoneInfo(runtime.timezone)).strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "",
        "本文件包含当天所有配置领域的 Slack 简报和全量论文链接/原始摘要。Hugging Face Trending Papers 会先转换为对应 arXiv 论文，并与 arXiv New Papers 去重。",
        "",
        "## Navigation",
        "",
    ]
    for domain in config.domains:
        output = domain_outputs.get(domain.id, {})
        papers = output.get("papers", [])
        lines.append(f"- [{domain.name}](#{domain_anchor(domain)}) ({len(papers)})")
    lines.append("")

    for domain in config.domains:
        output = domain_outputs.get(domain.id)
        lines.extend([f'<a id="{domain_anchor(domain)}"></a>', ""])
        if not output:
            lines.extend([f"## {domain.name}", "", "今日未抓取到论文。", ""])
            continue
        lines.extend(build_domain_brief_markdown(domain, output["papers"], output["result"]))
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def write_archive_file(config: DigestConfig, runtime: RuntimeConfig, daily_file: Path) -> Path:
    runtime.archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = runtime.archive_dir / f"{runtime.date_str}.md"
    archive_path.write_text(daily_file.read_text(encoding="utf-8"), encoding="utf-8")
    return archive_path


def upload_slack_markdown_file(path: Path, channel: str, title: str) -> str:
    token = os.getenv("SLACK_BOT_TOKEN")
    if not token:
        raise RuntimeError("Missing SLACK_BOT_TOKEN")

    content = path.read_bytes()
    headers = {"Authorization": f"Bearer {token}"}
    upload_resp = requests.post(
        SLACK_UPLOAD_URL_API,
        headers=headers,
        data={"filename": path.name, "length": str(len(content))},
        timeout=30,
    )
    upload_resp.raise_for_status()
    upload_data = upload_resp.json()
    if not upload_data.get("ok"):
        raise RuntimeError(f"files.getUploadURLExternal error: {upload_data}")

    file_resp = requests.post(
        upload_data["upload_url"],
        data=content,
        headers={"Content-Type": "text/markdown; charset=utf-8"},
        timeout=60,
    )
    file_resp.raise_for_status()

    file_id = upload_data["file_id"]
    complete_resp = requests.post(
        SLACK_COMPLETE_UPLOAD_API,
        headers={**headers, "Content-Type": "application/json"},
        json={"channel_id": channel, "files": [{"id": file_id, "title": title}]},
        timeout=30,
    )
    complete_resp.raise_for_status()
    complete_data = complete_resp.json()
    if not complete_data.get("ok"):
        raise RuntimeError(f"files.completeUploadExternal error: {complete_data}")
    return file_id


def get_slack_channel(config: DigestConfig) -> str:
    channel = os.getenv(config.default_channel_env, "")
    if not channel:
        raise RuntimeError(f"Set {config.default_channel_env} for the unified Slack channel")
    return channel


def send_to_slack(text: str, channel: str) -> None:
    token = os.getenv("SLACK_BOT_TOKEN")
    if not token:
        raise RuntimeError("Missing SLACK_BOT_TOKEN")
    resp = requests.post(
        SLACK_API_URL,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"channel": channel, "text": text, "mrkdwn": True},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack API error: {data}")


def prepare_domain_output(domain: DomainConfig, runtime: RuntimeConfig, arxiv_papers_all: List[Paper], hf_papers: List[Paper], llm_config: Tuple[Optional[str], Optional[str], Optional[str]]) -> dict:
    arxiv_papers = [paper for paper in arxiv_papers_all if paper_matches_domain(paper, domain)]
    domain_hf = [paper for paper in hf_papers if paper_matches_domain(paper, domain)][:runtime.hf_max_per_domain]
    papers = dedupe(arxiv_papers + domain_hf)
    print(f"  [{domain.name}] {len(arxiv_papers)} arXiv-today matches + {len(domain_hf)} HF/arXiv matches -> {len(papers)} deduped")

    analysis_papers = papers[:runtime.analysis_max_papers]
    if len(papers) > runtime.analysis_max_papers:
        print(f"  [{domain.name}] using first {runtime.analysis_max_papers} papers for LLM analysis; daily file still contains all {len(papers)} papers.")

    endpoint, model, api_key = llm_config
    if runtime.disable_llm or not (endpoint and model and api_key):
        result = fallback_digest(domain, analysis_papers)
    else:
        result = build_digest_with_llm(domain, analysis_papers, endpoint, model, api_key)
    return {"papers": papers, "result": result}


def send_domain_brief(domain: DomainConfig, config: DigestConfig, runtime: RuntimeConfig, output: dict, daily_file: Path, uploaded_file_id: Optional[str]) -> str:
    papers = output["papers"]
    if not papers:
        return f"[{domain.name}] no papers"
    message = build_slack_message(domain, papers, output["result"], runtime.date_str, daily_file.name)
    if runtime.dry_run:
        print(f"\n===== DRY RUN: {domain.name} =====\n{message[:6000]}\n")
        return f"[{domain.name}] dry-run ok ({len(papers)} papers)"
    channel = get_slack_channel(config)
    send_to_slack(message, channel)
    suffix = f" with file {uploaded_file_id}" if uploaded_file_id else ""
    return f"[{domain.name}] sent to {channel}{suffix} ({len(papers)} papers)"


def main() -> None:
    runtime = load_runtime_config()
    config = load_digest_config(runtime.config_path)
    max_workers = min(runtime.domain_workers, len(config.domains)) or 1
    llm_config = get_llm_config()

    print(f"Loaded {len(config.domains)} domains from {runtime.config_path}")
    print(f"Fetching recent arXiv AI candidates and filtering local date {runtime.date_str}...")
    arxiv_papers_all = fetch_arxiv_for_date(config, runtime)
    print(f"arXiv today candidates: {len(arxiv_papers_all)}")

    print("Fetching HuggingFace trending papers once and converting to arXiv...")
    hf_papers = [paper for paper in fetch_huggingface(runtime.hf_max_results) if paper_is_on_date(paper, runtime.date_str, runtime.timezone)]
    print(f"HuggingFace/arXiv candidates on {runtime.date_str}: {len(hf_papers)}")

    domain_outputs: Dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(prepare_domain_output, domain, runtime, arxiv_papers_all, hf_papers, llm_config): domain
            for domain in config.domains
        }
        for future in as_completed(futures):
            domain = futures[future]
            try:
                domain_outputs[domain.id] = future.result()
            except Exception as exc:
                print(f"[{domain.name}] failed: {exc}")
                raise

    daily_file = write_daily_digest_file(config, runtime, domain_outputs)
    print(f"Wrote daily digest file: {daily_file}")
    if runtime.update_archive:
        archive_path = write_archive_file(config, runtime, daily_file)
        print(f"Updated archive file: {archive_path}")

    uploaded_file_id: Optional[str] = None
    if not runtime.dry_run:
        upload_channel = get_slack_channel(config)
        uploaded_file_id = upload_slack_markdown_file(daily_file, upload_channel, f"Daily AI Paper Digest - {runtime.date_str}")
        print(f"Uploaded daily digest file {uploaded_file_id} to {upload_channel}")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(send_domain_brief, domain, config, runtime, domain_outputs.get(domain.id, {"papers": [], "result": fallback_digest(domain, [])}), daily_file, uploaded_file_id): domain
            for domain in config.domains
        }
        for future in as_completed(futures):
            domain = futures[future]
            try:
                print(future.result())
            except Exception as exc:
                print(f"[{domain.name}] failed: {exc}")
                raise


if __name__ == "__main__":
    main()
