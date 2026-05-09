# Project board

GitHub Projects v2 board for `mikeartee/magic-content-engine`.

## Board details

| Field | Value |
|---|---|
| Title | magic-content-engine |
| URL | https://github.com/users/mikeartee/projects/4 |
| Project node ID | `PVT_kwHOBIHFbs4BXLES` |
| Owner | `mikeartee` (user) |
| Number | 4 |

## Status field

| Field | Value |
|---|---|
| Field name | Status |
| Field ID | `PVTSSF_lAHOBIHFbs4BXLESzhSZ0c8` |

## Status options

| Option name | Option ID |
|---|---|
| Backlog | `93f29986` |
| Ready | `cf97eb3e` |
| In progress | `b3a26253` |
| In review | `7586ca13` |
| Done | `11c18117` |

## Skill → Status mapping

| Skill action | Status |
|---|---|
| `/triage` → `needs-triage` or `needs-info` | Backlog |
| `/triage` → `ready-for-agent` or `ready-for-human` | Ready |
| `/triage` → `tracking` or `/to-issues` parent | In progress |
| `/tdd` step 1 (work begins) | In progress |
| `/tdd` ship — PR opened | In review |
| `/triage` → `wontfix` | Done |

Issue closure (PR merged or manual close) moves the card to `Done` automatically via the board's built-in **Auto-close issue** workflow. Skills do not write `Done` themselves.

## Recommended board workflows to enable

Visit https://github.com/users/mikeartee/projects/4/workflows and enable:

1. **Auto-add to project** — filter: `repo:mikeartee/magic-content-engine is:issue,pr is:open`
2. **Auto-close issue** — so closing an issue lands its card on Done without skills having to

These can't be toggled via `gh` CLI yet — enable them manually in the browser.

## GraphQL helpers

Move a card to a specific Status option:

```bash
# First get the item ID for an issue
gh api graphql -f query='
  query($projectId: ID!, $issueNumber: Int!) {
    user(login: "mikeartee") {
      projectV2(number: 4) {
        items(first: 100) {
          nodes {
            id
            content { ... on Issue { number } }
          }
        }
      }
    }
  }
' -F issueNumber=<number>

# Then update the Status field
gh api graphql -f query='
  mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
    updateProjectV2ItemFieldValue(input: {
      projectId: $projectId
      itemId: $itemId
      fieldId: $fieldId
      value: { singleSelectOptionId: $optionId }
    }) { projectV2Item { id } }
  }
' \
  -f projectId="PVT_kwHOBIHFbs4BXLES" \
  -f itemId="<PVTI_...>" \
  -f fieldId="PVTSSF_lAHOBIHFbs4BXLESzhSZ0c8" \
  -f optionId="<option-id-from-table-above>"
```
