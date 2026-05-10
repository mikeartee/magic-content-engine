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
# import requests  # available via existing project dependency
