# JobWatch

A small personal job watcher. It stores every new listing, downloads its JD when available, decides
whether a fresher can apply, and adds it to the inbox. Existing listings are only marked as seen;
they are never sent through the fresher checker again.

## Code map

The useful path through the project is intentionally short:

```text
backend/app/
├── discovery/
│   ├── pipeline.py       # list -> new-only check -> detail -> classify -> save
│   ├── classifier.py     # Ollama/Gemini provider boundary
│   └── adapters/         # one scraper per careers platform
├── jobs/                 # inbox API and queries
├── models/               # database rows
├── worker/               # scheduled pipeline entry point
├── core/                 # config, database, auth, logging
└── main.py               # FastAPI assembly
```

The remaining feature folders are independent API features; they do not participate in scraping.

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

## Run the UI

Start Ollama in one terminal:

```bash
ollama serve
```

Then start JobWatch from the repository root in another terminal:

```bash
make run
```

Open [http://localhost:8077](http://localhost:8077). The `make run` command starts PostgreSQL,
applies migrations, and serves both the API and the built-in UI. Click **Fetch jobs** in the UI to
open the fetch review window.

On the dedicated **Settings** page (`/settings`), use **Sync registry** to add missing companies
and apply scraper configuration updates from `data/companies.json`. **Download JSON** shares the
bundled watchlist and scraper definitions. **Check all supported sites** samples the listing feed and,
where available, one job detail. Checks save their status and timestamp without importing jobs or
calling an LLM. Unsupported or restricted portals stay visible with their reason.

Anyone with a JobWatch checkout can import the shared file into their own migrated database:

```bash
cd backend
uv run python -m app.discovery.registry_import ../data/companies.json
uv run python -m app.discovery.registry_import ../data/companies.json --apply
```

The first command previews the sync. The second updates sites by company and site label, adds missing
entries, and keeps local jobs, application history, and scheduling settings. The JSON contains only
the bundled company and scraper definitions.

After editing the research CSV, regenerate the shareable watchlist from `backend/`:

```bash
uv run python -m app.discovery.registry_import ../data/companies.csv --json-for ../data/watchlist.txt > ../data/companies.json
```

The interactive review flow is intentionally manual:

1. Fetch all selected companies and inspect each expandable job description.
2. Edit/save the primary rejection and acceptance word lists and run preliminary rules.
3. Review Freshers, Not freshers, and Unmarked tabs.
4. Edit/save the model prompt, then send only Unmarked jobs to Ollama or Gemini.
5. Review Clear freshers, Not freshers, and Unclear LLM tabs before closing the window.

Rules and the model prompt are stored in the database setting `fresher_classifier`, so changes made
in the window are reused by later reviews and scheduled runs.

## Fresher classification

Local Ollama is the default when no Gemini key is configured:

```bash
ollama pull gemma3:4b
ollama serve
```

`JOBWATCH_LLM_PROVIDER=auto` selects Gemini when `JOBWATCH_GEMINI_API_KEY` is set, otherwise Ollama.
You can force `ollama`, `gemini`, or `disabled`. Provider failures produce an `unknown` verdict and
do not fail the scrape. The API returns `is_fresher_role`, `fresher_reason`, and `classified_by`, and
the Fresher filter uses the model verdict rather than a title/experience heuristic.

The classifier avoids unnecessary model calls:

- Seniority words such as Manager, Staff, Lead, Senior, Principal, Director, Head, and Architect are
  immediately marked not fresher-friendly.
- Explicit fresher, graduate, internship, entry-level, trainee, `0-1 years`, or "no experience
  required" wording is immediately marked fresher-friendly.
- Only ambiguous newly discovered jobs are sent to Ollama or Gemini.
