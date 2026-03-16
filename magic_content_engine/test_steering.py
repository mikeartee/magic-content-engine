"""Unit tests for the steering file loader."""

import pytest

from magic_content_engine.steering import STEERING_MAPPING, load_steering


def test_blog_loads_voice_and_template(tmp_path):
    """Blog output loads both voice and blog-post steering files."""
    (tmp_path / "01-niche-and-voice.md").write_text("voice content", encoding="utf-8")
    (tmp_path / "03-output-blog-post.md").write_text("blog template", encoding="utf-8")

    result = load_steering(str(tmp_path), "blog")

    assert result["voice"] == "voice content"
    assert result["template"] == "blog template"


def test_youtube_loads_voice_and_template(tmp_path):
    """YouTube output loads voice and youtube steering files."""
    (tmp_path / "01-niche-and-voice.md").write_text("voice", encoding="utf-8")
    (tmp_path / "04-output-youtube.md").write_text("yt template", encoding="utf-8")

    result = load_steering(str(tmp_path), "youtube")

    assert result["voice"] == "voice"
    assert result["template"] == "yt template"


def test_cfp_loads_talks_steering(tmp_path):
    """CFP output loads the talks steering file."""
    (tmp_path / "01-niche-and-voice.md").write_text("voice", encoding="utf-8")
    (tmp_path / "05-output-talks.md").write_text("talks", encoding="utf-8")

    result = load_steering(str(tmp_path), "cfp")

    assert result["template"] == "talks"


def test_usergroup_loads_talks_steering(tmp_path):
    """User group output shares the talks steering file with CFP."""
    (tmp_path / "01-niche-and-voice.md").write_text("voice", encoding="utf-8")
    (tmp_path / "05-output-talks.md").write_text("talks", encoding="utf-8")

    result = load_steering(str(tmp_path), "usergroup")

    assert result["template"] == "talks"


def test_digest_returns_voice_only(tmp_path):
    """Digest output returns only the voice file, no template key."""
    (tmp_path / "01-niche-and-voice.md").write_text("voice only", encoding="utf-8")

    result = load_steering(str(tmp_path), "digest")

    assert result["voice"] == "voice only"
    assert "template" not in result


def test_missing_voice_file_raises(tmp_path):
    """Missing voice file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError, match="01-niche-and-voice.md"):
        load_steering(str(tmp_path), "digest")


def test_missing_template_file_raises(tmp_path):
    """Missing template file raises FileNotFoundError when output type needs one."""
    (tmp_path / "01-niche-and-voice.md").write_text("voice", encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="03-output-blog-post.md"):
        load_steering(str(tmp_path), "blog")


def test_steering_mapping_keys():
    """STEERING_MAPPING contains exactly the expected output types."""
    assert set(STEERING_MAPPING.keys()) == {"blog", "youtube", "cfp", "usergroup"}
