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
import json
import time
from pathlib import Path
from typing import Any, Generator


def _tail_log(log_path: Path, run_state: Any) -> Generator[str, None, None]:
    """Yield new JSON Lines from *log_path* as SSE ``data:`` frames.

    Algorithm:
    1. Open the file and seek to the end so we only stream new content.
    2. Poll every 1 second for new lines.
    3. Yield each new line as ``data: <line>\\n\\n``.
    4. When the pipeline thread has exited (``run_state.in_progress == False``)
       and no new lines have arrived for 2 consecutive seconds, emit a
       synthetic ``pipeline_complete`` event and return.
    5. Handle ``GeneratorExit`` (client disconnect) by returning cleanly.
    """
    try:
        with open(log_path, "a+", encoding="utf-8") as fh:
            # Seek to end — only tail new content written after this point.
            fh.seek(0, 2)

            idle_seconds = 0  # seconds elapsed with no new lines after pipeline done

            while True:
                line = fh.readline()

                if line:
                    idle_seconds = 0
                    yield f"data: {line.rstrip()}\n\n"
                else:
                    # No new line available.
                    time.sleep(1)

                    if not run_state.in_progress:
                        idle_seconds += 1
                        if idle_seconds >= 2:
                            # Pipeline is done and no new lines for 2 s — emit
                            # the synthetic completion event and stop.
                            payload = json.dumps({"status": "complete"})
                            yield f"event: pipeline_complete\ndata: {payload}\n\n"
                            return
    except GeneratorExit:
        # Client disconnected — clean up and stop.
        return


def tail_log(run_state: Any) -> Generator[str, None, None]:
    """Return the ``_tail_log`` generator for *run_state*.

    Uses ``run_state.log_path`` if set (per-run log), otherwise falls back
    to the legacy ``output/agent-log.jsonl`` path.
    """
    log_path = getattr(run_state, "log_path", None)
    if log_path is None:
        repo_root = Path(__file__).resolve().parent.parent.parent
        log_path = repo_root / "output" / "agent-log.jsonl"
    else:
        log_path = Path(log_path)

    # Ensure the file exists so the tailer can open it immediately.
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)

    return _tail_log(log_path, run_state)
