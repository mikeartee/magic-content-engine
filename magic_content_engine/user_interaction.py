"""User interaction for scored article confirmation, removal, and output selection.

Presents a numbered list of articles scoring >= 3, waits for user
confirmation, and allows article removal by number. Removals are
recorded in the Agent_Log.

After confirmation, presents the output choice prompt for the user
to select which content outputs to generate.

Requirements: REQ-008.1, REQ-008.2, REQ-008.3, REQ-008.4,
             REQ-009.1, REQ-009.2, REQ-009.3
"""

from __future__ import annotations

import logging
import re
from typing import Callable

from magic_content_engine.models import Article

logger = logging.getLogger(__name__)


def format_article_list(articles: list[Article]) -> str:
    """Format a numbered list of scored articles for terminal display.

    Each entry shows: number, title, source, score, and the scoring
    rationale as a one-sentence summary.

    Returns the formatted string (no trailing newline).
    """
    if not articles:
        return "No articles to display."

    lines: list[str] = []
    for i, article in enumerate(articles, start=1):
        summary = article.scoring_rationale or "No summary available."
        lines.append(
            f"  [{i}] {article.title}\n"
            f"      Source: {article.source}  |  Score: {article.relevance_score}\n"
            f"      {summary}"
        )
    return "\n".join(lines)


def parse_removal_input(raw: str, max_index: int) -> list[int]:
    """Parse a removal command into a sorted list of 1-based indices.

    Accepted formats:
    - "remove 1,3"  or  "remove 1, 3"
    - "r 1 3"       or  "r 1,3"
    - "1,3"         or  "1 3"

    Numbers outside [1, max_index] are silently ignored.
    Returns a sorted, deduplicated list of valid indices.
    """
    # Strip optional "remove" / "r" prefix
    cleaned = re.sub(r"^(remove|r)\s*", "", raw.strip(), flags=re.IGNORECASE)

    # Split on commas and/or whitespace
    tokens = re.split(r"[,\s]+", cleaned)

    indices: set[int] = set()
    for token in tokens:
        if token.isdigit():
            num = int(token)
            if 1 <= num <= max_index:
                indices.add(num)

    return sorted(indices)


def present_scored_articles(
    articles: list[Article],
    input_fn: Callable[[str], str] = input,
) -> tuple[list[Article], list[Article]]:
    """Present scored articles and collect user confirmation/removal.

    Prints the numbered article list, then prompts the user to confirm
    or remove articles. The user can:
    - Press Enter or type "y" to confirm the full list.
    - Type a removal command (e.g. "remove 2,4" or "r 2 4" or "1,3")
      to remove articles by number.

    After removal the updated list is re-displayed and the user is
    prompted again until they confirm.

    Parameters
    ----------
    articles:
        Scored articles (relevance_score >= 3) to present.
    input_fn:
        Callable used to read user input. Defaults to built-in
        ``input()``; override in tests.

    Returns
    -------
    tuple of (confirmed_articles, removed_articles)
    """
    if not articles:
        logger.info("No scored articles to present.")
        return [], []

    remaining = list(articles)
    removed: list[Article] = []

    while True:
        print("\nScored articles:\n")
        print(format_article_list(remaining))
        print()

        response = input_fn(
            'Confirm (Enter/y) or remove articles (e.g. "remove 2,4"): '
        ).strip()

        # Confirm: empty input or "y"/"yes"
        if response == "" or response.lower() in ("y", "yes"):
            break

        # Parse removal
        to_remove = parse_removal_input(response, len(remaining))
        if not to_remove:
            print("No valid article numbers found. Try again.")
            continue

        # Remove in reverse order to keep indices stable
        newly_removed: list[Article] = []
        for idx in reversed(to_remove):
            newly_removed.append(remaining.pop(idx - 1))

        newly_removed.reverse()  # restore original order
        removed.extend(newly_removed)

        titles = ", ".join(a.title for a in newly_removed)
        logger.info("User removed %d article(s): %s", len(newly_removed), titles)

        if not remaining:
            print("All articles removed.")
            break

    return remaining, removed

# ---------------------------------------------------------------------------
# Output choice prompt (REQ-009)
# ---------------------------------------------------------------------------

OUTPUT_OPTIONS: dict[int, str] = {
    1: "blog",
    2: "youtube",
    3: "cfp",
    4: "usergroup",
    5: "digest",
}

UNATTENDED_DEFAULTS: list[str] = ["blog", "youtube"]


def format_output_choices() -> str:
    """Format the numbered output options for terminal display."""
    lines = [
        "  [1] Blog post",
        "  [2] YouTube script",
        "  [3] CFP proposal",
        "  [4] User group session outline",
        "  [5] Weekly digest email",
        "  [6] All of the above",
    ]
    return "\n".join(lines)


def _parse_output_selection(raw: str) -> list[int]:
    """Parse user input into a list of selected option numbers.

    Accepts comma-separated and/or space-separated numbers.
    Returns sorted, deduplicated list of valid option numbers (1-6).
    """
    tokens = re.split(r"[,\s]+", raw.strip())
    numbers: set[int] = set()
    for token in tokens:
        if token.isdigit():
            num = int(token)
            if 1 <= num <= 6:
                numbers.add(num)
    return sorted(numbers)


def prompt_output_choice(
    input_fn: Callable[[str], str] = input,
    unattended: bool = False,
) -> list[str]:
    """Present output options and return the user's selection.

    In unattended mode, returns UNATTENDED_DEFAULTS without prompting.

    Parameters
    ----------
    input_fn:
        Callable used to read user input. Defaults to built-in
        ``input()``; override in tests.
    unattended:
        When True, skip the prompt and return default outputs.

    Returns
    -------
    list of selected output type strings (e.g. ["blog", "youtube"])
    """
    if unattended:
        logger.info("Unattended mode: defaulting to %s", UNATTENDED_DEFAULTS)
        return list(UNATTENDED_DEFAULTS)

    while True:
        print("\nSelect output(s) to generate:\n")
        print(format_output_choices())
        print()

        response = input_fn("Enter option number(s) (e.g. 1,3 or 1 3 or 6): ").strip()

        selected = _parse_output_selection(response)
        if not selected:
            print("No valid option numbers found. Try again.")
            continue

        # Option 6 means all
        if 6 in selected:
            result = list(OUTPUT_OPTIONS.values())
        else:
            result = [OUTPUT_OPTIONS[n] for n in selected if n in OUTPUT_OPTIONS]

        if result:
            logger.info("User selected outputs: %s", result)
            return result

        print("No valid option numbers found. Try again.")
