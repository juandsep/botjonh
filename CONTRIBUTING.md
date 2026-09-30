# Contributing

## Branch flow

```
feat/*  chore/*  fix/*  ──►  dev  ──►  main
```

- `main` holds released, deployable code. Only `dev` merges into it.
- `dev` is the integration branch. Nothing lands here except a merge from a
  topic branch.
- Work happens on short-lived branches cut from `dev`, one per change:

```bash
git checkout dev && git pull
git checkout -b feat/short-description
# ... work, commit ...
git push -u origin feat/short-description
```

Open a pull request into `dev`. Merge it, then delete the branch. Prefixes:
`feat/` for behavior, `fix/` for defects, `chore/` for tooling and docs.

Releasing means a pull request from `dev` into `main`, which the protected
`production` environment gates behind a required reviewer.

## Commits

Conventional Commits, imperative mood, body explaining what changed and why:

```
fix: reject a non-allowlisted chat before spending tokens
```

## Local checks

```bash
uv sync
uv run pre-commit install          # once per clone
uv run pre-commit run --all-files
uv run pytest
uv run pytest --cov --cov-fail-under=80
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run --with pip-audit pip-audit
```

`detect-secrets` blocks credentials from being committed. If it flags a false
positive, add the finding to `.secrets.baseline`:

```bash
uvx detect-secrets scan --baseline .secrets.baseline
uvx detect-secrets audit .secrets.baseline
```

CI runs the same `detect-secrets` hook plus gitleaks over the pushed commits.
CI also fails under 80% test coverage, writes the coverage table to the job
summary and, when the `SONAR_TOKEN` secret is set, sends the analysis and
`coverage.xml` to SonarQube Cloud (`sonar-project.properties`).
Dependabot opens weekly update PRs into `dev` for uv, GitHub Actions and Docker.

If a real secret ever reaches a commit, treat it as compromised: rotate it
first, then rewrite history. Deleting the commit is not enough.

## Secrets and configuration

Credentials live in Secret Manager and are injected as environment variables,
never in the repository. Settings are read from environment variables; `.env`
is git-ignored for local development. CI and deploy use repository variables
and secrets, and `GITHUB_TOKEN` stays scoped to the minimum each workflow
declares. Never log PII: no `chat_id`, amounts or event titles in Cloud
Logging; message text is stored hashed in MLflow.

## Prompts and tools

The system prompt and tool schemas are versioned artifacts under
`src/assistant/llm/`. The prompt version is the sha256 (12 hex chars) of
`system.md` plus the tools JSON, so any change bumps it automatically; it is
recorded in MLflow so latency and cost are comparable across versions. Tool
names are allowlisted in `tools.py`; the LLM emits JSON validated against a
schema and the code performs the write — the model never executes anything.
