# Makefile to help automate key steps

.DEFAULT_GOAL := help
# Will likely fail on Windows, but Makefiles are in general not Windows
# compatible so we're not too worried
TEMP_FILE := $(shell mktemp)

# A helper script to get short descriptions of each target in the Makefile
define PRINT_HELP_PYSCRIPT
import re, sys

for line in sys.stdin:
	match = re.match(r'^([\$$\(\)a-zA-Z_-]+):.*?## (.*)$$', line)
	if match:
		target, help = match.groups()
		print("%-30s %s" % (target, help))
endef
export PRINT_HELP_PYSCRIPT


.PHONY: help
help:  ## print short description of each target
	@python3 -c "$$PRINT_HELP_PYSCRIPT" < $(MAKEFILE_LIST)

.PHONY: checks
checks:  ## run all the linting checks of the codebase
	@echo "=== pre-commit ==="; uv run --group dev pre-commit run --all-files || echo "--- pre-commit failed ---" >&2; \
		echo "=== mypy ==="; MYPYPATH=stubs uv run --group dev mypy src || echo "--- mypy failed ---" >&2; \
		echo "=== ty ==="; uv run --group dev ty check src || echo "--- ty failed ---" >&2; \
		echo "======"

.PHONY: ruff-fixes
ruff-fixes:  ## fix the code using ruff
    # format before and after checking so that the formatted stuff is checked and
    # the fixed stuff is formatted
	uv run --group dev ruff format src tests scripts docs
	uv run --group dev ruff check src tests scripts docs --fix
	uv run --group dev ruff format src tests scripts docs

.PHONY: test
test:  ## run the tests
	uv run --group tests pytest src tests -r a -v --doctest-modules --doctest-report ndiff --cov=esmporium

# Note on code coverage and testing:
# You must specify cov=src.
# Otherwise, funny things happen when doctests are involved.
# If you want to debug what is going on with coverage,
# we have found that adding COVERAGE_DEBUG=trace
# to the front of the below command
# can be very helpful as it shows you
# if coverage is tracking the coverage
# of all of the expected files or not.
# We are sure that the coverage maintainers would appreciate a PR
# that improves the coverage handling when there are doctests
# and a `src` layout like ours.

# The scratch database that migrations are authored against.
# It is deleted before and after every autogenerate,
# so autogenerate always compares the models against
# "every existing migration applied from scratch".
ALEMBIC_SCRATCH_DB := alembic-scratch.db

.PHONY: migration
migration:  ## generate a migration for the current models, e.g. make migration MESSAGE="add version table"
	@if [ -z "$(MESSAGE)" ]; then echo 'Usage: make migration MESSAGE="what you changed"' >&2; exit 1; fi
	rm -f $(ALEMBIC_SCRATCH_DB)
	uv run alembic upgrade head
	uv run alembic revision --autogenerate -m "$(MESSAGE)"
	rm -f $(ALEMBIC_SCRATCH_DB)
    # Alembic's output isn't formatted to our line length, so fix that here
    # rather than leaving it to fail in CI.
	uv run --group dev ruff format src/esmporium/db/migrations
	@echo "Now read the generated file in src/esmporium/db/migrations/versions/."
	@echo "Autogenerate is a first draft, not an answer:"
	@echo "it cannot see data migrations and it guesses at renames."

# A helper script to print the schema of a SQLite database
define PRINT_SQLITE_SCHEMA_PYSCRIPT
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
statements = connection.execute(
    "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type DESC, name"
)

print("\n\n".join(f"{statement};" for (statement,) in statements))
endef
export PRINT_SQLITE_SCHEMA_PYSCRIPT

.PHONY: migration-sql
migration-sql:  ## print the schema that applying every migration from scratch gives
    # This applies the migrations to a throwaway database
    # rather than using alembic's offline mode (`alembic upgrade base:head --sql`).
    # Offline mode cannot run our migrations:
    # SQLite needs batch mode (see `env.py`),
    # and batch mode has to reflect the existing table,
    # which it can only do against a real database.
	rm -f $(ALEMBIC_SCRATCH_DB)
	uv run alembic upgrade head
	@uv run python -c "$$PRINT_SQLITE_SCHEMA_PYSCRIPT" $(ALEMBIC_SCRATCH_DB)
	rm -f $(ALEMBIC_SCRATCH_DB)

.PHONY: docs
docs:  ## build the docs
	uv run --group docs properdocs build

.PHONY: docs-strict
docs-strict:  ## build the docs strictly (e.g. raise an error on warnings, this most closely mirrors what we do in the CI)
	uv run --group docs properdocs build --strict

.PHONY: docs-serve
docs-serve:  ## serve the docs locally
	uv run --group docs properdocs serve

.PHONY: changelog-draft
changelog-draft:  ## compile a draft of the next changelog
	uv run --group dev towncrier build --draft --version draft

.PHONY: licence-check
licence-check:  ## Check that licences of the dependencies are suitable
	# Will likely fail on Windows, but Makefiles are in general not Windows
	# compatible so we're not too worried
	uv export --no-dev > $(TEMP_FILE)
	uv run --group dev liccheck -r $(TEMP_FILE) -R licence-check.txt
	rm -f $(TEMP_FILE)

.PHONY: virtual-environment
virtual-environment:  ## update virtual environment, create a new one if it doesn't already exist
	uv sync --all-extras --group all-dev
	uv run --group dev pre-commit install
