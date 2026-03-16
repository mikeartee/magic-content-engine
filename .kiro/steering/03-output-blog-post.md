---
title: "Output: blog post"
inclusion: manual
version: "1.0.0"
---

# Blog post format

Target: dev.to or personal site. Technical audience, builder tone.

## Structure

```markdown
<!-- MIKE: Write your hook here. ~100–150 words. Open with the
problem or a moment, not "I". Bridge to the architecture section.
Suggested angles this week: [AGENT: 2–3 specific suggestions] -->

---

## What we're building
[Agent writes: overview, references architecture diagram]

![Architecture](./screenshots/architecture.png)

## The build
[Agent writes: code walkthrough, inline APA citations, 
one console screenshot per major step]

## Cost breakdown
[Agent writes: per-service table]

## What it produced
[Agent writes: sample output commentary + screenshot]

<!-- MIKE: Oceania angle — ~60 words. Why this matters for
NZ/Pacific builders specifically. Optional but recommended. -->

<!-- MIKE: Closing — ~50 words. CTA, GitHub link, UG invite. -->

## References
[Agent writes: APA 7th, sorted A–Z]
```

## Rules
- Inline citations format: (Amazon Web Services, 2025)
- APA web entry: Author (Year, Mon Day). *Title*. Site. URL
- Fallback author: "Amazon Web Services"
- Images as relative paths: ./screenshots/filename.png
- Code blocks use triple backticks with language hint
- Save as: ./YYYY-MM-DD-[slug]/post.md
