# Domain docs

## Layout: single-context

This repo uses a single-context layout:

- **`CONTEXT.md`** — at the repo root. Domain language, key concepts, and architectural vocabulary for the magic-content-engine. Does not exist yet; create it when you're ready to document the domain.
- **`docs/adr/`** — at the repo root. Architectural Decision Records. Does not exist yet; create it when you make your first significant architectural decision (e.g. AgentCore vs standard AWS, Lambda vs EC2).

## Consumer rules for skills

When a skill reads domain context, it should:

1. Read `CONTEXT.md` at the repo root first.
2. Read all `docs/adr/*.md` files in filename order (ADRs are numbered, e.g. `0001-use-lambda.md`).
3. Treat `CONTEXT.md` as the authoritative domain vocabulary. If a term in the code or an issue conflicts with `CONTEXT.md`, flag the discrepancy rather than silently resolving it.
4. Treat ADRs as immutable history. Do not rewrite or contradict a past ADR — instead, create a new ADR that supersedes it.

## ADR format

Use the [MADR](https://adr.github.io/madr/) lightweight format:

```markdown
# <number>. <title>

Date: YYYY-MM-DD
Status: Accepted | Superseded by [<number>](<link>)

## Context
<what situation prompted this decision>

## Decision
<what was decided>

## Consequences
<what changes as a result>
```

## Suggested first entries

When you're ready, consider documenting:

- `0001-runtime-choice.md` — AgentCore Runtime vs Lambda (the current open decision in `CLAUDE.md`)
- `0002-browser-strategy.md` — Playwright vs AgentCore Browser vs dropping JS-rendered sources
- `0003-memory-backend.md` — AgentCore Memory vs DynamoDB
