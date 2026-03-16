"""Steering file loader — reads voice and output-template files from disk at runtime.

Files are read on every invocation, never cached or hardcoded in agent definitions,
so steering updates take effect without redeployment.
"""

import pathlib

STEERING_MAPPING: dict[str, str] = {
    "blog": "03-output-blog-post.md",
    "youtube": "04-output-youtube.md",
    "cfp": "05-output-talks.md",
    "usergroup": "05-output-talks.md",
}


def load_steering(base_path: str, output_type: str) -> dict[str, str]:
    """Read steering files from disk at runtime. Never cached in agent definition.

    Args:
        base_path: Directory containing the steering markdown files.
        output_type: One of "blog", "youtube", "cfp", "usergroup", or "digest".

    Returns:
        A dict with a "voice" key (always present) and an optional "template" key
        for output types that have a dedicated steering file.

    Raises:
        FileNotFoundError: If any required steering file is missing on disk.
    """
    base = pathlib.Path(base_path)
    files: dict[str, pathlib.Path] = {"voice": base / "01-niche-and-voice.md"}

    if output_type in STEERING_MAPPING:
        files["template"] = base / STEERING_MAPPING[output_type]

    result: dict[str, str] = {}
    for key, path in files.items():
        if not path.exists():
            raise FileNotFoundError(f"Steering file missing: {path}")
        result[key] = path.read_text(encoding="utf-8")
    return result
