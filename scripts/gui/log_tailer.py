"""
Bullpen Web GUI — SSE log tailing.

This module provides ``_tail_log``, a generator that opens
``output/agent-log.jsonl``, seeks to the end, and polls for new lines
every 1 second. Each new line is yielded as a Server-Sent Events ``data:``
frame. A synthetic ``pipeline_complete`` event is emitted when the pipeline
thread exits, and the generator closes cleanly when the client disconnects.

Implemented in Task 4.
"""

# Standard library
import time
from pathlib import Path
from typing import Generator
