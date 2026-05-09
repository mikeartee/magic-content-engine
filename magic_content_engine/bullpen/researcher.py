"""Researcher Lambda — crawl 8 sources and read vault from S3.

Reads approved vault notes from s3://mce-second-brain/ami-context/,
crawls 8 external sources via HTTP/RSS/GitHub API, scores each article
for relevance using Claude Haiku (1-5 scale), and returns a structured
ResearchBrief.

IAM constraints (enforced externally):
  - S3 GetObject scoped to mce-second-brain/ami-context/ only
  - Bedrock InvokeModel (Claude Haiku only)
  - CloudWatch Logs
  - NO S3 PutObject, NO SES SendEmail

Research sources (8 total, no browser required):
  1. kiro.dev/changelog/ide/          — HTTP GET
  2. kiro.dev/changelog/cli/          — HTTP GET
  3. github.com/kirodotdev/Kiro/issues — GitHub API
  4. aws.amazon.com/new/              — HTTP GET, keyword filter
  5. aws.amazon.com/blogs/machine-learning/ — HTTP GET
  6. community.aws RSS feed           — RSS at https://community.aws/rss
  7. github.com/awslabs/              — GitHub API for new releases
  8. strandsagents.com                — HTTP GET

Requirements: Bullpen Req 3, Req 4
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol
from xml.etree import ElementTree

import boto3
import requests

from magic_content_engine import config
from magic_content_engine.errors import ErrorCollector, StepError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AWS_NEWS_KEYWORDS: tuple[str, ...] = ("bedrock", "agentcore", "kiro", "lambda")

# Fixed 2-second delay between crawl retries (matches existing crawler.py pattern)
_CRAWL_FIXED_DELAY: float = 2.0
_MAX_CRAWL_ATTEMPTS: int = 3

# Default request timeout in seconds
_HTTP_TIMEOUT: int = 15

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class ScoredArticle:
    """A single article with relevance score from the Researcher.

    Lightweight bullpen-specific model passed through the pipeline.
    """

    title: str
    url: str
    source: str
    relevance_score: int  # 1-5
    summary: str  # one-sentence summary


@dataclass
class ResearchBrief:
    """Output of the Researcher Agent."""

    articles: list[ScoredArticle]
    sources_crawled: list[str]  # URLs attempted
    sources_failed: list[str]  # URLs that failed after retries
    run_timestamp: str  # ISO 8601

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict."""
        return {
            "articles": [asdict(a) for a in self.articles],
            "sources_crawled": list(self.sources_crawled),
            "sources_failed": list(self.sources_failed),
            "run_timestamp": self.run_timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResearchBrief":
        """Reconstruct a ResearchBrief from a plain dict (round-trip)."""
        return cls(
            articles=[ScoredArticle(**a) for a in data["articles"]],
            sources_crawled=list(data["sources_crawled"]),
            sources_failed=list(data["sources_failed"]),
            run_timestamp=data["run_timestamp"],
        )


# ---------------------------------------------------------------------------
# Raw article (internal, pre-scoring)
# ---------------------------------------------------------------------------


@dataclass
class _RawArticle:
    """An article discovered during crawling, before scoring."""

    title: str
    url: str
    source: str
    content: str = ""  # page text used for keyword filtering


# ---------------------------------------------------------------------------
# LLM scorer protocol
# ---------------------------------------------------------------------------


class LLMScorer(Protocol):
    """Protocol for the LLM call used during relevance scoring."""

    def __call__(self, prompt: str, model_id: str) -> str:
        """Send *prompt* to the model and return the raw text response."""
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Scoring prompt
# ---------------------------------------------------------------------------

_SCORING_PROMPT_TEMPLATE = """\
You are a relevance scorer for an AI Engineering content niche focused on \
Kiro IDE, AgentCore, Strands Agents SDK, and Bedrock — from the Aotearoa / \
Oceania community perspective.

Score the following article on a 1-to-5 integer scale:

High (4-5): Kiro IDE features or breaking changes, AgentCore/Strands/Bedrock \
announcements, MCP spec updates, steering docs or Kiro extension ecosystem \
news, Community Builder programme news.

Medium (3): AWS Lambda/S3/IAM changes affecting agent deployments, general \
agentic AI patterns with AWS application, NZ/Oceania AWS events or community news.

Low (1-2): Generic AI news without AWS angle, AWS services with no agent relevance.

Article title: {title}
Article URL: {url}
Article source: {source}

Respond with ONLY a JSON object (no markdown, no extra text):
{{
  "score": <integer 1-5>,
  "summary": "<one sentence summary of the article>",
  "rationale": "<one sentence explaining the score>"
}}
"""


def _parse_score_response(raw: str) -> tuple[int, str]:
    """Extract (score, summary) from the LLM JSON response.

    Raises ``ValueError`` when the response cannot be parsed or the
    score is outside the valid 1-5 range.
    """
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        cleaned = cleaned.rsplit("```", 1)[0]
    try:
        data = json.loads(cleaned.strip())
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM response is not valid JSON: {raw!r}") from exc

    score = data.get("score")
    summary = data.get("summary", "")

    if not isinstance(score, int) or score < 1 or score > 5:
        raise ValueError(f"Score must be an integer in [1, 5], got {score!r}")

    return score, str(summary)


# ---------------------------------------------------------------------------
# Keyword filter
# ---------------------------------------------------------------------------


def matches_aws_news_keywords(text: str) -> bool:
    """Return True if *text* contains at least one AWS news keyword.

    Matching is case-insensitive. Used to filter aws.amazon.com/new/.
    """
    lower = text.lower()
    return any(kw in lower for kw in AWS_NEWS_KEYWORDS)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _http_get(url: str, headers: dict[str, str] | None = None) -> requests.Response:
    """Perform an HTTP GET with a standard timeout and user-agent."""
    hdrs = {
        "User-Agent": "magic-content-engine/1.0 (research crawler)",
        **(headers or {}),
    }
    response = requests.get(url, headers=hdrs, timeout=_HTTP_TIMEOUT)
    response.raise_for_status()
    return response


def _retry_fetch(
    fetch_fn: Callable[[], list[_RawArticle]],
    source_name: str,
    sources_crawled: list[str],
    sources_failed: list[str],
    collector: ErrorCollector,
) -> list[_RawArticle]:
    """Attempt *fetch_fn* up to 3 times with fixed 2-second delay.

    On success, appends *source_name* to *sources_crawled*.
    On final failure, appends to *sources_failed* and returns [].
    """
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_CRAWL_ATTEMPTS + 1):
        try:
            articles = fetch_fn()
            sources_crawled.append(source_name)
            return articles
        except Exception as exc:
            last_exc = exc
            if attempt < _MAX_CRAWL_ATTEMPTS:
                logger.warning(
                    "Crawl retry %d/%d for %s — waiting %.1fs: %s",
                    attempt,
                    _MAX_CRAWL_ATTEMPTS,
                    source_name,
                    _CRAWL_FIXED_DELAY,
                    exc,
                )
                time.sleep(_CRAWL_FIXED_DELAY)

    assert last_exc is not None
    logger.error(
        "Source crawl failed after %d attempts: %s — %s",
        _MAX_CRAWL_ATTEMPTS,
        source_name,
        last_exc,
    )
    collector.add(
        StepError(
            step="crawl",
            target=source_name,
            error_message=str(last_exc),
            context={"retry_count": _MAX_CRAWL_ATTEMPTS},
        )
    )
    sources_failed.append(source_name)
    return []


# ---------------------------------------------------------------------------
# Individual source crawlers
# ---------------------------------------------------------------------------


def _crawl_http_page(url: str, source_name: str) -> list[_RawArticle]:
    """Fetch a plain HTTP page and return it as a single raw article."""
    resp = _http_get(url)
    title = _extract_title_from_html(resp.text) or source_name
    return [_RawArticle(title=title, url=url, source=source_name, content=resp.text)]


def _extract_title_from_html(html: str) -> str:
    """Extract the <title> tag content from HTML, or return empty string."""
    import re
    match = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return ""


def _crawl_github_issues(repo: str, token: str | None, source_name: str) -> list[_RawArticle]:
    """Fetch open issues from a GitHub repo via the REST API.

    Returns up to 30 issues (one page, GitHub default).
    """
    url = f"https://api.github.com/repos/{repo}/issues"
    headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = _http_get(url, headers=headers)
    issues = resp.json()

    articles: list[_RawArticle] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        title = issue.get("title", "")
        html_url = issue.get("html_url", "")
        body = issue.get("body", "") or ""
        if title and html_url:
            articles.append(
                _RawArticle(
                    title=title,
                    url=html_url,
                    source=source_name,
                    content=body,
                )
            )
    return articles


def _crawl_rss_feed(url: str, source_name: str) -> list[_RawArticle]:
    """Fetch and parse an RSS feed, returning items as raw articles."""
    resp = _http_get(url)
    try:
        root = ElementTree.fromstring(resp.content)
    except ElementTree.ParseError as exc:
        raise ValueError(f"RSS parse error for {url}: {exc}") from exc

    articles: list[_RawArticle] = []
    # Handle both RSS 2.0 (<channel><item>) and Atom (<entry>)
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    # RSS 2.0
    for item in root.findall(".//item"):
        title_el = item.find("title")
        link_el = item.find("link")
        desc_el = item.find("description")
        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        link = link_el.text.strip() if link_el is not None and link_el.text else ""
        desc = desc_el.text or "" if desc_el is not None else ""
        if title and link:
            articles.append(
                _RawArticle(title=title, url=link, source=source_name, content=desc)
            )

    # Atom
    if not articles:
        for entry in root.findall(".//{http://www.w3.org/2005/Atom}entry"):
            title_el = entry.find("{http://www.w3.org/2005/Atom}title")
            link_el = entry.find("{http://www.w3.org/2005/Atom}link")
            summary_el = entry.find("{http://www.w3.org/2005/Atom}summary")
            title = title_el.text.strip() if title_el is not None and title_el.text else ""
            link = link_el.get("href", "") if link_el is not None else ""
            summary = summary_el.text or "" if summary_el is not None else ""
            if title and link:
                articles.append(
                    _RawArticle(title=title, url=link, source=source_name, content=summary)
                )

    return articles


def _crawl_github_releases(org: str, token: str | None, source_name: str) -> list[_RawArticle]:
    """Fetch recent releases from all repos in a GitHub org.

    Uses the search API to find recently published releases.
    Returns up to 30 results.
    """
    url = f"https://api.github.com/orgs/{org}/repos"
    headers: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = _http_get(url, headers=headers)
    repos = resp.json()

    articles: list[_RawArticle] = []
    # Check the most recently updated repos (up to 10) for releases
    for repo in repos[:10]:
        if not isinstance(repo, dict):
            continue
        repo_name = repo.get("name", "")
        if not repo_name:
            continue
        releases_url = f"https://api.github.com/repos/{org}/{repo_name}/releases"
        try:
            rel_resp = _http_get(releases_url, headers=headers)
            releases = rel_resp.json()
            for release in releases[:3]:  # top 3 releases per repo
                if not isinstance(release, dict):
                    continue
                tag = release.get("tag_name", "")
                name = release.get("name", "") or tag
                html_url = release.get("html_url", "")
                body = release.get("body", "") or ""
                if name and html_url:
                    articles.append(
                        _RawArticle(
                            title=f"{repo_name} {name}",
                            url=html_url,
                            source=source_name,
                            content=body,
                        )
                    )
        except Exception as exc:
            logger.debug("Skipping releases for %s/%s: %s", org, repo_name, exc)

    return articles


# ---------------------------------------------------------------------------
# Vault reader
# ---------------------------------------------------------------------------


def read_vault_notes(
    bucket: str,
    prefix: str,
    s3_client: Any | None = None,
) -> list[str]:
    """Read all vault notes from S3 ami-context/ prefix.

    Returns a list of note contents as strings.
    The Lambda IAM role has S3 GetObject scoped to this prefix only.
    """
    if s3_client is None:
        s3_client = boto3.client("s3")

    notes: list[str] = []
    paginator = s3_client.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=bucket, Prefix=prefix)

    for page in pages:
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue  # skip directory markers
            try:
                response = s3_client.get_object(Bucket=bucket, Key=key)
                content = response["Body"].read().decode("utf-8", errors="replace")
                notes.append(content)
                logger.debug("Read vault note: s3://%s/%s", bucket, key)
            except Exception as exc:
                logger.warning("Failed to read vault note s3://%s/%s: %s", bucket, key, exc)

    logger.info("Read %d vault notes from s3://%s/%s", len(notes), bucket, prefix)
    return notes


# ---------------------------------------------------------------------------
# Main crawl orchestration
# ---------------------------------------------------------------------------


def crawl_all_sources(
    github_token: str | None = None,
    collector: ErrorCollector | None = None,
) -> tuple[list[_RawArticle], list[str], list[str]]:
    """Crawl all 8 research sources.

    Returns (raw_articles, sources_crawled, sources_failed).
    Each source is attempted up to 3 times with a 2-second fixed delay.
    Failures are logged and skipped — the crawl continues.
    """
    if collector is None:
        collector = ErrorCollector()

    sources_crawled: list[str] = []
    sources_failed: list[str] = []
    all_articles: list[_RawArticle] = []

    # --- Source 1: kiro.dev/changelog/ide/ ---
    def _fetch_kiro_ide() -> list[_RawArticle]:
        return _crawl_http_page(
            "https://kiro.dev/changelog/ide/",
            "kiro.dev/changelog/ide/",
        )

    all_articles.extend(
        _retry_fetch(_fetch_kiro_ide, "kiro.dev/changelog/ide/", sources_crawled, sources_failed, collector)
    )

    # --- Source 2: kiro.dev/changelog/cli/ ---
    def _fetch_kiro_cli() -> list[_RawArticle]:
        return _crawl_http_page(
            "https://kiro.dev/changelog/cli/",
            "kiro.dev/changelog/cli/",
        )

    all_articles.extend(
        _retry_fetch(_fetch_kiro_cli, "kiro.dev/changelog/cli/", sources_crawled, sources_failed, collector)
    )

    # --- Source 3: github.com/kirodotdev/Kiro/issues ---
    def _fetch_kiro_issues() -> list[_RawArticle]:
        return _crawl_github_issues(
            "kirodotdev/Kiro",
            github_token,
            "github.com/kirodotdev/Kiro/issues",
        )

    all_articles.extend(
        _retry_fetch(
            _fetch_kiro_issues,
            "github.com/kirodotdev/Kiro/issues",
            sources_crawled,
            sources_failed,
            collector,
        )
    )

    # --- Source 4: aws.amazon.com/new/ (keyword filter) ---
    def _fetch_aws_news() -> list[_RawArticle]:
        articles = _crawl_http_page(
            "https://aws.amazon.com/new/",
            "aws.amazon.com/new/",
        )
        # Apply keyword filter: only keep if content matches niche keywords
        filtered = [a for a in articles if matches_aws_news_keywords(a.content)]
        before = len(articles)
        after = len(filtered)
        logger.info("AWS news keyword filter: %d -> %d articles", before, after)
        return filtered

    all_articles.extend(
        _retry_fetch(
            _fetch_aws_news,
            "aws.amazon.com/new/",
            sources_crawled,
            sources_failed,
            collector,
        )
    )

    # --- Source 5: aws.amazon.com/blogs/machine-learning/ ---
    def _fetch_aws_ml_blog() -> list[_RawArticle]:
        return _crawl_http_page(
            "https://aws.amazon.com/blogs/machine-learning/",
            "aws.amazon.com/blogs/machine-learning/",
        )

    all_articles.extend(
        _retry_fetch(
            _fetch_aws_ml_blog,
            "aws.amazon.com/blogs/machine-learning/",
            sources_crawled,
            sources_failed,
            collector,
        )
    )

    # --- Source 6: community.aws RSS feed ---
    def _fetch_community_aws_rss() -> list[_RawArticle]:
        return _crawl_rss_feed(
            "https://community.aws/rss",
            "community.aws",
        )

    all_articles.extend(
        _retry_fetch(
            _fetch_community_aws_rss,
            "community.aws",
            sources_crawled,
            sources_failed,
            collector,
        )
    )

    # --- Source 7: github.com/awslabs/ releases ---
    def _fetch_awslabs_releases() -> list[_RawArticle]:
        return _crawl_github_releases(
            "awslabs",
            github_token,
            "github.com/awslabs/",
        )

    all_articles.extend(
        _retry_fetch(
            _fetch_awslabs_releases,
            "github.com/awslabs/",
            sources_crawled,
            sources_failed,
            collector,
        )
    )

    # --- Source 8: strandsagents.com ---
    def _fetch_strands() -> list[_RawArticle]:
        return _crawl_http_page(
            "https://strandsagents.com",
            "strandsagents.com",
        )

    all_articles.extend(
        _retry_fetch(
            _fetch_strands,
            "strandsagents.com",
            sources_crawled,
            sources_failed,
            collector,
        )
    )

    logger.info(
        "Crawl complete: %d articles from %d sources (%d failed)",
        len(all_articles),
        len(sources_crawled),
        len(sources_failed),
    )
    return all_articles, sources_crawled, sources_failed


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _score_raw_article(
    article: _RawArticle,
    llm: LLMScorer,
    model_id: str,
) -> tuple[int, str]:
    """Score a single raw article via the LLM.

    Returns (score, summary). Raises on LLM or parsing failure.
    """
    prompt = _SCORING_PROMPT_TEMPLATE.format(
        title=article.title,
        url=article.url,
        source=article.source,
    )
    raw_response = llm(prompt, model_id)
    return _parse_score_response(raw_response)


def score_articles(
    articles: list[_RawArticle],
    llm: LLMScorer,
    threshold: int = 3,
    collector: ErrorCollector | None = None,
) -> list[ScoredArticle]:
    """Score articles via Claude Haiku and return those meeting the threshold.

    Articles with relevance_score >= threshold are returned as ScoredArticle.
    Articles below threshold are dropped.
    Per-article failures are logged and skipped (log-and-continue).
    """
    if collector is None:
        collector = ErrorCollector()

    model_id = config.HAIKU_MODEL_ID
    scored: list[ScoredArticle] = []

    for article in articles:
        try:
            score, summary = _score_raw_article(article, llm, model_id)

            if score >= threshold:
                scored.append(
                    ScoredArticle(
                        title=article.title,
                        url=article.url,
                        source=article.source,
                        relevance_score=score,
                        summary=summary,
                    )
                )
                logger.info("Article scored %d (kept): %s", score, article.url)
            else:
                logger.info("Article scored %d (excluded): %s", score, article.url)

        except Exception as exc:
            collector.add(
                StepError(
                    step="score",
                    target=article.url,
                    error_message=str(exc),
                    context={"model": model_id},
                )
            )
            logger.warning("Scoring failed for %s: %s", article.url, exc)

    return scored


# ---------------------------------------------------------------------------
# Default production LLM scorer (Bedrock via boto3)
# ---------------------------------------------------------------------------


def _make_bedrock_scorer() -> LLMScorer:
    """Return a production LLM scorer backed by Bedrock InvokeModel."""
    bedrock = boto3.client("bedrock-runtime", region_name="ap-southeast-2")

    def _call(prompt: str, model_id: str) -> str:
        body = json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 256,
                "messages": [{"role": "user", "content": prompt}],
            }
        )
        response = bedrock.invoke_model(
            modelId=model_id,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        result = json.loads(response["body"].read())
        return result["content"][0]["text"]

    return _call


# ---------------------------------------------------------------------------
# Lambda handler / main entry point
# ---------------------------------------------------------------------------


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda handler for the Researcher Agent.

    Expected event fields:
      - topic (str): the content topic from BullpenBrief
      - context_feed_path (str, optional): S3 URI override for vault notes
      - score_threshold (int, optional): minimum score to keep (default 3)

    Returns a JSON-serialisable ResearchBrief dict.
    """
    topic = event.get("topic", "")
    score_threshold = int(event.get("score_threshold", config.RELEVANCE_THRESHOLD))
    github_token = config.__dict__.get("GITHUB_TOKEN") or None

    # Read GITHUB_TOKEN from environment if available
    import os
    github_token = os.environ.get("GITHUB_TOKEN") or github_token

    collector = ErrorCollector()

    # Read vault notes from S3 (read-only, IAM enforced)
    vault_notes: list[str] = []
    try:
        vault_notes = read_vault_notes(
            bucket=config.MCE_SECOND_BRAIN_BUCKET,
            prefix=config.MCE_S3_AMI_CONTEXT_PREFIX,
        )
        logger.info("Loaded %d vault notes", len(vault_notes))
    except Exception as exc:
        logger.warning("Could not read vault notes: %s", exc)
        collector.add(
            StepError(
                step="vault_read",
                target=f"s3://{config.MCE_SECOND_BRAIN_BUCKET}/{config.MCE_S3_AMI_CONTEXT_PREFIX}",
                error_message=str(exc),
                context={},
            )
        )

    # Crawl all 8 sources
    raw_articles, sources_crawled, sources_failed = crawl_all_sources(
        github_token=github_token,
        collector=collector,
    )

    # Score articles via Claude Haiku
    llm = _make_bedrock_scorer()
    scored_articles = score_articles(
        raw_articles,
        llm=llm,
        threshold=score_threshold,
        collector=collector,
    )

    brief = ResearchBrief(
        articles=scored_articles,
        sources_crawled=sources_crawled,
        sources_failed=sources_failed,
        run_timestamp=datetime.now(timezone.utc).isoformat(),
    )

    logger.info(
        "ResearchBrief complete: %d articles kept, %d sources crawled, %d failed",
        len(scored_articles),
        len(sources_crawled),
        len(sources_failed),
    )

    return brief.to_dict()
