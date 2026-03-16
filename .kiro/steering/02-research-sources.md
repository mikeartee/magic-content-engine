---
title: "Research sources and scoring"
inclusion: manual
version: "1.0.0"
---

# Research sources

## Primary (crawl every run)
- kiro.dev/changelog/ide/ — Kiro IDE releases
- github.com/kirodotdev/Kiro/issues — community feature requests
- aws.amazon.com/new/ — filter: bedrock, agentcore, kiro, lambda
- aws.amazon.com/blogs/machine-learning/
- community.aws/ — Community Builder posts

## Secondary (crawl weekly)
- github.com/awslabs/ — new AgentCore and Strands releases
- strandsagents.com — Strands SDK updates
- repost.aws/ — trending AI/ML questions
- kiro.dev/blog/ — Kiro blog posts

## Ignore entirely
- General AWS infrastructure (EC2, RDS, networking) unless AI-adjacent
- AWS pricing changes unless they affect AgentCore/Bedrock
- Non-AWS AI news unless it affects MCP or agentic patterns

## Relevance scoring (Haiku)
Score 1–5. Drop anything below 3.

High (4–5):
- Kiro IDE new features or breaking changes
- AgentCore, Strands, Bedrock announcements
- MCP spec updates
- Steering docs / Kiro extension ecosystem
- Community Builder programme news
- Anything that affects kiro-steering-docs-extension

Medium (3):
- AWS Lambda, S3, IAM changes affecting agent deployments
- General agentic AI patterns with AWS application
- NZ/Oceania AWS events or community news

Low (1–2):
- Generic AI news without AWS angle
- AWS services with no agent relevance
