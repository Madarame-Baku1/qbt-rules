# Contributing to qbt-rules

Thanks for your interest! Bug reports, rule ideas, docs fixes and code are all welcome.

- **Questions or help with a rule:** ask in [Discussions](https://github.com/andronics/qbt-rules/discussions).
- **Bugs and feature requests:** open an [issue](https://github.com/andronics/qbt-rules/issues/new/choose). Describing the problem you're trying to solve helps more than a proposed solution.
- **Something small to start on:** look for issues labelled [`good first issue`](https://github.com/andronics/qbt-rules/labels/good%20first%20issue).

## Making a change

```bash
git clone https://github.com/<you>/qbt-rules.git
cd qbt-rules
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

1. Branch from `main` in your fork.
2. Keep the change focused, and add or update tests for it.
3. Use [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `test:`, `chore:`). The changelog is generated from them at release time, so please don't edit `CHANGELOG.md`.
4. Open a pull request against `main`. CI runs the tests and builds the Docker image.

The wiki's [Contributing](https://github.com/andronics/qbt-rules/wiki/Contributing), [Architecture Internals](https://github.com/andronics/qbt-rules/wiki/Architecture-Internals) and [Testing Guide](https://github.com/andronics/qbt-rules/wiki/Testing-Guide) pages go into more detail on the code layout and tests.
