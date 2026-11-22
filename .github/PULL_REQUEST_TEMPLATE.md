## What does this PR do?

<!-- A short description of the change and the problem it solves. -->

## Verification

- [ ] `ruff check src tests`
- [ ] `pytest -q`
- [ ] UI change verified in a browser (attach screenshot if applicable)
- [ ] No tokens, private keys or private media included
- [ ] README / config comments updated if user-visible behavior changed

## Safety checklist

- [ ] Cleanup behavior remains preview-first (dry-run by default in CLI)
- [ ] Destructive operations only move files into trash and re-verify content
- [ ] New similarity detection is not more aggressive by default
