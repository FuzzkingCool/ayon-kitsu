# Testing

This directory is **unit-only**: tests mock Kitsu / AYON and do **not** require a running AYON server or mounted Kitsu addon. Contract tests against a live stack were removed; add them elsewhere (e.g. studio CI) if you need that coverage again.

See **[TEST_MAP.md](TEST_MAP.md)** for each module and the production code it exercises.

## Setup

Use the **Poetry** environment under `ayon-kitsu/tests` (declared in `pyproject.toml` there). The codebase targets **Python 3.10+** (e.g. `str | None` in server helpers); older interpreters will fail collection.

```shell
cd ayon-kitsu/tests

poetry install --no-root
```

## Running tests

```shell
cd ayon-kitsu/tests

poetry run pytest

# Single file
poetry run pytest tests/tests/test_processor_task_relink.py
```

Processor-only tests also live under [`../services/processor/tests/`](../services/processor/tests/) (e.g. `test_processor_image.py`); run from that package or widen `pytest` paths if you include them in CI.
