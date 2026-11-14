# deadlift

## Agent skills

### Issue tracker

Issues live in GitHub Issues on `hangindev/deadlift`, managed through the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Uses the five default triage labels: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single context: one `GLOSSARY.md` and `docs/adr/` at the repo root, created only when first needed. See `docs/agents/domain.md`.

## Tests

`uv sync` once, then `uv run pytest` for everything but two Docker-backed suites, which run only when named:

- `uv run pytest tests/delivery`: needs Docker, `flock` and the system `python3` with PyYAML (`python3-yaml`), as on a Server.
- `tests/server_setup`: bootstraps the machine it runs on, so it refuses anything but a throwaway one. CI runs it; locally, `tests/server_setup/multipass.sh` runs it in a VM.

The actions tests need `jq`: the fake `gh` applies `--jq` with the real jq, as gh does. Lint and types: `uv run ruff check`, `uv run ruff format --check`, `uv run pyright`, `tests/actionlint.sh`.
