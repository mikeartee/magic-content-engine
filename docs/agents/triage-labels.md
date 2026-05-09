# Triage labels

Label strings used by the `triage`, `to-issues`, and `tdd` skills for this repo.
All six roles use the canonical defaults — no overrides.

## Label map

| Canonical role | Label string | Meaning |
|---|---|---|
| needs-triage | `needs-triage` | Maintainer needs to evaluate this issue |
| needs-info | `needs-info` | Waiting on reporter for more information |
| ready-for-agent | `ready-for-agent` | Fully specified; an AFK agent can pick this up with no human context |
| ready-for-human | `ready-for-human` | Needs human implementation |
| tracking | `tracking` | Container/parent issue — work lives in sub-issues |
| wontfix | `wontfix` | Will not be actioned |

## Creating labels in GitHub

If these labels don't exist yet in the repo, create them:

```bash
gh label create needs-triage   --repo mikeartee/magic-content-engine --color "e4e669" --description "Maintainer needs to evaluate"
gh label create needs-info     --repo mikeartee/magic-content-engine --color "d93f0b" --description "Waiting on reporter"
gh label create ready-for-agent --repo mikeartee/magic-content-engine --color "0075ca" --description "AFK-ready — fully specified"
gh label create ready-for-human --repo mikeartee/magic-content-engine --color "7057ff" --description "Needs human implementation"
gh label create tracking       --repo mikeartee/magic-content-engine --color "e99695" --description "Container/parent issue"
gh label create wontfix        --repo mikeartee/magic-content-engine --color "ffffff" --description "Will not be actioned"
```
