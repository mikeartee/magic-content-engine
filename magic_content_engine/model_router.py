"""Model routing — selects the Bedrock model ID for each task type.

Haiku handles structured tasks (scoring, metadata, citations, digest, brief).
Sonnet handles narrative writing (blog, YouTube, CFP, user group).
"""

from enum import Enum

from magic_content_engine.config import HAIKU_MODEL_ID, SONNET_MODEL_ID


class TaskType(Enum):
    RELEVANCE_SCORING = "relevance_scoring"
    METADATA_EXTRACTION = "metadata_extraction"
    APA_CITATION = "apa_citation"
    DIGEST_EMAIL = "digest_email"
    WEEKLY_BRIEF = "weekly_brief"
    BLOG_POST = "blog_post"
    YOUTUBE_SCRIPT = "youtube_script"
    CFP_ABSTRACT = "cfp_abstract"
    USERGROUP_OUTLINE = "usergroup_outline"


MODEL_ROUTING: dict[TaskType, str] = {
    TaskType.RELEVANCE_SCORING: HAIKU_MODEL_ID,
    TaskType.METADATA_EXTRACTION: HAIKU_MODEL_ID,
    TaskType.APA_CITATION: HAIKU_MODEL_ID,
    TaskType.DIGEST_EMAIL: HAIKU_MODEL_ID,
    TaskType.WEEKLY_BRIEF: HAIKU_MODEL_ID,
    TaskType.BLOG_POST: SONNET_MODEL_ID,
    TaskType.YOUTUBE_SCRIPT: SONNET_MODEL_ID,
    TaskType.CFP_ABSTRACT: SONNET_MODEL_ID,
    TaskType.USERGROUP_OUTLINE: SONNET_MODEL_ID,
}


def get_model(task: TaskType) -> str:
    """Return the Bedrock model ID for the given task type."""
    return MODEL_ROUTING[task]
