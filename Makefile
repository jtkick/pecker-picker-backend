up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f backend

demo:
	docker compose exec backend python manage.py demo_users sync

migrate:
	docker compose exec backend python manage.py migrate

migrations:
	docker compose exec backend python manage.py makemigrations

shell:
	docker compose exec backend python manage.py shell

# ── Production (on the server) ──────────────────────────────────────────────
PROD = docker compose -f docker-compose.prod.yml

deploy:
	git pull --ff-only
	$(PROD) up -d --build
	$(PROD) ps

prod-logs:
	$(PROD) logs -f --tail=200 backend

prod-shell:
	$(PROD) exec backend python manage.py shell

backup:
	deploy/backup.sh
