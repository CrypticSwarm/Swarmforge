# Testing

## Unit Tests

The launcher, the tongs layer, and the container-side translators are covered by stdlib `unittest` tests in `tests/test_*.py`. A test module is named for the source module it covers — `tests/test_tongs_<module>.py` for `swarmforge/tongs/<module>.py`, `tests/test_anvil_<module>.py` for `swarmforge/anvil/<module>.py` — so the file that covers a change is the one named after it. Two modules have no namesake file because they have nothing to assert on their own: `swarmforge/anvil/readiness.py` is exercised through `run_with_tongs`, and `swarmforge/anvil/errors.py` holds one exception class. Fixtures that more than one test module needs live in `tests/tongs_fixtures.py`, `tests/anvil_fixtures.py` and `tests/harness_fixtures.py`, which the discovery glob skips.

Three files assert on the shape of the repo rather than on any one module. `tests/test_image_layout.py` holds the Dockerfile and the entrypoint to the same import root, `tests/test_package_layering.py` keeps the package's imports acyclic and keeps loading a module from a file path out of everything but the `bin/` shims, and `tests/test_harness_conformance.py` holds every registered harness to the contract described under [Harness lifecycle](architecture.md). All three fail the way a build should — before anything reaches a container.

- Run them: `make test`

The target is `python3 -m unittest discover -s tests -p 'test_*.py'` with the repo root on `PYTHONPATH`, and CI runs the same discovery. Nothing names test modules by hand, so a new `tests/test_*.py` file runs the moment it lands. It needs only a host python — no Docker, no network, no model.

## Lint

- Run it: `make lint`

`ruff check` over every Python file in the repo, configured in `pyproject.toml` — including the extensionless commands in `bin/`, which ruff would otherwise skip. The rule set is ruff's default — the pycodestyle checks that catch mistakes plus all of pyflakes — and stops there on purpose: line length, import order, and whitespace are left to the author, so turning the linter on does not reflow files a change never touched. Only `ruff check` is ever run; `ruff format` is not part of this repo. Install ruff with `pipx install ruff` (CI pins the version), or point the target at another copy with `make lint RUFF=<path>`.

Ruff is a contributor tool, not a dependency: the harness image installs no third-party Python, and every module under `swarmforge/` stays stdlib-only.

## Skill Tests

A lightweight skill test harness runs scenario prompts against a chosen model and verifies expected behavior. It drives a real model inside the OpenCode image, which is why it is a separate target from the unit suite.

- Run all skill tests: `make test-skills MODEL=<provider/model>`
- Run a single skill's tests: `make test-skills MODEL=<provider/model> TEST_SKILL=<skill-name>`
- Optional judge mode: `make test-skills MODEL=<student> TEST_ENABLE_JUDGE=1 EVAL_MODEL=<judge>`
- Timeout override: `make test-skills MODEL=<provider/model> TEST_TIMEOUT_S=<seconds>`

Tests live in `skills/<skill-name>/tests/*.json`; the runner is `scripts/skill_eval.py`.
Assertions can be:

- Output patterns: `expect.must_match` and `expect.must_not_match` (regex against formatted output)
- Tool calls: `expect.must_tool` and `expect.must_not_tool` (extracted from `opencode run --format json` events)
