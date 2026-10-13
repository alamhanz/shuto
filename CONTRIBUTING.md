# Contributing to shuto

Thanks for helping. Everyone is welcome, whether you fix a typo or add a feature. shuto is a
small project, so keep changes focused.

## Ways to contribute

- **Bug reports:** use the bug report issue form. Say your OS and Python version.
- **Restriction-list updates:** Shutterstock changes its rules. Open an issue (or a PR) with the
  subject, where it applies and a link to the official Shutterstock source.
- **Model catalog updates:** new or corrected entries in `src/shuto/data/models.yaml`, with
  tags verified against the Ollama library.
- **Docs:** clearer wording, missing steps, examples.
- **Translations of docs:** welcome, put them under `docs/` and link the original.
- **Code:** fixes and features. Look for issues labelled `good first issue`.

For anything bigger than a small fix (new feature, new provider, refactor), open an issue first
so we can agree on the approach before you spend time on it.

## Workflow

1. Fork the repository and clone your fork.
2. Branch from `main`: `feat/...`, `fix/...` or `docs/...`.
3. Make small, focused commits.
4. Run the tests and ruff (see Dev setup).
5. Open a pull request against `main` and fill in the template.
6. CI must pass on Linux, macOS and Windows.
7. The maintainer reviews. Expect some requested changes; that is normal.

## Who merges

Only the maintainer (@alamhanz) merges into `main`. Contributors never push to `main`. Every
change lands through a reviewed pull request, and the maintainer may squash it on merge.

## Dev setup

Python 3.12 or newer on Linux, macOS or Windows.

```
git clone https://github.com/<your-user>/shuto.git
cd shuto
python -m venv .venv
```

Activate it (`source .venv/bin/activate` on Linux/macOS, `.venv\Scripts\activate` on Windows):

```
pip install -e ".[dev,all]"
pytest -q
ruff check .
ruff format --check .
```

`pytest -n auto` runs the tests in parallel (much faster; CI does this). CI runs pytest on
Ubuntu, macOS and Windows with Python 3.12 and 3.13, and ruff once on Ubuntu 3.12. Tests must
not assume a GPU, a running Ollama, network access, POSIX paths or POSIX file permissions.
Use the fake LLM and fake FTP helpers already in `tests/`.

## Principles

- Local-first by default and usable offline. Cloud APIs are optional.
- Never automate the Shutterstock contributor portal; the final Submit stays manual.
- No secrets in config, tests or docs.
- Cross-platform: Linux, macOS and Windows.

## Guidelines

- Use `pathlib`, and `subprocess.run([...], timeout=...)` without `shell=True`. No OS-specific scripts.
- Core modules stay pure; only `pipeline.py` touches the store and the config.
- Keep `docs/shutterstock-restrictions.md` and `src/shuto/data/restrictions.yaml` in sync: change both in the same pull request and cite the Shutterstock source.
- Model sizes, speeds and prices in `src/shuto/data/models.yaml` are approximate. Verify tags and prices against the Ollama library and provider pricing pages before changing them.
- Do not add real personal photos to tests. Generate synthetic images (see `tests/helpers.py`) or use public-domain ones with a credit.

## Commit messages

Short imperative subject, ideally under 72 characters: `Fix FTPS timeout on retry`. Add a body
if the why is not obvious. If you do not want your address in the history, use your GitHub
noreply email (`<id>+<user>@users.noreply.github.com`).

## Pull requests

Describe what changed and why, and add or update tests. Run the commands above first.

By contributing you agree that your work is released under the project's MIT license. Please
follow the [Code of Conduct](CODE_OF_CONDUCT.md). Security issues go through
[SECURITY.md](SECURITY.md), not public issues.
