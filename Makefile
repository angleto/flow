.PHONY: sync lint fmt type test web-check eval eval-humus eval-bench mcp-coverage mcp-coverage-check up down \
        db-bootstrap migrate db-harden revision run-api run-mcp run-worker run-sdi

sync:
	uv sync --all-packages

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

type:
	uv run mypy -p mycelium_core -p mycelium_api -p mycelium_mcp -p mycelium_worker -p mycelium_sdi_inbound

test:
	uv run pytest

# The SPA gate, matching what CI's `web` job runs. Worth a target of its
# own: `tsc --noEmit` from the repo root silently checks NOTHING (the root
# tsconfig.json is a references stub with "files": []), so the obvious
# invocation reports success on a file it never opened. `tsc -b` is the
# one that type-checks.
web-check:
	cd web && pnpm install --frozen-lockfile \
	  && pnpm exec eslint . \
	  && pnpm check:shared \
	  && pnpm check:i18n \
	  && pnpm check:css \
	  && pnpm typecheck \
	  && pnpm test

# The browser extension gate, matching CI's `extension` job. Its own
# package with its own lockfile, so `pnpm install` here does not touch
# the SPA's tree.
extension-check:
	cd extension && pnpm install --frozen-lockfile \
	  && pnpm exec eslint . \
	  && pnpm check:messages \
	  && pnpm check:grammar \
	  && pnpm check:tokens \
	  && pnpm typecheck \
	  && pnpm test

# A loadable unpacked directory at extension/dist/unpacked. There is no
# default origin: it decides which deployment the package talks to, which
# origin it may fetch from and which origin may hand it a credential, so
# a missing value stops the build rather than picking somewhere.
#   MYCELIUM_EXTENSION_ORIGIN=https://mycelium.xeno.garden make extension-build
extension-build:
	cd extension && pnpm build

# The archive: the upload artifact, and what a deployment serves for
# download. Refuses a non-https origin, because the store would accept a
# localhost build and every installer would get an extension talking to
# their own machine.
#
# The script is `zip` and not `pack` because `pnpm pack` is a BUILT-IN
# that shadows a script of that name: this target used to produce an npm
# tarball of the package sources, silently, and never the archive it
# names.
extension-zip:
	cd extension && pnpm zip

# Offline retrieval eval gate (ADR-0035 / Mycelio WS-E1): deterministic
# gold-set recall@k/MRR + dense-tier health over the real pipeline. Runs
# in CI as part of `test`; this target runs just the gate for a quick
# local baseline check.
eval:
	uv run pytest core/tests/test_eval_offline.py -q

# Humus retrieval A/B over a REAL corpus (task 4836a6cc / note 9a2adb4a §4):
# same run_humus_ab matrix as the CI test, from gold JSONL files. Provide the
# files (and optionally org/actor, else an owner is auto-resolved):
#   make eval-humus RAW=raw.jsonl CON=consolidation.jsonl [ORG=<uuid> ACTOR=<uuid>]
eval-humus:
	uv run python scripts/eval_humus_ab.py --raw "$(RAW)" --consolidation "$(CON)" \
		$(if $(ORG),--org $(ORG),) $(if $(ACTOR),--actor $(ACTOR),)

# Public memory benchmarks (LongMemEval / LOCOMO, task cc4653bd): ingest an
# operator-provided dataset file into throwaway orgs and score retrieval on
# the same run_eval path as CI. NEVER against prod (creates orgs/blobs):
#   make eval-bench DATASET=longmemeval FILE=~/.../longmemeval_oracle.json [K=10 LIMIT=20 QLIMIT=50]
eval-bench:
	uv run python scripts/eval_public_bench.py --dataset "$(DATASET)" --path "$(FILE)" \
		$(if $(K),--k $(K),) $(if $(LIMIT),--limit-instances $(LIMIT),) \
		$(if $(QLIMIT),--limit-questions $(QLIMIT),)

# Regenerate the auto-generated tool inventory in docs/mcp-coverage.md from
# the live registry (counts + per-domain listing never drift from code).
mcp-coverage:
	uv run python scripts/gen_mcp_coverage.py

# CI/pre-commit gate: fail if docs/mcp-coverage.md is stale. DB-free and
# embedder-free (importing the server only registers tool callables).
mcp-coverage-check:
	uv run python scripts/gen_mcp_coverage.py --check

up:
	docker compose -f deploy/local/docker-compose.yml up -d

down:
	docker compose -f deploy/local/docker-compose.yml down

# Create/ensure the runtime role mycelium_app and set its password from
# MYCELIUM_DB_APP_PASSWORD (env). Run after `up`, before `migrate`.
db-bootstrap:
	docker compose -f deploy/local/docker-compose.yml exec -T \
	  -e PGPASSWORD=$${POSTGRES_PASSWORD:-mycelium} db \
	  psql -v ON_ERROR_STOP=1 -U $${POSTGRES_USER:-mycelium} -d $${POSTGRES_DB:-mycelium} \
	  -v app_pw="$${MYCELIUM_DB_APP_PASSWORD:?set MYCELIUM_DB_APP_PASSWORD}" \
	  -f - < deploy/local/bootstrap_roles.sql

migrate:
	uv run alembic -c core/alembic.ini upgrade head

# Reproduce the production function-execute posture: revoke the default
# PUBLIC execute on our functions so mycelium_app keeps only its explicit
# grants (see the SQL header and docs/adr/0015). Run after `migrate`. The
# pytest suite applies this automatically (root conftest); this target is
# for the local docker stack so `make run-api` matches prod too.
db-harden:
	docker compose -f deploy/local/docker-compose.yml exec -T \
	  -e PGPASSWORD=$${POSTGRES_PASSWORD:-mycelium} db \
	  psql -v ON_ERROR_STOP=1 -U $${POSTGRES_USER:-mycelium} -d $${POSTGRES_DB:-mycelium} \
	  -f - < deploy/local/harden_function_acls.sql

revision:
	uv run alembic -c core/alembic.ini revision --autogenerate -m "$(m)"

run-api:
	uv run uvicorn mycelium_api.main:app --reload

run-mcp:
	uv run python -m mycelium_mcp.main

run-worker:
	uv run python -m mycelium_worker.main

run-sdi:
	uv run uvicorn mycelium_sdi_inbound.main:app --reload --port 8081

# CLI dev convenience: ``make cli ARGS="task list --json"``.
cli:
	uv run mycelium $(ARGS)

# Run the CLI smoke tests only (offline, no backend needed).
test-cli:
	uv run pytest cli/tests -x
