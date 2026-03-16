"""Embargo release check for held content outputs.

At run start, queries long-term memory for HeldItems whose release_date
has arrived (≤ today). Released items are listed to the user, SES
notifications are sent (failures logged, no retry), and the user
confirms which items to include in the Publish_Gate queue. Confirmed
items are removed from memory and returned for S3 upload.

Requirements: REQ-031.1–REQ-031.6, REQ-032.1–REQ-032.5
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Callable, Protocol, runtime_checkable

from magic_content_engine.errors import ErrorCollector, log_ses_failure
from magic_content_engine.models import HeldItem

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocols (testable seams)
# ---------------------------------------------------------------------------


@runtime_checkable
class HeldItemMemoryProtocol(Protocol):
    """Protocol for loading/saving/removing HeldItems from long-term memory."""

    def load_held_items(self) -> list[HeldItem]:
        """Return all HeldItems currently stored in memory."""
        ...

    def save_held_item(self, item: HeldItem) -> None:
        """Persist a single HeldItem to memory."""
        ...

    def remove_held_item(self, item: HeldItem) -> None:
        """Remove a single HeldItem from memory."""
        ...


@runtime_checkable
class SESNotifierProtocol(Protocol):
    """Protocol for sending SES notifications (testable seam)."""

    def send_embargo_release(self, item: HeldItem) -> None:
        """Send an embargo-lifted notification for *item*.

        Subject: "Magic Content Engine — embargo lifted: [title]"
        where [title] is derived from the item's article_titles.
        """
        ...


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_released_items(items: list[HeldItem]) -> str:
    """Format a list of released HeldItems for terminal display.

    Each item shows: index, filename, run date, release date, path.
    """
    if not items:
        return "No embargoed items ready for release."

    lines: list[str] = ["Embargoed items ready for release:\n"]
    for idx, item in enumerate(items, start=1):
        lines.append(
            f"  [{idx}] {item.filename}\n"
            f"      Run date:     {item.run_date.isoformat()}\n"
            f"      Release date: {item.release_date.isoformat()}\n"
            f"      Path:         {item.local_file_path}"
        )
    return "\n".join(lines)



# ---------------------------------------------------------------------------
# Core embargo release check
# ---------------------------------------------------------------------------


def check_embargo_releases(
    memory: HeldItemMemoryProtocol,
    ses: SESNotifierProtocol,
    collector: ErrorCollector,
    run_date: date,
    input_fn: Callable[[str], str] = input,
) -> list[HeldItem]:
    """Check for embargoed items whose release date has arrived.

    1. Query memory for HeldItems with release_date ≤ *run_date*.
    2. If none found, return empty list.
    3. List released items to user.
    4. Send SES notification per item (catch failures via log_ses_failure).
    5. Prompt user to confirm which items to include (by number).
    6. Remove confirmed items from memory.
    7. Return confirmed items for S3 upload.

    Parameters
    ----------
    memory:
        Long-term memory backend for HeldItem persistence.
    ses:
        SES notification sender.
    collector:
        Error collector for logging SES failures.
    run_date:
        The current run date used to determine which items are released.
    input_fn:
        Callable for reading user input. Override in tests.

    Returns
    -------
    list[HeldItem]
        The items the user confirmed for inclusion in the Publish_Gate queue.
    """
    # --- Step 1: Query memory for released items ---
    all_held = memory.load_held_items()
    released = [item for item in all_held if item.release_date <= run_date]

    # --- Log the embargo check ---
    logger.info(
        "Embargo check: %d held items total, %d released (run_date=%s)",
        len(all_held),
        len(released),
        run_date.isoformat(),
    )

    # --- Step 2: Nothing to release ---
    if not released:
        return []

    # --- Step 3: Display released items ---
    print(f"\n{format_released_items(released)}")

    # --- Step 4: Send SES notification per item ---
    for item in released:
        try:
            ses.send_embargo_release(item)
            logger.info("SES notification sent for: %s", item.filename)
        except Exception as exc:
            log_ses_failure(exc, item.filename, collector)

    # --- Step 5: Prompt user for confirmation ---
    print(
        "\nEnter the numbers of items to include in this run's Publish Gate "
        "(comma-separated), or 'all' for all, or 'none' to skip:"
    )
    raw = input_fn("Include items: ").strip().lower()

    confirmed = _parse_selection(raw, released)

    # --- Step 6: Remove confirmed items from memory ---
    for item in confirmed:
        memory.remove_held_item(item)
        logger.info("Removed confirmed held item from memory: %s", item.filename)

    # --- Step 7: Return confirmed items ---
    logger.info("Embargo release: %d items confirmed for Publish Gate", len(confirmed))
    return confirmed


def _parse_selection(raw: str, items: list[HeldItem]) -> list[HeldItem]:
    """Parse user selection string into a list of confirmed HeldItems.

    Accepts:
    - ``"all"`` — return all items
    - ``"none"`` or ``""`` — return empty list
    - Comma-separated 1-based indices, e.g. ``"1,3"``
    """
    if raw == "all":
        return list(items)
    if raw in ("none", ""):
        return []

    selected: list[HeldItem] = []
    for part in raw.split(","):
        part = part.strip()
        try:
            idx = int(part) - 1  # 1-based to 0-based
            if 0 <= idx < len(items):
                selected.append(items[idx])
        except ValueError:
            continue
    return selected
