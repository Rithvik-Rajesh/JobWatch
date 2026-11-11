# JobWatch

Personal job discovery and application management platform: monitors company career pages
(Workday first), keeps every opportunity in PostgreSQL, and tracks applications from discovery to offer.

- Architecture & research: [docs/phase1](docs/phase1)
- Phase 2 decisions, conventions and ownership: [docs/phase2/decisions.md](docs/phase2/decisions.md)
- API contract snapshot: [docs/api/openapi.json](docs/api/openapi.json)

## Local development

Requirements: Docker, [uv](https://docs.astral.sh/uv/), Python 3.12.

```bash
make db-up      # Postgres 17 on 127.0.0.1:5433
make migrate
make test       # integration tests against real Postgres
make api        # http://localhost:8077/docs
```

Copy `backend/.env.example` to `backend/.env` and set an admin password hash
(`cd backend && uv run python -m app.core.auth hash-password`).
