# Ship style

Changes reach the default branch via **pull request**.

## Workflow

1. Create a feature branch from the default branch:
   ```bash
   git checkout -b <branch-name>
   ```
   Branch naming convention: `<type>/<issue-number>-<short-description>`
   Examples: `feat/12-wire-lambda-handler`, `fix/7-slug-trailing-hyphen`

2. Make changes, commit:
   ```bash
   git add <files>
   git commit -m "<message>"
   ```

3. Push and open a PR with a closing keyword:
   ```bash
   git push -u origin <branch-name>
   gh pr create \
     --repo mikeartee/magic-content-engine \
     --title "<title>" \
     --body "Closes #<issue-number>\n\n<description>"
   ```

4. Mike merges the PR. The issue closes automatically via the `Closes` keyword.

## Notes

- Always include `Closes #<number>` in the PR body so the issue closes on merge.
- Keep PR titles under 70 characters. Use the description for detail.
- Do not force-push to the default branch.
- Do not push directly to `main` — always go through a PR.
