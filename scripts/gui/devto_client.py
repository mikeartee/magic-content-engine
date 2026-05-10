"""
Bullpen Web GUI — dev.to API client.

This module provides ``publish_article``, which POSTs to the dev.to REST API
at ``https://dev.to/api/articles``. It uses ``DEVTO_API_KEY`` from the
environment (loaded via ``magic_content_engine/config.py``).

Returns a structured error dict on non-201 responses or network failures so
that the Flask endpoint can return a consistent JSON error shape to the browser.

Implemented in Task 8.
"""

# Standard library
import os
from typing import Any

# Third-party
import requests

_DEVTO_ARTICLES_URL = "https://dev.to/api/articles"


def publish_article(
    api_key: str,
    title: str,
    body_markdown: str,
    tags: list[str],
    published: bool,
) -> dict[str, Any]:
    """POST an article to dev.to and return a structured result dict.

    On HTTP 201:
        {"success": True, "url": <article url>, "id": <article id>}

    On non-201 HTTP response:
        {"success": False, "status_code": <int>, "error": <response text>}

    On network exception:
        {"success": False, "status_code": None, "error": <exception message>}
    """
    headers = {
        "api-key": api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "article": {
            "title": title,
            "body_markdown": body_markdown,
            "tags": tags,
            "published": published,
        }
    }

    try:
        response = requests.post(
            _DEVTO_ARTICLES_URL,
            json=payload,
            headers=headers,
            timeout=30,
        )
    except Exception as exc:
        return {"success": False, "status_code": None, "error": str(exc)}

    if response.status_code == 201:
        response_json = response.json()
        return {
            "success": True,
            "url": response_json["url"],
            "id": response_json["id"],
        }

    return {
        "success": False,
        "status_code": response.status_code,
        "error": response.text,
    }
