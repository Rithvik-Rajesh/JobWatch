.PHONY: db-up db-down migrate test lint api openapi

db-up:        ## start local Postgres (127.0.0.1:5433)
	docker compose -f docker-compose.dev.yml up -d --wait
db-down:
	docker compose -f docker-compose.dev.yml down
migrate:
	cd backend && uv run alembic upgrade head
test: db-up   ## integration tests against real Postgres (creates jobwatch_test)
	cd backend && uv run pytest -q
lint:
	cd backend && uv run ruff check app tests && uv run ruff format --check app tests
api: db-up migrate
	cd backend && uv run uvicorn app.main:app --reload --port 8077
openapi:      ## write the API contract snapshot
	cd backend && uv run python -c "import json; from app.main import app; print(json.dumps(app.openapi(), indent=2))" > ../docs/api/openapi.json
