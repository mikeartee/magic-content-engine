"""Production dependency factory for the Magic Content Engine.

Constructs WorkflowDependencies with real AWS-backed implementations:

  - DynamoDB (single table, pk/sk design) for all memory/persistence
  - Amazon Bedrock via boto3 for LLM calls (Haiku + Sonnet)
  - requests + GitHub REST API for web crawling (no JS rendering)
  - AWS Secrets Manager for credentials
  - boto3 SES for email notifications
  - boto3 S3 for file uploads
  - pathlib for local file operations (/tmp in Lambda)

JS-rendered sources (kiro.dev, community.aws, strandsagents.com,
repost.aws) are gracefully skipped — they raise during fetch_page(),
which retry_crawl() catches, logs, and continues past.

DynamoDB table schema (single table: magic-content-engine):
  pk / sk                              attributes
  ─────────────────────────────────────────────────────────
  VOICE_PROFILE / PROFILE              content: str
  COVERED_URL   / <url>                run_date: str
  TOPIC_COVERAGE / MAP                 data: json_str
  ENGAGEMENT    / <title>#<date>       PostEngagement fields
  HELD_ITEM     / <filename>#<date>    HeldItem fields

Secrets Manager secret (magic-content-engine/credentials):
  {
    "github_token":     "...",
    "devto_api_key":    "...",
    "devto_username":   "...",
    "ses_sender_email": "...",
    "ses_recipient_email": "..."
  }
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import boto3
import requests
from botocore.config import Config as BotocoreConfig

from magic_content_engine.crawler import CrawlResult
from magic_content_engine.models import (
    HeldItem,
    PostEngagement,
    TopicCoverageEntry,
    TopicCoverageMap,
)
from magic_content_engine.orchestrator import MemoryProtocol, WorkflowDependencies

logger = logging.getLogger(__name__)

_AWS_REGION = os.getenv("AWS_REGION", "ap-southeast-2")
_DYNAMODB_TABLE = os.getenv("DYNAMODB_TABLE", "magic-content-engine")
_SECRETS_NAME = os.getenv("SECRETS_NAME", "magic-content-engine/credentials")
_GITHUB_API_BASE = "https://api.github.com"
_USER_AGENT = "magic-content-engine/1.0 (+https://github.com/mikefromnz)"


# ============================================================================
# Credentials
# ============================================================================


class _Credentials:
    """Lazy-loading credentials from AWS Secrets Manager."""

    def __init__(
        self,
        secret_name: str = _SECRETS_NAME,
        region: str = _AWS_REGION,
    ) -> None:
        self._secret_name = secret_name
        self._region = region
        self._cache: dict[str, str] | None = None

    def _load(self) -> dict[str, str]:
        if self._cache is None:
            client = boto3.client("secretsmanager", region_name=self._region)
            resp = client.get_secret_value(SecretId=self._secret_name)
            self._cache = json.loads(resp["SecretString"])
            logger.info("Loaded credentials from Secrets Manager: %s", self._secret_name)
        return self._cache

    def get(self, key: str, default: str = "") -> str:
        return self._load().get(key, default)


# ============================================================================
# DynamoDB store — implements all five memory protocols
# ============================================================================


class DynamoDBStore:
    """Single DynamoDB table backing all five memory protocols.

    A single instance can be passed for memory, dedup_memory,
    topic_memory, engagement_memory, and held_item_memory in
    WorkflowDependencies.
    """

    def __init__(
        self,
        table_name: str = _DYNAMODB_TABLE,
        region: str = _AWS_REGION,
    ) -> None:
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)
        self._url_cache: set[str] | None = None

    # ------------------------------------------------------------------
    # orchestrator.MemoryProtocol
    # ------------------------------------------------------------------

    def load_voice_profile(self) -> str:
        try:
            resp = self._table.get_item(Key={"pk": "VOICE_PROFILE", "sk": "PROFILE"})
            return resp.get("Item", {}).get("content", "")
        except Exception as exc:
            logger.warning("Could not load voice profile: %s", exc)
            return ""

    def load_covered_urls(self) -> set[str]:
        return self._load_url_cache()

    def store_covered_urls(self, urls: set[str], run_date: date) -> None:
        for url in urls:
            self.store_article_url(url, run_date)

    # ------------------------------------------------------------------
    # deduplication.MemoryProtocol
    # ------------------------------------------------------------------

    def is_url_previously_covered(self, url: str) -> bool:
        return url in self._load_url_cache()

    def store_article_url(self, url: str, run_date: date) -> None:
        try:
            self._table.put_item(Item={
                "pk": "COVERED_URL",
                "sk": url,
                "run_date": run_date.isoformat(),
            })
            if self._url_cache is not None:
                self._url_cache.add(url)
        except Exception as exc:
            logger.warning("Could not store covered URL %s: %s", url, exc)

    def _load_url_cache(self) -> set[str]:
        if self._url_cache is None:
            try:
                from boto3.dynamodb.conditions import Key
                resp = self._table.query(
                    KeyConditionExpression=Key("pk").eq("COVERED_URL")
                )
                self._url_cache = {item["sk"] for item in resp.get("Items", [])}
                logger.info("Loaded %d covered URLs from DynamoDB", len(self._url_cache))
            except Exception as exc:
                logger.warning("Could not load covered URLs: %s", exc)
                self._url_cache = set()
        return self._url_cache

    # ------------------------------------------------------------------
    # topic_coverage.TopicCoverageMemoryProtocol
    # ------------------------------------------------------------------

    def load_topic_coverage_map(self) -> Optional[TopicCoverageMap]:
        try:
            resp = self._table.get_item(Key={"pk": "TOPIC_COVERAGE", "sk": "MAP"})
            item = resp.get("Item")
            if not item:
                return None
            data = json.loads(item["data"])
            return _deserialize_coverage_map(data)
        except Exception as exc:
            logger.warning("Could not load topic coverage map: %s", exc)
            return None

    def save_topic_coverage_map(self, coverage_map: TopicCoverageMap) -> None:
        try:
            from dataclasses import asdict
            self._table.put_item(Item={
                "pk": "TOPIC_COVERAGE",
                "sk": "MAP",
                "data": json.dumps(asdict(coverage_map), default=str),
            })
        except Exception as exc:
            logger.warning("Could not save topic coverage map: %s", exc)

    # ------------------------------------------------------------------
    # engagement.EngagementMemoryProtocol
    # ------------------------------------------------------------------

    def load_engagements(self) -> list[PostEngagement]:
        try:
            from boto3.dynamodb.conditions import Key
            resp = self._table.query(
                KeyConditionExpression=Key("pk").eq("ENGAGEMENT")
            )
            return [_deserialize_engagement(item) for item in resp.get("Items", [])]
        except Exception as exc:
            logger.warning("Could not load engagements: %s", exc)
            return []

    def save_engagements(self, engagements: list[PostEngagement]) -> None:
        for eng in engagements:
            try:
                self._table.put_item(Item={
                    "pk": "ENGAGEMENT",
                    "sk": f"{eng.post_title}#{eng.publication_date.isoformat()}",
                    "post_title": eng.post_title,
                    "publication_date": eng.publication_date.isoformat(),
                    "url": eng.url,
                    "views": eng.views,
                    "reactions": eng.reactions,
                    "comments": eng.comments,
                    "reading_time_minutes": eng.reading_time_minutes,
                    "last_fetched": eng.last_fetched.isoformat(),
                })
            except Exception as exc:
                logger.warning("Could not save engagement for '%s': %s", eng.post_title, exc)

    # ------------------------------------------------------------------
    # embargo.HeldItemMemoryProtocol
    # ------------------------------------------------------------------

    def load_held_items(self) -> list[HeldItem]:
        try:
            from boto3.dynamodb.conditions import Key
            resp = self._table.query(
                KeyConditionExpression=Key("pk").eq("HELD_ITEM")
            )
            return [_deserialize_held_item(item) for item in resp.get("Items", [])]
        except Exception as exc:
            logger.warning("Could not load held items: %s", exc)
            return []

    def save_held_item(self, item: HeldItem) -> None:
        try:
            self._table.put_item(Item={
                "pk": "HELD_ITEM",
                "sk": f"{item.filename}#{item.run_date.isoformat()}",
                "filename": item.filename,
                "s3_destination_path": item.s3_destination_path,
                "release_date": item.release_date.isoformat(),
                "article_titles": item.article_titles,
                "run_date": item.run_date.isoformat(),
                "local_file_path": item.local_file_path,
            })
        except Exception as exc:
            logger.warning("Could not save held item '%s': %s", item.filename, exc)

    def remove_held_item(self, item: HeldItem) -> None:
        try:
            self._table.delete_item(Key={
                "pk": "HELD_ITEM",
                "sk": f"{item.filename}#{item.run_date.isoformat()}",
            })
        except Exception as exc:
            logger.warning("Could not remove held item '%s': %s", item.filename, exc)


# ============================================================================
# DynamoDB deserializers
# ============================================================================


def _deserialize_coverage_map(data: dict) -> TopicCoverageMap:
    entries = []
    for e in data.get("entries", []):
        lcd = e.get("last_covered_date")
        entries.append(TopicCoverageEntry(
            topic=e["topic"],
            covered=e["covered"],
            article_titles=e.get("article_titles", []),
            last_covered_date=date.fromisoformat(lcd) if lcd else None,
            adjacent_topics=e.get("adjacent_topics", []),
        ))
    lu = data.get("last_updated")
    return TopicCoverageMap(
        entries=entries,
        last_updated=date.fromisoformat(lu) if lu else date.today(),
        recommended_focus=data.get("recommended_focus"),
    )


def _deserialize_engagement(item: dict) -> PostEngagement:
    return PostEngagement(
        post_title=item["post_title"],
        publication_date=date.fromisoformat(item["publication_date"]),
        url=item.get("url", ""),
        views=int(item.get("views", 0)),
        reactions=int(item.get("reactions", 0)),
        comments=int(item.get("comments", 0)),
        reading_time_minutes=int(item.get("reading_time_minutes", 0)),
        last_fetched=date.fromisoformat(item["last_fetched"]),
    )


def _deserialize_held_item(item: dict) -> HeldItem:
    return HeldItem(
        filename=item["filename"],
        s3_destination_path=item["s3_destination_path"],
        release_date=date.fromisoformat(item["release_date"]),
        article_titles=item.get("article_titles", []),
        run_date=date.fromisoformat(item["run_date"]),
        local_file_path=item["local_file_path"],
    )


# ============================================================================
# HTTP + GitHub API browser
# ============================================================================

# Sources that require JS rendering — raise to trigger graceful skip
_JS_SOURCES: frozenset[str] = frozenset({
    "https://kiro.dev/changelog/ide/",
    "https://kiro.dev/blog/",
    "https://community.aws/",
    "https://strandsagents.com",
    "https://repost.aws/",
})


class HTTPBrowser:
    """BrowserProtocol backed by plain HTTP requests and the GitHub REST API.

    JS-rendered sources raise RuntimeError, which retry_crawl() catches,
    logs as a source failure, and skips. The remaining five sources
    (GitHub Kiro issues, GitHub awslabs repos, aws.amazon.com/new/,
    aws.amazon.com/blogs/machine-learning/) are fetched reliably.
    """

    def __init__(self, github_token: str = "", timeout: int = 30) -> None:
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers["User-Agent"] = _USER_AGENT

        self._gh = requests.Session()
        self._gh.headers["User-Agent"] = _USER_AGENT
        self._gh.headers["Accept"] = "application/vnd.github.v3+json"
        if github_token:
            self._gh.headers["Authorization"] = f"Bearer {github_token}"

    def fetch_page(self, url: str) -> CrawlResult:
        if url in _JS_SOURCES:
            raise RuntimeError(
                f"Source requires JS rendering — skipped in HTTP-only mode: {url}"
            )
        if "github.com/kirodotdev/Kiro/issues" in url:
            return self._github_issues("kirodotdev", "Kiro")
        if "github.com/awslabs" in url:
            return self._github_org_repos("awslabs")
        return self._http_page(url)

    def _github_issues(self, owner: str, repo: str) -> CrawlResult:
        resp = self._gh.get(
            f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/issues",
            params={"state": "open", "per_page": 25, "sort": "updated"},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        issues = resp.json()
        links = [i["html_url"] for i in issues if "html_url" in i]
        content = "\n".join(
            f"{i.get('title', '')} — {(i.get('body') or '')[:200]}"
            for i in issues
        )
        return CrawlResult(
            url=f"https://github.com/{owner}/{repo}/issues",
            content=content,
            title=f"{owner}/{repo} Issues",
            links=links or None,
        )

    def _github_org_repos(self, org: str) -> CrawlResult:
        resp = self._gh.get(
            f"{_GITHUB_API_BASE}/orgs/{org}/repos",
            params={"sort": "updated", "per_page": 20, "type": "public"},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        repos = resp.json()
        # Prefer agent/AI-related repos
        relevant = [
            r for r in repos
            if any(
                kw in (r.get("name", "") + " " + (r.get("description") or "")).lower()
                for kw in ("agentcore", "strands", "bedrock", "agent", "mcp")
            )
        ]
        chosen = relevant or repos[:10]
        links = [r["html_url"] for r in chosen]
        content = "\n".join(
            f"{r.get('name', '')} — {r.get('description', '')}"
            for r in chosen
        )
        return CrawlResult(
            url=f"https://github.com/{org}",
            content=content,
            title=f"{org} repositories",
            links=links or None,
        )

    def fetch_article_body(self, url: str) -> str:
        """Fetch the body text of a single article URL.

        Handles GitHub issue URLs via the API (returns title + body),
        GitHub repo URLs (returns description + README intro), and
        plain HTTP pages (returns stripped text up to 3000 chars).
        Returns empty string on failure.
        """
        import re as _re
        try:
            # GitHub issue: https://github.com/owner/repo/issues/NUMBER
            gh_issue = _re.match(
                r"https://github\.com/([^/]+)/([^/]+)/issues/(\d+)", url
            )
            if gh_issue:
                owner, repo, number = gh_issue.groups()
                resp = self._gh.get(
                    f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/issues/{number}",
                    timeout=self._timeout,
                )
                if resp.ok:
                    data = resp.json()
                    title = data.get("title", "")
                    body = (data.get("body") or "")[:2000]
                    return f"{title}\n\n{body}".strip()

            # GitHub repo: https://github.com/owner/repo
            gh_repo = _re.match(r"https://github\.com/([^/]+)/([^/]+)/?$", url)
            if gh_repo:
                owner, repo = gh_repo.groups()
                resp = self._gh.get(
                    f"{_GITHUB_API_BASE}/repos/{owner}/{repo}",
                    timeout=self._timeout,
                )
                if resp.ok:
                    data = resp.json()
                    desc = data.get("description") or ""
                    # Try README
                    readme_resp = self._gh.get(
                        f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/readme",
                        timeout=self._timeout,
                    )
                    readme = ""
                    if readme_resp.ok:
                        import base64 as _b64
                        raw = readme_resp.json().get("content", "")
                        try:
                            readme = _b64.b64decode(raw).decode("utf-8", errors="replace")[:1500]
                        except Exception:
                            pass
                    return f"{desc}\n\n{readme}".strip()[:2000]

            # Plain HTTP page
            resp = self._session.get(url, timeout=self._timeout)
            if resp.ok:
                html = resp.text
                text = _re.sub(r"<[^>]+>", " ", html)
                text = _re.sub(r"\s+", " ", text).strip()
                return text[:3000]
        except Exception:
            pass
        return ""

    def _http_page(self, url: str) -> CrawlResult:
        resp = self._session.get(url, timeout=self._timeout)
        resp.raise_for_status()
        html = resp.text
        title = _extract_html_title(html)
        links = _extract_article_links(html, url) or None
        content = re.sub(r"<[^>]+>", " ", html)
        content = re.sub(r"\s+", " ", content).strip()[:8000]
        return CrawlResult(url=url, content=content, title=title, links=links)


def _extract_html_title(html: str) -> str:
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _extract_article_links(html: str, base_url: str) -> list[str]:
    """Extract article-looking hrefs from AWS pages."""
    base = urlparse(base_url)
    hrefs = re.findall(r'href=["\']([^"\'#?]+)["\']', html)
    links: list[str] = []
    seen: set[str] = set()

    for href in hrefs:
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)

        if parsed.netloc != base.netloc:
            continue

        path = parsed.path
        if any(pat in path for pat in (
            "/new/features/",
            "/blogs/machine-learning/",
            "/blogs/aws/",
            "/about-aws/whats-new/",
        )):
            clean = f"{parsed.scheme}://{parsed.netloc}{path}"
            if clean not in seen and len(links) < 30:
                seen.add(clean)
                links.append(clean)

    return links


# ============================================================================
# Null screenshot browser
# ============================================================================


class NullScreenshotBrowser:
    """Screenshots are not available in Lambda (no display).

    Raises RuntimeError so the pipeline logs each attempt as a failed
    screenshot capture and continues — consistent with how all other
    screenshot failures are handled.
    """

    def capture(
        self,
        url: str,
        viewport_width: int = 1440,
        viewport_height: int = 900,
        wait_seconds: int = 3,
    ) -> bytes:
        raise RuntimeError(f"Screenshots unavailable in Lambda (no display): {url}")


# ============================================================================
# Bedrock LLM callers
# ============================================================================


class _BedrockClient:
    """Shared Bedrock runtime client.

    Uses a 600-second read timeout so that long Sonnet generations
    (blog posts, YouTube scripts) don't hit boto3's default 60s limit.
    """

    def __init__(self, region: str = _AWS_REGION) -> None:
        _cfg = BotocoreConfig(
            read_timeout=600,
            connect_timeout=30,
            retries={"max_attempts": 3, "mode": "adaptive"},
        )
        self._client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=_cfg,
        )

    def invoke(self, model_id: str, prompt: str) -> str:
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 8192,
            "messages": [{"role": "user", "content": prompt}],
        })
        response = self._client.invoke_model(
            modelId=model_id,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        result = json.loads(response["body"].read())
        return result["content"][0]["text"]


class BedrockPositionalLLM:
    """LLM caller for scorer / extractor / formatter protocols.

    Signature: (prompt: str, model_id: str) -> str
    """

    def __init__(self, bedrock: _BedrockClient) -> None:
        self._bedrock = bedrock

    def __call__(self, prompt: str, model_id: str) -> str:
        return self._bedrock.invoke(model_id, prompt)


class BedrockKeywordLLM:
    """LLM caller for writing_agent.LLMProtocol.

    Signature: (*, model_id: str, prompt: str) -> str
    """

    def __init__(self, bedrock: _BedrockClient) -> None:
        self._bedrock = bedrock

    def __call__(self, *, model_id: str, prompt: str) -> str:
        return self._bedrock.invoke(model_id, prompt)


# ============================================================================
# dev.to API
# ============================================================================


class DevToAPI:
    """Fetches user articles from the dev.to REST API."""

    def fetch_user_articles(self, username: str, api_key: str) -> list[dict]:
        headers = {"api-key": api_key, "User-Agent": _USER_AGENT}
        resp = requests.get(
            "https://dev.to/api/articles",
            params={"username": username, "per_page": 50},
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()


# ============================================================================
# SES notifier
# ============================================================================


class SESNotifier:
    """Sends embargo release notifications via Amazon SES."""

    def __init__(
        self,
        sender: str,
        recipient: str,
        region: str = _AWS_REGION,
    ) -> None:
        self._client = boto3.client("ses", region_name=region)
        self._sender = sender
        self._recipient = recipient

    def send_embargo_release(self, item: HeldItem) -> None:
        if not self._sender or not self._recipient:
            raise RuntimeError("SES sender/recipient not configured")
        titles = ", ".join(item.article_titles) if item.article_titles else item.filename
        subject = f"Magic Content Engine — embargo lifted: {titles}"
        body = (
            f"The following content is ready for release:\n\n"
            f"File:         {item.filename}\n"
            f"Run date:     {item.run_date.isoformat()}\n"
            f"Release date: {item.release_date.isoformat()}\n"
            f"Local path:   {item.local_file_path}\n"
        )
        self._client.send_email(
            Source=self._sender,
            Destination={"ToAddresses": [self._recipient]},
            Message={
                "Subject": {"Data": subject},
                "Body": {"Text": {"Data": body}},
            },
        )


# ============================================================================
# S3 client
# ============================================================================


class BotoS3Client:
    """S3 upload implementation using boto3."""

    def __init__(self, region: str = _AWS_REGION) -> None:
        self._client = boto3.client("s3", region_name=region)

    def upload_file(self, local_path: str, bucket: str, key: str) -> None:
        self._client.upload_file(local_path, bucket, key)


# ============================================================================
# File operations
# ============================================================================


class PathLibBundleFileOps:
    """bundle.FileOps backed by pathlib.

    Paths are relative to cwd (set to /tmp in Lambda handler).
    """

    def write_text(self, path: str, content: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def write_json(self, path: str, data: dict) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    def ensure_dir(self, path: str) -> None:
        Path(path).mkdir(parents=True, exist_ok=True)


class PathLibGateFileOps:
    """publish_gate.FileOps backed by shutil."""

    def move_file(self, src: str, dest_dir: str, filename: str) -> str:
        dest = os.path.join(dest_dir, filename)
        os.makedirs(dest_dir, exist_ok=True)
        shutil.move(src, dest)
        return dest


# ============================================================================
# Factory
# ============================================================================


def build_dependencies() -> WorkflowDependencies:
    """Construct WorkflowDependencies with real AWS-backed implementations.

    Loads credentials from Secrets Manager. All AWS calls use the
    region configured in the AWS_REGION environment variable
    (default: ap-southeast-2).

    In Lambda the handler sets cwd to /tmp before calling run_workflow(),
    so relative output paths (output/YYYY-MM-DD-slug/) resolve there.
    """
    creds = _Credentials()
    store = DynamoDBStore()
    bedrock = _BedrockClient()

    return WorkflowDependencies(
        # DynamoDB store implements all five memory protocols
        memory=store,
        dedup_memory=store,
        topic_memory=store,
        engagement_memory=store,
        held_item_memory=store,

        # dev.to engagement API
        engagement_api=DevToAPI(),

        # SES for embargo notifications
        ses_notifier=SESNotifier(
            sender=creds.get("ses_sender_email"),
            recipient=creds.get("ses_recipient_email"),
        ),

        # HTTP browser (GitHub API + plain HTTP; JS sources gracefully skipped)
        browser=HTTPBrowser(github_token=creds.get("github_token")),

        # Bedrock LLM callers
        llm_scorer=BedrockPositionalLLM(bedrock),
        llm_extractor=BedrockPositionalLLM(bedrock),
        llm_formatter=BedrockPositionalLLM(bedrock),
        llm_writer=BedrockKeywordLLM(bedrock),

        # Null screenshot browser (no display in Lambda)
        screenshot_browser=NullScreenshotBrowser(),

        # S3 uploader
        s3_client=BotoS3Client(),

        # File operations
        bundle_file_ops=PathLibBundleFileOps(),
        gate_file_ops=PathLibGateFileOps(),

        # Unattended — no interactive prompts in Lambda
        unattended=True,

        # Config overrides
        s3_bucket=os.getenv("S3_BUCKET", "magic-content-dev"),
        steering_base_path=os.getenv(
            "STEERING_BASE_PATH", "/var/task/.kiro/steering/"
        ),
    )
