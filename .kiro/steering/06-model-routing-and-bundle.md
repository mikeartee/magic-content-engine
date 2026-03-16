---
title: "Model routing and output bundle"
inclusion: manual
version: "1.0.0"
---

# Model routing

| Task                     | Model   |
|--------------------------|---------|
| Relevance scoring        | Haiku   |
| APA citation formatting  | Haiku   |
| Metadata extraction      | Haiku   |
| Digest email             | Haiku   |
| Blog post writing        | Sonnet  |
| YouTube script           | Sonnet  |
| CFP abstract             | Sonnet  |
| UG session outline       | Sonnet  |

Use Haiku for anything structured or template-following.
Use Sonnet only where voice and narrative judgment matter.

---

# Output bundle structure

```
YYYY-MM-DD-[slug]/
  post.md                  if selected
  script.md                if selected
  description.txt          if selected (YouTube only)
  cfp-proposal.md          if selected
  usergroup-session.md     if selected
  references.bib           always — BibTeX of all APA refs
  cost-estimate.txt        always — this run's breakdown
  screenshots/
    research/              source landing pages
    console-runtime.png
    console-gateway.png
    console-memory.png
    console-observability.png
    sample-output.png
  agent-log.json           articles found, scores, model per step
```

## Output choice prompt
Before generating content, ask Mike:

  Which outputs do you want this run?
  [1] Blog post
  [2] YouTube script
  [3] CFP proposal
  [4] User group session outline
  [5] Weekly digest email
  [6] All of the above

If unattended/automated, default to [1] + [2].

## Screenshot rules
- Browser viewport: 1440×900 before each capture
- Wait 3s minimum after navigation for React pages to render
- Filenames: kebab-case matching section they illustrate
- Save to ./screenshots/ relative to output folder

## Terminal summary on completion
Print:
- Articles found / kept after scoring
- Outputs generated
- Estimated run cost (AgentCore + LLM tokens)
- Screenshots that failed to capture (if any)
