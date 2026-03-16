"""Unit tests for model_router module."""

import pytest

from magic_content_engine.config import HAIKU_MODEL_ID, SONNET_MODEL_ID
from magic_content_engine.model_router import MODEL_ROUTING, TaskType, get_model

HAIKU_TASKS = [
    TaskType.RELEVANCE_SCORING,
    TaskType.METADATA_EXTRACTION,
    TaskType.APA_CITATION,
    TaskType.DIGEST_EMAIL,
    TaskType.WEEKLY_BRIEF,
]

SONNET_TASKS = [
    TaskType.BLOG_POST,
    TaskType.YOUTUBE_SCRIPT,
    TaskType.CFP_ABSTRACT,
    TaskType.USERGROUP_OUTLINE,
]


@pytest.mark.parametrize("task", HAIKU_TASKS)
def test_haiku_tasks_return_haiku_model(task: TaskType) -> None:
    assert get_model(task) == HAIKU_MODEL_ID


@pytest.mark.parametrize("task", SONNET_TASKS)
def test_sonnet_tasks_return_sonnet_model(task: TaskType) -> None:
    assert get_model(task) == SONNET_MODEL_ID


def test_all_task_types_have_routing() -> None:
    """Every TaskType member must have an entry in MODEL_ROUTING."""
    for task in TaskType:
        assert task in MODEL_ROUTING, f"{task} missing from MODEL_ROUTING"


def test_routing_only_uses_known_models() -> None:
    """MODEL_ROUTING values should only be HAIKU or SONNET model IDs."""
    allowed = {HAIKU_MODEL_ID, SONNET_MODEL_ID}
    for task, model in MODEL_ROUTING.items():
        assert model in allowed, f"{task} routes to unexpected model: {model}"


def test_get_model_raises_on_invalid_key() -> None:
    """get_model should raise KeyError for a value not in the routing dict."""
    with pytest.raises(KeyError):
        get_model("not_a_task")  # type: ignore[arg-type]
