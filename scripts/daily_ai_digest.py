import hashlib
import html
import json
import os
import re
import time
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
SLACK_CANVAS_CREATE_API = "https://slack.com/api/canvases.create"
VALUE_LABELS = {"值得精读", "值得扫读", "可以忽略"}
_LAST_ARXIV_REQUEST_AT = 0.0
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "for", "from", "has", "have", "in", "into", "is", "it",
    "its", "of", "on", "or", "our", "that", "the", "their", "these", "this", "to", "using", "via", "we", "with",
    "across", "against", "based", "between", "data", "different", "during", "each", "existing", "first", "high",
    "large", "many", "model", "models", "new", "novel", "paper", "performance", "propose", "proposed", "results",
    "show", "shows", "significant", "task", "tasks", "training", "use", "used", "using", "various", "which", "while",
}
TECH_TERMS = [
    "agent", "alignment", "attention", "autoregressive", "benchmark", "chain-of-thought", "code generation",
    "contrastive learning", "diffusion", "distillation", "embodied ai", "fine-tuning", "foundation models",
    "generative ai", "graph neural networks", "human feedback", "in-context learning", "knowledge distillation",
    "large language models", "llm agents", "machine translation", "mixture of experts", "moe", "multi-agent",
    "multimodal", "preference optimization", "reasoning", "reinforcement learning", "retrieval augmented generation",
    "rlhf", "robot learning", "semantic segmentation", "speech recognition", "text-to-image", "text-to-video",
    "transformer", "video generation", "vision-language", "world models",
]


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
    keywords: List[str] = None


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


def env_text(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


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
    keywords = []
    for tag in entry.get("tags", []) or []:
        term = clean_text(tag.get("term", "") if isinstance(tag, dict) else getattr(tag, "term", ""))
        if term:
            keywords.append(term)
    return Paper(
        source=source,
        title=clean_text(entry.get("title", "")),
        abstract=clean_text(entry.get("summary", "")),
        url=url,
        published=entry.get("published", ""),
        arxiv_id=extract_arxiv_id(url),
        keywords=list(dict.fromkeys(keywords)),
    )


def parse_retry_after(value: str) -> Optional[float]:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def arxiv_get(params: dict, timeout: int = 60) -> requests.Response:
    global _LAST_ARXIV_REQUEST_AT

    retries = int_env("ARXIV_RETRIES", 6)
    base_wait = float_env("ARXIV_RETRY_BASE_SECONDS", 20.0)
    max_wait = float_env("ARXIV_RETRY_MAX_SECONDS", 300.0)
    request_delay = float_env("ARXIV_REQUEST_DELAY_SECONDS", 3.5)

    for attempt in range(retries + 1):
        elapsed = time.monotonic() - _LAST_ARXIV_REQUEST_AT
        if elapsed < request_delay:
            time.sleep(request_delay - elapsed)

        resp = requests.get(ARXIV_URL, params=params, timeout=timeout)
        _LAST_ARXIV_REQUEST_AT = time.monotonic()
        if resp.status_code not in {429, 500, 502, 503, 504}:
            return resp

        if attempt >= retries:
            return resp

        retry_after = parse_retry_after(resp.headers.get("Retry-After", ""))
        wait_seconds = retry_after if retry_after is not None else min(max_wait, base_wait * (2 ** attempt))
        print(f"Warning: arXiv API returned {resp.status_code}; retrying in {wait_seconds:.0f}s ({attempt + 1}/{retries}).")
        time.sleep(wait_seconds)

    return resp


def fetch_recent_arxiv(categories: Iterable[str], max_results: int) -> List[Paper]:
    papers: List[Paper] = []
    page_size = max(1, min(max_results, int_env("ARXIV_PAGE_SIZE", 500)))
    query = arxiv_category_query(categories)

    for start in range(0, max_results, page_size):
        resp = arxiv_get(
            {
                "search_query": query,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
                "start": str(start),
                "max_results": str(min(page_size, max_results - start)),
            },
            timeout=60,
        )
        resp.raise_for_status()
        feed = feedparser.parse(resp.text)
        entries = [parse_arxiv_entry(entry) for entry in feed.entries]
        if not entries:
            break
        papers.extend(entries)
        if len(entries) < page_size:
            break

    return papers[:max_results]


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
        resp = arxiv_get({"id_list": ",".join(chunk)}, timeout=40)
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
        candidates.append(Paper("HuggingFace", title, abstract, url, arxiv_id=arxiv_id, keywords=[]))
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


def matched_keywords(paper: Paper, domain: DomainConfig) -> List[str]:
    blob = f"{paper.title} {paper.abstract}".lower()
    return [keyword for keyword in domain.keywords if keyword_matches(blob, keyword)]


def tokenize_keyword_text(text: str) -> List[str]:
    return re.findall(r"[A-Za-z][A-Za-z0-9+.-]*", text.lower())


def readable_keyword(phrase: str) -> str:
    special = {"llm": "LLM", "moe": "MoE", "rag": "RAG", "rlhf": "RLHF", "ai": "AI"}
    words = []
    for word in phrase.split():
        words.append(special.get(word, word.upper() if len(word) <= 4 and word.isalpha() and word not in STOPWORDS else word))
    return " ".join(words)


def is_arxiv_category(keyword: str) -> bool:
    return bool(re.fullmatch(r"[a-z-]+(?:\.[A-Z]{2})?", keyword))


def extract_fine_keywords(paper: Paper, limit: int = 6) -> List[str]:
    text = f"{paper.title}. {paper.abstract}"
    lower = text.lower()
    title_tokens = set(tokenize_keyword_text(paper.title))
    tokens = tokenize_keyword_text(text)
    scores: Dict[str, float] = {}

    for term in TECH_TERMS:
        if re.search(rf"\b{re.escape(term)}\b", lower):
            scores[term] = scores.get(term, 0.0) + 8.0 + lower.count(term)

    for size in (4, 3, 2, 1):
        for index in range(0, max(0, len(tokens) - size + 1)):
            gram_tokens = tokens[index:index + size]
            if gram_tokens[0] in STOPWORDS or gram_tokens[-1] in STOPWORDS:
                continue
            if sum(token not in STOPWORDS for token in gram_tokens) < min(2, size):
                continue
            if any(len(token) <= 2 and not token.isupper() for token in gram_tokens):
                continue
            phrase = " ".join(gram_tokens)
            if is_arxiv_category(phrase):
                continue
            score = 1.0 + size * 0.8
            if any(token in title_tokens for token in gram_tokens):
                score += 3.0
            if any(char.isdigit() for char in phrase) or any("-" in token for token in gram_tokens):
                score += 1.0
            scores[phrase] = scores.get(phrase, 0.0) + score

    ranked = sorted(scores.items(), key=lambda item: (-item[1], len(item[0]), item[0]))
    out = []
    for phrase, _score in ranked:
        if any(phrase in chosen or chosen in phrase for chosen in out):
            continue
        out.append(phrase)
        if len(out) >= limit:
            break
    return [readable_keyword(item) for item in out]


def format_paper_keywords(paper: Paper) -> str:
    return ", ".join(extract_fine_keywords(paper)) or "未提供"


def paper_matches_domain(paper: Paper, domain: DomainConfig) -> bool:
    return bool(matched_keywords(paper, domain))


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
    deepseek_key = env_text("DEEPSEEK_API_KEY")
    if deepseek_key:
        return (
            env_text("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1/chat/completions"),
            env_text("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            deepseek_key,
        )
    openai_key = env_text("OPENAI_API_KEY")
    if openai_key:
        return (
            "https://api.openai.com/v1/chat/completions",
            env_text("OPENAI_MODEL", "gpt-4.1-mini"),
            openai_key,
        )
    return None, None, None


def describe_llm_config(runtime: RuntimeConfig, llm_config: Tuple[Optional[str], Optional[str], Optional[str]]) -> str:
    endpoint, model, api_key = llm_config
    if runtime.disable_llm:
        return "LLM disabled by DISABLE_LLM=1; using fallback summaries."
    if not api_key:
        return "LLM disabled because no DEEPSEEK_API_KEY or OPENAI_API_KEY is configured; using fallback summaries."
    if not model:
        return "LLM disabled because model name is empty; using fallback summaries."
    provider = "DeepSeek" if "deepseek" in (endpoint or "").lower() else "OpenAI-compatible"
    return f"LLM enabled: {provider} model={model}"


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


def limit_text(text: str, max_chars: int = 300) -> str:
    text = clean_text(text)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip(" ，,。.;；") + "..."


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
            "为每篇论文分别抽取：领域痛点、研究方法、研究结果。",
            "Top Picks 的 pain_point、method、result 要写得信息密度高一些，每项建议160到300个中文字符，重点内容可以展开说明；每项不要超过300个中文字符。",
            "pain_point 要说明这篇论文解决的具体研究痛点；method 要说明核心方法/机制；result 要说明关键实验结果、指标或结论。",
            "为每篇论文判断研究价值，只能使用：值得精读、值得扫读、可以忽略。",
            "选出3篇Top Picks；如果候选论文少于3篇则全部推荐。",
            "Top Picks 必须带上每篇论文自己的 pain_point、method、result；Slack 简报会逐篇展示，不要只写领域级汇总。",
            "domain_summary 只作为兜底字段，可以简短；重点放在每篇推荐论文的有价值研究点。",
        ],
        "output_schema": {
            "top_picks": [{"title": "原英文标题", "classification": "领域分类", "value": "值得精读", "relation": "关联说明", "pain_point": "这篇论文对应的领域痛点", "method": "这篇论文的研究方法", "result": "这篇论文的研究结果"}],
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
            "pain_point": limit_text(item.get("pain_point", "") or "痛点提取失败，请查看原始摘要。"),
            "method": limit_text(item.get("method", "") or "方法提取失败，请查看原始摘要。"),
            "result": limit_text(item.get("result", "") or "结果提取失败，请查看原始摘要。"),
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


def build_slack_message(domain: DomainConfig, papers: List[Paper], result: dict, date_str: str, canvas_id: Optional[str]) -> str:
    by_title = {paper.title: paper for paper in papers}
    lines = [f"*Daily AI Research Brief - {date_str}*", f"*领域：{domain.name}*", "", "*🔥 今日推荐精读 Top 3*", ""]
    for index, item in enumerate(result["top_picks"][:3], 1):
        paper = by_title.get(item["title"])
        if not paper:
            continue
        keywords = format_paper_keywords(paper)
        lines.extend([
            f"{index}. <{paper.url}|{paper.title}>",
            f"   分类：{item['classification']}",
            f"   关键词：{keywords}",
            f"   研究价值：{item['value']}",
            f"   关联：{item['relation']}",
            "   *有价值研究点*",
            f"   - 痛点：{item['pain_point']}",
            f"   - 方法：{item['method']}",
            f"   - 结果：{item['result']}",
            "",
        ])

    canvas_suffix = f"（Canvas ID: `{canvas_id}`）" if canvas_id else ""
    lines.extend(["*📎 全量论文链接与摘要*", f"已创建今日总 Slack Canvas{canvas_suffix}。Canvas 中只包含各领域全量论文链接和完整原始摘要。"])
    return "\n".join(lines)[:39000]


def domain_anchor(domain: DomainConfig) -> str:
    return domain.id


def markdown_link_abstract_block(index: int, paper: Paper, domain: DomainConfig) -> List[str]:
    return [
        f"### {index}. [{paper.title}]({paper.url})",
        "",
        f"- 来源：{paper.source}",
        f"- 关键词：{format_paper_keywords(paper)}",
        f"- 链接：<{paper.url}>",
        "",
        "**原始摘要**",
        "",
        paper.abstract or "Hugging Face 页面未提供摘要。",
        "",
    ]


def build_domain_papers_markdown(domain: DomainConfig, papers: List[Paper]) -> List[str]:
    lines = [f"## {domain.name}", ""]
    if papers:
        for index, paper in enumerate(papers, 1):
            lines.extend(markdown_link_abstract_block(index, paper, domain))
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
        "本文件只包含当天所有配置领域的全量论文链接和完整原始摘要；不包含分析、排序或 Slack 简报内容。Hugging Face Trending Papers 会先转换为对应 arXiv 论文，并与 arXiv New Papers 去重。",
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
        lines.extend(build_domain_papers_markdown(domain, output["papers"]))
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def write_archive_file(config: DigestConfig, runtime: RuntimeConfig, daily_file: Path) -> Path:
    runtime.archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = runtime.archive_dir / f"{runtime.date_str}.md"
    archive_path.write_text(daily_file.read_text(encoding="utf-8"), encoding="utf-8")
    return archive_path


def markdown_for_slack_canvas(markdown: str) -> str:
    lines = []
    for line in markdown.splitlines():
        if re.fullmatch(r'<a id="[^"]+"></a>', line.strip()):
            continue
        line = re.sub(r"\[([^\]]+)\]\(#[^)]+\)", r"\1", line)
        lines.append(line)
    return "\n".join(lines).rstrip() + "\n"


def slack_post_json(url: str, payload: dict, timeout: int = 30) -> dict:
    token = os.getenv("SLACK_BOT_TOKEN")
    if not token:
        raise RuntimeError("Missing SLACK_BOT_TOKEN")

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    retries = int_env("SLACK_RETRIES", 4)
    base_wait = float_env("SLACK_RETRY_BASE_SECONDS", 3.0)
    max_wait = float_env("SLACK_RETRY_MAX_SECONDS", 60.0)

    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                if attempt >= retries:
                    resp.raise_for_status()
                retry_after = parse_retry_after(resp.headers.get("Retry-After", ""))
                wait_seconds = retry_after if retry_after is not None else min(max_wait, base_wait * (2 ** attempt))
                print(f"Warning: Slack API returned {resp.status_code}; retrying in {wait_seconds:.0f}s ({attempt + 1}/{retries}).")
                time.sleep(wait_seconds)
                continue
            resp.raise_for_status()
            data = resp.json()
            if data.get("ok") or attempt >= retries:
                return data
            if data.get("error") not in {"ratelimited", "request_timeout", "internal_error", "service_unavailable"}:
                return data
            wait_seconds = min(max_wait, base_wait * (2 ** attempt))
            print(f"Warning: Slack API error {data.get('error')}; retrying in {wait_seconds:.0f}s ({attempt + 1}/{retries}).")
            time.sleep(wait_seconds)
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= retries:
                raise
            wait_seconds = min(max_wait, base_wait * (2 ** attempt))
            print(f"Warning: Slack request failed: {exc}; retrying in {wait_seconds:.0f}s ({attempt + 1}/{retries}).")
            time.sleep(wait_seconds)

    if last_error:
        raise last_error
    return {"ok": False, "error": "unknown_slack_error"}


def create_slack_canvas_from_markdown(path: Path, channel: str, title: str) -> str:
    markdown = markdown_for_slack_canvas(path.read_text(encoding="utf-8"))
    data = slack_post_json(
        SLACK_CANVAS_CREATE_API,
        {
            "title": title,
            "channel_id": channel,
            "document_content": {"type": "markdown", "markdown": markdown},
        },
        timeout=60,
    )
    if not data.get("ok"):
        raise RuntimeError(f"canvases.create error: {data}")
    return data["canvas_id"]


def get_slack_channel(config: DigestConfig) -> str:
    channel = os.getenv(config.default_channel_env, "")
    if not channel:
        raise RuntimeError(f"Set {config.default_channel_env} for the unified Slack channel")
    return channel


def send_to_slack(text: str, channel: str) -> None:
    data = slack_post_json(SLACK_API_URL, {"channel": channel, "text": text, "mrkdwn": True}, timeout=30)
    if not data.get("ok"):
        raise RuntimeError(f"Slack API error: {data}")


def prepare_domain_output(domain: DomainConfig, runtime: RuntimeConfig, arxiv_papers_all: List[Paper], hf_papers: List[Paper], llm_config: Tuple[Optional[str], Optional[str], Optional[str]]) -> dict:
    arxiv_papers = [paper for paper in arxiv_papers_all if paper_matches_domain(paper, domain)]
    domain_hf = [paper for paper in hf_papers if paper_matches_domain(paper, domain)][:runtime.hf_max_per_domain]
    papers = dedupe(arxiv_papers + domain_hf)
    print(f"  [{domain.name}] {len(arxiv_papers)} arXiv-today matches + {len(domain_hf)} HF/arXiv matches -> {len(papers)} deduped")

    analysis_papers = papers[:runtime.analysis_max_papers]
    if len(papers) > runtime.analysis_max_papers:
        print(f"  [{domain.name}] using first {runtime.analysis_max_papers} papers for LLM analysis; daily report still contains all {len(papers)} papers.")

    endpoint, model, api_key = llm_config
    if runtime.disable_llm or not (endpoint and model and api_key):
        result = fallback_digest(domain, analysis_papers)
    else:
        result = build_digest_with_llm(domain, analysis_papers, endpoint, model, api_key)
    return {"papers": papers, "result": result}


def send_domain_brief(domain: DomainConfig, config: DigestConfig, runtime: RuntimeConfig, output: dict, canvas_id: Optional[str]) -> str:
    papers = output["papers"]
    if not papers:
        return f"[{domain.name}] no papers"
    message = build_slack_message(domain, papers, output["result"], runtime.date_str, canvas_id)
    if runtime.dry_run:
        print(f"\n===== DRY RUN: {domain.name} =====\n{message[:6000]}\n")
        return f"[{domain.name}] dry-run ok ({len(papers)} papers)"
    channel = get_slack_channel(config)
    send_to_slack(message, channel)
    suffix = f" with canvas {canvas_id}" if canvas_id else ""
    return f"[{domain.name}] sent to {channel}{suffix} ({len(papers)} papers)"


def main() -> None:
    runtime = load_runtime_config()
    config = load_digest_config(runtime.config_path)
    max_workers = min(runtime.domain_workers, len(config.domains)) or 1
    llm_config = get_llm_config()

    print(f"Loaded {len(config.domains)} domains from {runtime.config_path}")
    print(describe_llm_config(runtime, llm_config))
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

    canvas_id: Optional[str] = None
    if not runtime.dry_run:
        channel = get_slack_channel(config)
        canvas_id = create_slack_canvas_from_markdown(daily_file, channel, f"Daily AI Paper Digest - {runtime.date_str}")
        print(f"Created daily digest canvas {canvas_id} in {channel}")

    for domain in config.domains:
        try:
            print(send_domain_brief(domain, config, runtime, domain_outputs.get(domain.id, {"papers": [], "result": fallback_digest(domain, [])}), canvas_id))
        except Exception as exc:
            print(f"[{domain.name}] failed: {exc}")
            raise


if __name__ == "__main__":
    main()
