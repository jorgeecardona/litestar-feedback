# litestar-feedback -- task verbs. Check verbs are thin aliases to moon tasks (moon.yml holds the
# command once). Only verbs with no moon task run a command here: format, bump, bump-check,
# install-hooks.
# One-time setup after cloning:  make install-hooks
.DEFAULT_GOAL := help
.PHONY: help install-hooks format lint typecheck test check bump bump-check build

help: ## list targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install-hooks: ## point git at the tracked .githooks/ (pre-commit + pre-push)
	git config core.hooksPath .githooks
	chmod +x .githooks/pre-commit .githooks/pre-push scripts/bump-version scripts/check-version-bump
	@echo "hooks installed: core.hooksPath -> .githooks"

format: ## auto-format + autofix lint (ruff)
	uv run ruff format src tests examples
	uv run ruff check --fix src tests examples

lint: ## lint + format check (ruff) -- the moon :lint and :format-check tasks
	moon run :lint :format-check

typecheck: ## type-check (pyright strict) -- the moon :typecheck task
	moon run :typecheck

test: ## run the test suite -- the moon :test task
	moon run :test

# Appends "<epoch> <seconds> <pass|fail>" to .git/check-times.log (local, untracked).
check: ## lint + typecheck + test, timed
	@start=$$(date +%s); \
	if $(MAKE) --no-print-directory lint typecheck test; then r=pass; else r=fail; fi; \
	end=$$(date +%s); \
	echo "$$end $$((end - start)) $$r" >> "$$(git rev-parse --git-dir)/check-times.log"; \
	echo "check: $$r in $$((end - start))s"; \
	[ "$$r" = pass ]

bump: ## bump __version__ -- TYPE=patch|minor|major (default patch)
	./scripts/bump-version $(or $(TYPE),patch)

bump-check: ## fail when src/ changed since the upstream branch without a version bump
	@base=$$(git rev-parse -q --verify '@{upstream}' 2>/dev/null) \
		&& sh scripts/check-version-bump "$$base" \
		|| echo "bump-check: no upstream branch; skipping"

build: ## build sdist + wheel into dist/ -- the moon :build task
	moon run :build
