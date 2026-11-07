.PHONY: setup infra api worker web test test-unit test-integration test-e2e build contracts verify
setup:
	go mod download
	uv sync --project engine --frozen
	npm ci
	npx playwright install chromium
infra:
	docker compose up -d postgres temporal jaeger
api:
	go run ./apps/api/cmd/server
worker:
	uv run --project engine --frozen python -m agentproof.worker
web:
	npm run dev
contracts:
	uv run --project engine python scripts/export_contracts.py
	uv run --project engine python scripts/export_benchmark_contracts.py
	uv run --project engine python scripts/export_interaction_contracts.py
build:
	go build ./apps/api/cmd/server
	npm run build
	rm -f server
test-unit:
	go test ./...
	uv run --project engine --frozen pytest -q
test-integration:
	@test -n "$$TEST_DATABASE_URL" || (echo 'Set TEST_DATABASE_URL'; exit 1)
	@test -n "$$TEST_TEMPORAL_ADDRESS" || (echo 'Set TEST_TEMPORAL_ADDRESS'; exit 1)
	go test ./...
	uv run --project engine --frozen pytest -q
test-e2e:
	npm run test:e2e
test: test-unit test-e2e
verify: build test-integration test-e2e

.PHONY: dev-docker verify-docker
dev-docker:
	docker compose up -d --build --wait
verify-docker:
	docker compose up -d --build --wait
	docker compose --profile verification build verify
	docker compose --profile verification run --rm verify
