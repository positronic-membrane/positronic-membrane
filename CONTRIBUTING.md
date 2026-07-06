# Contributing to Positronic Membrane

This project is solo-maintained. External contributors are welcome, but please open an issue and discuss your approach before writing code — this keeps effort aligned with the project roadmap and avoids wasted work.

---

## Reporting Bugs

Check the [Issues tab](../../issues) first. If the bug isn't there, open a new issue and include:
- A clear, descriptive title
- Steps to reproduce
- Expected vs. actual behavior
- Relevant logs, error output, or environment details

---

## Suggesting Features

Open an issue describing what you want and why. The maintainer will confirm whether it fits the roadmap before you start coding.

---

## Submitting Code

### Branch naming

Follow the project convention — never commit directly to `main`:

```bash
git checkout main && git pull origin main
git checkout -b <branch-name>
```

Branch name format:
- `v3/t1-skills-library` — work scoped to a versioned milestone
- `backlog/t5-experience-log` — work picked up from the unslotted backlog

### Adding a new skill

New skills belong in [janus-skills-library](https://github.com/jmccauley75gh/janus-skills-library), not in this repo. See that repo's README for the skill file format, `registry.json` schema, and test harness pattern. Positronic Membrane pulls skills from the library at boot via `sync_from_registry()`.

Only open a PR here if you are adding a new Safe\* SDK wrapper in `src/skills.py` that a skill needs — the skill implementation itself still goes in janus-skills-library.

### Code review

Run `/code-review high` on your branch before opening a PR and fix all findings. This is the primary quality gate:

```bash
/code-review high        # find issues
/code-review high --fix  # auto-apply findings
```

### Testing

Run the test suite before opening a PR:

```bash
JANUS_TEST_MODE=1 pytest
```

This runs the fast unit suite only — `tests/e2e/` is excluded by default (see
`addopts` in `pyproject.toml`). If your change touches chat/persona, the
daemon's reflection cycle, sandbox sessions, or auth/party isolation, also run
the slower end-to-end suite:

```bash
JANUS_TEST_MODE=1 pytest -m e2e
```

- New modules get a matching `tests/test_<module>.py`.
- Mock external calls (`urllib.request.urlopen` for GitHub, `get_effective_workspace_root` for filesystem-boundary tests) — never hit real network/services in tests.
- See `tests/test_github_integration.py` and `tests/test_document_memory.py` for the established mocking patterns.

### Pull request

- Squash-merge workflow — one atomic commit per issue lands on `main`
- Include `Closes #<issue-number>` in the PR body
- Every PR must reference an issue — create one first if it doesn't exist

---

## Questions?

Open a GitHub Discussion or an issue labeled `question`.
