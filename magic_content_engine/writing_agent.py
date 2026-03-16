"""Writing_Sub_Agent core — framework for content generation.

Provides the shared infrastructure that all output-type generators
(blog, YouTube, CFP, user group, digest) build on:

- WritingContext dataclass carrying invocation parameters
- LLMProtocol for testable LLM call seam
- Voice-rule validation (banned phrases, em-dashes, "I" openings)
- Model routing via OUTPUT_TYPE_TO_TASK mapping
- generate_content() orchestration with error collection

Requirements: REQ-010.1, REQ-010.2, REQ-018.1–REQ-018.8,
             REQ-019.2, REQ-027.3
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from magic_content_engine.errors import ErrorCollector, StepError
from magic_content_engine.model_router import TaskType, get_model
from magic_content_engine.models import APACitation, Article
from magic_content_engine.steering import load_steering

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ArticleWithCitation:
    """An article bundled with its APA citation for the writing agent."""

    article: Article
    citation: APACitation


@dataclass
class WritingContext:
    """All parameters the Writing_Sub_Agent needs for a single invocation."""

    articles: list[ArticleWithCitation]
    output_type: str  # "blog" | "youtube" | "cfp" | "usergroup" | "digest"
    steering_base_path: str
    screenshots_path: str
    run_date: date
    slug: str


# ---------------------------------------------------------------------------
# LLM protocol — testable seam
# ---------------------------------------------------------------------------


class LLMProtocol(Protocol):
    """Protocol for LLM generation calls.

    Any callable matching this signature can be injected, making the
    writing agent testable without real Bedrock calls.
    """

    def __call__(self, *, model_id: str, prompt: str) -> str:
        """Generate text from *prompt* using the given *model_id*.

        Returns:
            The generated text content.

        Raises:
            Exception: On any generation failure.
        """
        ...


# ---------------------------------------------------------------------------
# Voice rules
# ---------------------------------------------------------------------------

VOICE_BANNED_PHRASES: list[str] = [
    "leverage",
    "empower",
    "unlock",
    "dive into",
    "game-changer",
]

# Pre-compiled pattern for em-dash detection (Unicode U+2014 and HTML entity)
_EM_DASH_RE = re.compile(r"\u2014|&#8212;")

# Pattern to detect paragraphs opening with "I" (after optional whitespace)
_PARA_OPENS_WITH_I_RE = re.compile(r"(?:^|\n\n)\s*I\b")


def validate_voice_rules(text: str) -> list[str]:
    """Check *text* against voice rules and return a list of violations.

    Checks performed (per REQ-018.3–REQ-018.5):
    - No banned phrases (case-insensitive)
    - No em-dashes (U+2014 or ``&#8212;``)
    - No paragraph or section opening with the word "I"

    Returns:
        A list of human-readable violation strings.  Empty list means
        the text passes all voice-rule checks.
    """
    violations: list[str] = []

    text_lower = text.lower()
    for phrase in VOICE_BANNED_PHRASES:
        if phrase.lower() in text_lower:
            violations.append(f"Banned phrase found: '{phrase}'")

    if _EM_DASH_RE.search(text):
        violations.append("Em-dash character found")

    if _PARA_OPENS_WITH_I_RE.search(text):
        violations.append("Paragraph or section opens with 'I'")

    return violations


# ---------------------------------------------------------------------------
# Output-type → TaskType mapping
# ---------------------------------------------------------------------------

OUTPUT_TYPE_TO_TASK: dict[str, TaskType] = {
    "blog": TaskType.BLOG_POST,
    "youtube": TaskType.YOUTUBE_SCRIPT,
    "cfp": TaskType.CFP_ABSTRACT,
    "usergroup": TaskType.USERGROUP_OUTLINE,
    "digest": TaskType.DIGEST_EMAIL,
}


# ---------------------------------------------------------------------------
# Content generation orchestration
# ---------------------------------------------------------------------------


def generate_content(
    context: WritingContext,
    llm: LLMProtocol,
    collector: ErrorCollector,
) -> str | None:
    """Generate a single content output for the given *context*.

    Steps:
    1. Load steering files for the output type.
    2. Resolve the correct model via the model router.
    3. Build a prompt from steering + article context.
    4. Call the LLM.
    5. Validate voice rules on the output.
    6. Return the generated content, or ``None`` on failure.

    On any failure the error is recorded via *collector* and ``None``
    is returned (log-and-continue per REQ-027.3).
    """
    output_type = context.output_type

    # 1. Load steering -------------------------------------------------------
    try:
        steering = load_steering(context.steering_base_path, output_type)
    except FileNotFoundError as exc:
        collector.add(
            StepError(
                step="generate",
                target=output_type,
                error_message=f"Steering file missing: {exc}",
                context={"output_type": output_type},
            )
        )
        return None

    # 2. Resolve model -------------------------------------------------------
    task_type = OUTPUT_TYPE_TO_TASK.get(output_type)
    if task_type is None:
        collector.add(
            StepError(
                step="generate",
                target=output_type,
                error_message=f"Unknown output type: {output_type}",
                context={"output_type": output_type},
            )
        )
        return None

    model_id = get_model(task_type)

    # 3. Build prompt --------------------------------------------------------
    prompt = _build_prompt(context, steering)

    # 4. Call LLM ------------------------------------------------------------
    try:
        content = llm(model_id=model_id, prompt=prompt)
    except Exception as exc:
        collector.add(
            StepError(
                step="generate",
                target=output_type,
                error_message=str(exc),
                context={"output_type": output_type, "model_id": model_id},
            )
        )
        return None

    # 5. Validate voice rules ------------------------------------------------
    violations = validate_voice_rules(content)
    if violations:
        logger.warning(
            "Voice-rule violations in %s output: %s",
            output_type,
            "; ".join(violations),
        )

    return content


# ---------------------------------------------------------------------------
# Prompt builder (minimal scaffold — output-specific prompts in 13.4–13.8)
# ---------------------------------------------------------------------------


def build_blog_prompt(context: WritingContext, steering: dict[str, str]) -> str:
    """Build the LLM prompt specifically for blog post generation.

    Instructs the model to produce the body sections (architecture walkthrough,
    build walkthrough with inline APA in-text citations, cost breakdown, and
    sample output) that ``assemble_blog_post`` will wrap with placeholders and
    structural chrome.

    Requirements: REQ-011.1–REQ-011.10
    """
    parts: list[str] = []

    # Voice rules
    parts.append("## Voice and style rules\n")
    parts.append(steering["voice"])

    # Output template
    if "template" in steering:
        parts.append("\n## Output template\n")
        parts.append(steering["template"])

    # Articles with citations
    parts.append("\n## Articles to cover (use inline APA in-text citations)\n")
    for item in context.articles:
        a = item.article
        c = item.citation
        parts.append(f"- **{a.title}** ({a.source})")
        parts.append(f"  Score: {a.relevance_score}  In-text: {c.in_text_citation}")
        parts.append(f"  URL: {a.url}\n")

    # Instructions
    parts.append("\n## Generation instructions\n")
    parts.append(
        "Generate the following sections for a dev.to blog post in Markdown:\n"
        "1. Architecture section: explain the system architecture. "
        "Reference the console screenshot as `![Architecture](screenshots/console-runtime.png)`.\n"
        "2. Build walkthrough: step-by-step guide with inline APA in-text citations "
        "like (Author, 2025) where relevant. Reference console screenshots as needed "
        "(e.g. `![Console](screenshots/console-gateway.png)`).\n"
        "3. Cost breakdown: a Markdown table with columns Service, Usage, "
        "Estimated Cost.\n"
        "4. Sample output: describe what the output looks like. "
        "Reference `![Sample output](screenshots/sample-output.png)`.\n\n"
        "Do NOT write the hook, Aotearoa angle, or closing. "
        "Those are placeholder sections added separately.\n"
        "Do NOT open any paragraph with the word 'I'.\n"
        "Do NOT use banned phrases: leverage, empower, unlock, dive into, game-changer.\n"
        "Do NOT use em-dashes."
    )

    # Run metadata
    parts.append(f"\n## Run metadata\n")
    parts.append(f"- Run date: {context.run_date.isoformat()}")
    parts.append(f"- Slug: {context.slug}")
    parts.append(f"- Screenshots path: {context.screenshots_path}")

    return "\n".join(parts)


def assemble_blog_post(context: WritingContext, generated_body: str) -> str:
    """Assemble the final ``post.md`` with all required sections and placeholders.

    The LLM generates the body content; this function wraps it with the
    structural chrome required by ``03-output-blog-post.md``:

    - Hook placeholder with 2-3 agent-suggested angles
    - Architecture section with screenshot ref
    - Build walkthrough (from generated body, includes inline APA citations)
    - Cost breakdown table (from generated body)
    - Sample output section with screenshot ref
    - Oceania angle placeholder (~60 words)
    - Closing placeholder (~50 words, CTA, GitHub link)
    - References section (APA citations sorted alphabetically)

    Requirements: REQ-011.1–REQ-011.10
    """
    parts: list[str] = []

    # --- Title ---
    parts.append(f"# {context.slug.replace('-', ' ').title()}\n")

    # --- Hook placeholder with agent-suggested angles ---
    top_titles = [item.article.title for item in context.articles[:3]]
    angle_lines = "\n".join(f"  - {t}" for t in top_titles) if top_titles else "  - (no articles available)"
    parts.append(
        "<!-- MIKE: [Write a personal hook. 2-3 agent-suggested angles below, ~100 words]\n"
        f"  Suggested angles based on top-scored articles:\n"
        f"{angle_lines}\n"
        "-->\n"
    )

    # --- Generated body (architecture, build walkthrough, cost, sample output) ---
    parts.append(generated_body.strip())
    parts.append("")

    # --- Oceania angle placeholder ---
    parts.append(
        "<!-- MIKE: [Aotearoa angle, ~60 words] -->\n"
    )

    # --- Closing placeholder ---
    parts.append(
        "<!-- MIKE: [Closing with CTA and GitHub link, ~50 words] -->\n"
    )

    # --- References section (APA citations sorted alphabetically) ---
    sorted_citations = sorted(
        context.articles,
        key=lambda item: item.citation.reference_entry.lower(),
    )
    parts.append("## References\n")
    for item in sorted_citations:
        parts.append(item.citation.reference_entry)
    parts.append("")

    return "\n".join(parts)


def build_youtube_prompt(context: WritingContext, steering: dict[str, str]) -> str:
    """Build the LLM prompt for YouTube script generation.

    Instructs the model to produce the four scripted sections (The Problem,
    Architecture Walkthrough, The Build, Results + Cost) that
    ``assemble_youtube_script`` will wrap with placeholders and structural
    chrome.

    Requirements: REQ-012.1–REQ-012.7
    """
    parts: list[str] = []

    # Voice rules
    parts.append("## Voice and style rules\n")
    parts.append(steering["voice"])

    # Output template
    if "template" in steering:
        parts.append("\n## Output template\n")
        parts.append(steering["template"])

    # Articles with citations
    parts.append("\n## Articles to cover (use inline APA in-text citations)\n")
    for item in context.articles:
        a = item.article
        c = item.citation
        parts.append(f"- **{a.title}** ({a.source})")
        parts.append(f"  Score: {a.relevance_score}  In-text: {c.in_text_citation}")
        parts.append(f"  URL: {a.url}\n")

    # Instructions
    parts.append("\n## Generation instructions\n")
    parts.append(
        "Generate four scripted sections for a YouTube video in Markdown:\n"
        "1. **## The Problem** (~200 words): explain the problem this content addresses.\n"
        "2. **## Architecture Walkthrough** (~300 words): walk through the architecture. "
        "Include B-roll cues referencing actual screenshot filenames, e.g. "
        "`[B-ROLL: screenshots/console-runtime.png]`.\n"
        "3. **## The Build** (~400 words): step-by-step build walkthrough.\n"
        "4. **## Results + Cost** (~150 words): show results and cost breakdown.\n\n"
        "Do NOT write the cold open, outro, thumbnail concept, or description. "
        "Those are handled separately.\n"
        "Do NOT open any paragraph with the word 'I'.\n"
        "Do NOT use banned phrases: leverage, empower, unlock, dive into, game-changer.\n"
        "Do NOT use em-dashes."
    )

    # Run metadata
    parts.append(f"\n## Run metadata\n")
    parts.append(f"- Run date: {context.run_date.isoformat()}")
    parts.append(f"- Slug: {context.slug}")
    parts.append(f"- Screenshots path: {context.screenshots_path}")

    return "\n".join(parts)


def assemble_youtube_script(context: WritingContext, generated_body: str) -> str:
    """Assemble the final ``script.md`` with all required sections and placeholders.

    The LLM generates the four scripted sections; this function wraps them
    with the structural chrome required by ``04-output-youtube.md``:

    - Thumbnail concept (2-3 words + visual description based on top article)
    - Cold open placeholder (30-45s, topic suggestion from top article)
    - Four scripted sections (from generated body)
    - Outro placeholder (~30s)

    Requirements: REQ-012.1–REQ-012.7
    """
    parts: list[str] = []

    # --- Title ---
    parts.append(f"# {context.slug.replace('-', ' ').title()} — YouTube Script\n")

    # --- Thumbnail concept ---
    top_title = context.articles[0].article.title if context.articles else "AWS Update"
    parts.append("## Thumbnail Concept\n")
    parts.append(f"**{top_title[:40]}**")
    parts.append(f"Visual: Close-up of console showing {top_title.lower()}, bold text overlay.\n")

    # --- Cold open placeholder ---
    topic_suggestion = context.articles[0].article.title if context.articles else "this week's AWS updates"
    parts.append(
        f"<!-- MIKE: [Cold open, 30-45s to camera. Topic suggestion: {topic_suggestion}, ~no script] -->\n"
    )

    # --- Generated body (four scripted sections) ---
    parts.append(generated_body.strip())
    parts.append("")

    # --- Outro placeholder ---
    parts.append(
        "<!-- MIKE: [Outro, ~30s to camera] -->\n"
    )

    return "\n".join(parts)


def assemble_youtube_description(context: WritingContext) -> str:
    """Assemble the ``description.txt`` for the YouTube video.

    Produces ~150 words with required hashtags.

    Requirements: REQ-012.3, REQ-012.7
    """
    parts: list[str] = []

    # Summary from articles
    top_titles = [item.article.title for item in context.articles[:5]]
    if top_titles:
        parts.append(f"This week: {', '.join(top_titles)}.")
    else:
        parts.append("This week's AWS and Kiro IDE updates.")

    parts.append("")
    parts.append(
        "Building AI Engineering tooling on AWS using Kiro IDE, "
        "from the Aotearoa builder perspective. "
        "Covering AgentCore, Strands Agents SDK, Bedrock, and the MCP protocol. "
        "Part of the AWS Community Builders programme."
    )

    # Links
    parts.append("")
    for item in context.articles[:3]:
        parts.append(f"- {item.article.title}: {item.article.url}")

    # Hashtags
    parts.append("")
    parts.append("#AWS #AWSCommunity #KiroIDE #AgentCore #BuildOnAWS #Aotearoa")

    return "\n".join(parts)

def build_cfp_prompt(context: WritingContext, steering: dict[str, str]) -> str:
    """Build the LLM prompt specifically for CFP proposal generation.

    Instructs the model to produce the abstract body, three takeaways, and
    target audience description that ``assemble_cfp_proposal`` will wrap
    with structural chrome (title options, session outlines, speaker bio,
    events list).

    Requirements: REQ-013.1–REQ-013.11
    """
    parts: list[str] = []

    # Voice rules
    parts.append("## Voice and style rules\n")
    parts.append(steering["voice"])

    # Output template
    if "template" in steering:
        parts.append("\n## Output template\n")
        parts.append(steering["template"])

    # Articles with citations
    parts.append("\n## Articles to cover (use inline APA in-text citations)\n")
    for item in context.articles:
        a = item.article
        c = item.citation
        parts.append(f"- **{a.title}** ({a.source})")
        parts.append(f"  Score: {a.relevance_score}  In-text: {c.in_text_citation}")
        parts.append(f"  URL: {a.url}\n")

    # CFP-specific instructions
    parts.append("\n## Generation instructions\n")
    parts.append(
        "Generate the following sections for a CFP proposal in Markdown:\n"
        "1. Abstract body: 250 words maximum. Describe the talk content, "
        "what attendees will learn, and why it matters right now.\n"
        "2. Three specific, actionable key takeaways that attendees will "
        "walk away with.\n"
        "3. Target audience description: specify skill level, role, and "
        "prior knowledge assumed.\n\n"
        "Do NOT write the title options, session outlines, speaker bio, "
        "personal note, or events list. Those are added separately.\n"
        "Do NOT open any paragraph with the word 'I'.\n"
        "Do NOT use banned phrases: leverage, empower, unlock, dive into, game-changer.\n"
        "Do NOT use em-dashes."
    )

    # Run metadata
    parts.append(f"\n## Run metadata\n")
    parts.append(f"- Run date: {context.run_date.isoformat()}")
    parts.append(f"- Slug: {context.slug}")
    parts.append(f"- Screenshots path: {context.screenshots_path}")

    return "\n".join(parts)


def assemble_cfp_proposal(context: WritingContext, generated_body: str) -> str:
    """Assemble the final ``cfp-proposal.md`` with all required sections.

    The LLM generates the abstract body, takeaways, and target audience;
    this function wraps them with the structural chrome required by
    ``05-output-talks.md`` (CFP section):

    - 3 title options (technical, community, personal story angles)
    - Abstract area (from generated body, max 250 words)
    - 3 takeaways (from generated body)
    - Target audience (from generated body)
    - 25-min session outline with time allocations
    - 45-min variant with extended demo + Q&A
    - Speaker bio (~100 words)
    - Personal note placeholder
    - Events list

    Requirements: REQ-013.1–REQ-013.11
    """
    parts: list[str] = []

    # --- Title ---
    parts.append(f"# CFP Proposal: {context.slug.replace('-', ' ').title()}\n")

    # --- 3 title options ---
    top_titles = [item.article.title for item in context.articles[:3]]
    topic = top_titles[0] if top_titles else "AI Engineering on AWS"

    parts.append("## Title Options\n")
    parts.append(f"1. **Technical:** Building {topic} with AgentCore and Kiro IDE")
    parts.append(f"2. **Community:** What {topic} means for builders in Aotearoa")
    parts.append(f"3. **Personal story:** From side project to production: shipping {topic} from Palmerston North\n")

    # --- Generated body (abstract, takeaways, target audience) ---
    parts.append(generated_body.strip())
    parts.append("")

    # --- 25-min session outline ---
    parts.append("## Session Outline (25 minutes)\n")
    parts.append("| Time | Section | Description |")
    parts.append("|------|---------|-------------|")
    parts.append("| 0:00-3:00 | Introduction | Context and problem statement |")
    parts.append("| 3:00-8:00 | Architecture | System design and key decisions |")
    parts.append("| 8:00-16:00 | Live demo | Building the solution step by step |")
    parts.append("| 16:00-20:00 | Results | What worked, what surprised us, costs |")
    parts.append("| 20:00-23:00 | Lessons learned | Practical takeaways for your own projects |")
    parts.append("| 23:00-25:00 | Q&A | Questions from the audience |\n")

    # --- 45-min variant ---
    parts.append("## Session Outline (45 minutes)\n")
    parts.append("| Time | Section | Description |")
    parts.append("|------|---------|-------------|")
    parts.append("| 0:00-5:00 | Introduction | Context, problem statement, and Aotearoa perspective |")
    parts.append("| 5:00-12:00 | Architecture | System design, service choices, and trade-offs |")
    parts.append("| 12:00-28:00 | Extended live demo | Full build walkthrough with audience interaction |")
    parts.append("| 28:00-35:00 | Results and cost | Production metrics, cost breakdown, surprises |")
    parts.append("| 35:00-40:00 | Lessons learned | Practical takeaways and next steps |")
    parts.append("| 40:00-45:00 | Q&A | Extended questions and discussion |\n")

    # --- Speaker bio ---
    parts.append("## Speaker Bio\n")
    parts.append(
        "Mike Rewiri-Thorsen is an AWS Community Builder in AI Engineering (2026 cohort), "
        "based in Palmerston North, Aotearoa New Zealand. "
        "Builder of the kiro-steering-docs-extension for Kiro IDE and co-organiser of "
        "AWS User Group Oceania. "
        "Currently building AI Engineering tooling on AWS using Kiro IDE, AgentCore, "
        "and the Strands Agents SDK. "
        "Focused on making agentic AI practical for builders in the Pacific region.\n"
    )

    # --- Personal note placeholder ---
    parts.append("<!-- MIKE: [Personal note for this CFP, ~50 words] -->\n")

    # --- Events list ---
    parts.append("## Target Events\n")
    parts.append("- AWS Summit Sydney")
    parts.append("- AWS Summit Auckland")
    parts.append("- AWS Community Day Oceania")
    parts.append("- KiwiCon")
    parts.append("- YOW! Conference")
    parts.append("- DevOpsDays NZ")
    parts.append("- NDC Sydney")
    parts.append("")

    return "\n".join(parts)



def build_usergroup_prompt(context: WritingContext, steering: dict[str, str]) -> str:
    """Build the LLM prompt for user group session outline generation.

    Instructs the model to produce the session outline body (explanation +
    live demo + audience participation) and step-by-step live demo
    instructions that ``assemble_usergroup_session`` will wrap with
    structural chrome (recommended format, opening story placeholder,
    slide outline).

    Requirements: REQ-014.1–REQ-014.7
    """
    parts: list[str] = []

    # Voice rules
    parts.append("## Voice and style rules\n")
    parts.append(steering["voice"])

    # Output template
    if "template" in steering:
        parts.append("\n## Output template\n")
        parts.append(steering["template"])

    # Articles with citations
    parts.append("\n## Articles to cover (use inline APA in-text citations)\n")
    for item in context.articles:
        a = item.article
        c = item.citation
        parts.append(f"- **{a.title}** ({a.source})")
        parts.append(f"  Score: {a.relevance_score}  In-text: {c.in_text_citation}")
        parts.append(f"  URL: {a.url}\n")

    # User-group-specific instructions
    parts.append("\n## Generation instructions\n")
    parts.append(
        "Generate the following sections for a user group session outline in Markdown:\n"
        "1. **## Session Outline**: a session outline for a community audience that mixes "
        "explanation, live demo, and one audience participation moment. "
        "Structure it as a numbered list of segments with time estimates.\n"
        "2. **## Live Demo Instructions**: step-by-step live demo instructions "
        "that are followable on stage. Each step should be a numbered item "
        "with a clear action and expected result.\n\n"
        "Do NOT write the recommended format section, opening story, or slide outline. "
        "Those are added separately.\n"
        "Do NOT open any paragraph with the word 'I'.\n"
        "Do NOT use banned phrases: leverage, empower, unlock, dive into, game-changer.\n"
        "Do NOT use em-dashes."
    )

    # Run metadata
    parts.append(f"\n## Run metadata\n")
    parts.append(f"- Run date: {context.run_date.isoformat()}")
    parts.append(f"- Slug: {context.slug}")
    parts.append(f"- Screenshots path: {context.screenshots_path}")

    return "\n".join(parts)


def assemble_usergroup_session(context: WritingContext, generated_body: str) -> str:
    """Assemble the final ``usergroup-session.md`` with all required sections.

    The LLM generates the session outline and live demo instructions; this
    function wraps them with the structural chrome required by
    ``05-output-talks.md`` (user group section):

    - Title
    - Recommended format (lightning 10min / standard 30min / workshop 60min)
      with rationale based on article count and complexity
    - Opening story placeholder
    - Generated body (session outline + live demo instructions)
    - Slide outline (title + one-line description, max 12 for 30min)

    Requirements: REQ-014.1–REQ-014.7
    """
    parts: list[str] = []

    # --- Title ---
    parts.append(f"# User Group Session: {context.slug.replace('-', ' ').title()}\n")

    # --- Recommended format with rationale ---
    article_count = len(context.articles)
    if article_count <= 2:
        recommended = "Lightning (10 minutes)"
        rationale = (
            f"With {article_count} article{'s' if article_count != 1 else ''} to cover, "
            "a lightning talk keeps the content focused and punchy."
        )
    elif article_count <= 5:
        recommended = "Standard (30 minutes)"
        rationale = (
            f"With {article_count} articles to cover, a 30-minute session "
            "gives enough room for explanation, a live demo, and audience questions."
        )
    else:
        recommended = "Workshop (60 minutes)"
        rationale = (
            f"With {article_count} articles spanning multiple topics, "
            "a workshop format allows hands-on exploration and deeper discussion."
        )

    parts.append("## Recommended Format\n")
    parts.append(f"**{recommended}**\n")
    parts.append(f"{rationale}\n")
    parts.append("Available formats:")
    parts.append("- Lightning: 10 minutes")
    parts.append("- Standard: 30 minutes")
    parts.append("- Workshop: 60 minutes\n")

    # --- Opening story placeholder ---
    parts.append("<!-- MIKE: [Opening story for this session, ~50 words] -->\n")

    # --- Generated body (session outline + live demo instructions) ---
    parts.append(generated_body.strip())
    parts.append("")

    # --- Slide outline (max 12 slides for 30min) ---
    parts.append("## Slide Outline\n")
    slides: list[tuple[str, str]] = [
        ("Title Slide", f"{context.slug.replace('-', ' ').title()} — User Group Session"),
        ("Agenda", "What we will cover today and key takeaways"),
    ]

    # Add slides from article topics (up to 8 content slides to stay within 12 total)
    for item in context.articles[:8]:
        slides.append((item.article.title, f"Key points from {item.article.source}"))

    # Closing slides
    if len(slides) < 11:
        slides.append(("Live Demo Recap", "Summary of what we built and saw"))
    if len(slides) < 12:
        slides.append(("Questions and Discussion", "Open floor for audience questions"))

    # Cap at 12 slides
    slides = slides[:12]

    for i, (title, description) in enumerate(slides, 1):
        parts.append(f"{i}. **{title}** — {description}")
    parts.append("")

    return "\n".join(parts)


def build_digest_prompt(context: WritingContext, steering: dict[str, str]) -> str:
    """Build the LLM prompt for weekly digest email generation.

    Instructs Claude Haiku to produce 3-4 plain English sentences per article,
    grouped by theme when 5+ articles are present. The result is wrapped by
    ``assemble_digest_email`` with a personal note placeholder and sign-off.

    Requirements: REQ-015.1–REQ-015.5
    """
    parts: list[str] = []

    # Voice rules (digest loads voice file only, no output-specific template)
    parts.append("## Voice and style rules\n")
    parts.append(steering["voice"])

    # Articles
    parts.append("\n## Articles to summarise\n")
    for item in context.articles:
        a = item.article
        parts.append(f"- **{a.title}** ({a.source})")
        parts.append(f"  Score: {a.relevance_score}")
        parts.append(f"  URL: {a.url}\n")

    # Digest-specific instructions
    parts.append("\n## Generation instructions\n")
    parts.append(
        "Generate a plain-text weekly digest email body.\n"
        "For each article, write 3-4 sentences in plain English summarising "
        "what it covers and why it matters.\n"
    )
    if len(context.articles) >= 5:
        parts.append(
            "There are 5 or more articles. Group them by theme with a short "
            "theme heading before each group.\n"
        )
    parts.append(
        "Do NOT write the personal note or sign-off. Those are added separately.\n"
        "Do NOT open any paragraph with the word 'I'.\n"
        "Do NOT use banned phrases: leverage, empower, unlock, dive into, game-changer.\n"
        "Do NOT use em-dashes.\n"
        "Output plain text only, no Markdown formatting."
    )

    # Run metadata
    parts.append(f"\n## Run metadata\n")
    parts.append(f"- Run date: {context.run_date.isoformat()}")
    parts.append(f"- Slug: {context.slug}")

    return "\n".join(parts)


def assemble_digest_email(context: WritingContext, generated_body: str) -> str:
    """Assemble the final ``digest-email.txt`` with structural chrome.

    The LLM generates the article summaries; this function wraps them with:

    - Personal note placeholder (2-3 sentences)
    - Generated body (article summaries from LLM)
    - Simple sign-off line

    Requirements: REQ-015.1–REQ-015.5
    """
    parts: list[str] = []

    # Personal note placeholder
    parts.append("<!-- MIKE: [Personal note for this digest, 2-3 sentences] -->\n")

    # Generated body
    parts.append(generated_body.strip())
    parts.append("")

    # Sign-off
    parts.append("Ngā mihi,")
    parts.append("Mike")
    parts.append("")

    return "\n".join(parts)


def _build_prompt(context: WritingContext, steering: dict[str, str]) -> str:
    """Assemble the LLM prompt from steering files and article context.

    This builds a generic prompt structure.  Tasks 13.4–13.8 will extend
    this with output-type-specific prompt templates.
    """
    parts: list[str] = []

    # Voice rules (always present)
    parts.append("## Voice and style rules\n")
    parts.append(steering["voice"])

    # Output template (present for blog, youtube, cfp, usergroup)
    if "template" in steering:
        parts.append("\n## Output template\n")
        parts.append(steering["template"])

    # Article context
    parts.append("\n## Articles to cover\n")
    for item in context.articles:
        a = item.article
        c = item.citation
        parts.append(f"- **{a.title}** ({a.source})")
        parts.append(f"  Score: {a.relevance_score}  Citation: {c.in_text_citation}")
        parts.append(f"  URL: {a.url}\n")

    # Run metadata
    parts.append(f"\n## Run metadata\n")
    parts.append(f"- Run date: {context.run_date.isoformat()}")
    parts.append(f"- Slug: {context.slug}")
    parts.append(f"- Screenshots path: {context.screenshots_path}")
    parts.append(f"- Output type: {context.output_type}")

    return "\n".join(parts)
