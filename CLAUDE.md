# CLAUDE.md

- Output only what is asked.
- If uncertain: omit. Do not guess.
- Bullets > prose. Prefer deletion over verbosity.
- Do not rewrite whole files; make surgical edits.

## Hard Rules
- Django 6 / DRF backend; all app logic lives in `api/`.
- All DB changes require a migration — never modify schema without `makemigrations`.
- Run via Docker; do not run `manage.py` directly outside the container.
- `.env` / secrets are never committed.
- `make migrations` generates files INSIDE the container — they are NOT written to the host `api/migrations/` dir unless the source is bind-mounted. Always verify the file appeared on disk after running; if not, write it manually and rebuild.
- After adding a new model or field: `make migrations && make migrate`, then rebuild the container (`docker compose up -d --build`) so the running image has the updated code.
- JWT logout is handled via `POST api/auth/logout/` — requires `rest_framework_simplejwt.token_blacklist` in `INSTALLED_APPS` (already present).

## Authority & Links
- [requirements.txt](requirements.txt) — Python dependencies
- [pecker_picker/settings.py](pecker_picker/settings.py) — Django settings
- [api/models.py](api/models.py) — data models
- [api/views.py](api/views.py) — API views
- [api/urls.py](api/urls.py) — URL routing
- [docker-compose.yml](docker-compose.yml) — service definition
- [Makefile](Makefile) — all common commands

## Setup / Test
- `make up` — start Docker services
- `make migrate` — apply migrations
- `docker compose exec backend python manage.py test`

## Workflow
- Start: `make up`
- Logs: `make logs`
- New migration: `make migrations && make migrate` then `docker compose up -d --build`
- Shell: `make shell`
- Seed data: `make seed`
- Seed kinks: `docker compose exec backend python manage.py seed_kinks`
- Seed interests: `docker compose exec backend python manage.py seed_interests`

## Stop Conditions
- Missing `.env` values → ask before proceeding
- Destructive migrations (drop column/table) → stop and confirm
- `docker compose down -v` (volume wipe) → stop and confirm
- Production deploy → stop and confirm
