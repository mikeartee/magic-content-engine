# Issue tracker

Issues for this repo live in **GitHub Issues** on `mikeartee/magic-content-engine`.

## CLI

Skills use the [GitHub CLI](https://cli.github.com/) (`gh`). Ensure `gh auth status` passes before running any skill that touches issues.

## Creating issues

```bash
gh issue create \
  --repo mikeartee/magic-content-engine \
  --title "<title>" \
  --body "<body>" \
  --label "<label>"
```

## Reading issues

```bash
# List open issues
gh issue list --repo mikeartee/magic-content-engine --state open --json number,title,labels,body

# Read a specific issue
gh issue view <number> --repo mikeartee/magic-content-engine --json number,title,labels,body,comments
```

## Closing issues

```bash
gh issue close <number> --repo mikeartee/magic-content-engine --comment "<reason>"
```

## Applying labels

```bash
gh issue edit <number> --repo mikeartee/magic-content-engine --add-label "<label>"
gh issue edit <number> --repo mikeartee/magic-content-engine --remove-label "<label>"
```

## Notes

- The repo is user-owned (`mikeartee`), not org-owned.
- Always pass `--repo mikeartee/magic-content-engine` explicitly — do not rely on the current directory's remote.
