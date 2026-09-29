<!-- Feature, fix or chore branch into `dev`. For `dev` into `main`, use
     ?template=release.md on the PR URL. -->

## Summary

<!-- What changed, in 1-3 sentences. -->

## Why

<!-- The problem or need behind the change. -->

## How to test

<!-- Commands or steps a reviewer can run. -->

## Docs

- [ ] README / CONTRIBUTING updated, or not needed

## Checklist

- [ ] Branch cut from `dev` and targets `dev`
- [ ] `uv run pre-commit run --all-files` passes
- [ ] `uv run pytest` and `uv run mypy src` pass
- [ ] No credentials, tokens or keys added
- [ ] No PII logged (chat_id, amounts, event titles)
- [ ] Prompt/tool changes bump the prompt version
