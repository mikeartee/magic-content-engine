---
title: "Output: YouTube script"
inclusion: manual
version: "1.0.0"
---

# YouTube script format

## Structure

```markdown
# [AGENT: working title]

## Thumbnail concept
[AGENT: 2–3 words for thumbnail text + one-line visual description]

## YouTube description
[AGENT: 150 words, timestamps placeholder, hashtags:
#AWS #AWSCommunity #KiroIDE #AgentCore #BuildOnAWS #Aotearoa]

---

<!-- MIKE: Cold open — record to camera, no script, 30–45 sec.
Talk about what you found this week and why it caught your
attention. Don't read from notes.
Topic suggestion: [AGENT: one sentence based on top item] -->

## Section 1 — The problem (~200 words)
[Agent writes]

## Section 2 — Architecture walkthrough (~300 words)
[Agent writes, B-roll cues in brackets]
[B-ROLL: console screenshot filename]

## Section 3 — The build (~400 words)
[Agent writes, code callouts]

## Section 4 — Results + cost (~150 words)
[Agent writes]

<!-- MIKE: Outro — to camera, ~30 sec. Thank community,
mention AWS User Group Oceania, ask a question for comments. -->
```

## Rules
- B-roll cues reference actual screenshot filenames from ./screenshots/
- Save as: ./YYYY-MM-DD-[slug]/script.md
- Also save: ./YYYY-MM-DD-[slug]/description.txt (YouTube description only)
