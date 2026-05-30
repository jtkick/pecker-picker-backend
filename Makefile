up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f backend

seed:
	docker compose exec backend python manage.py seed_test_users

unseed:
	docker compose exec backend python manage.py seed_test_users --clear

migrate:
	docker compose exec backend python manage.py migrate

migrations:
	docker compose exec backend python manage.py makemigrations

shell:
	docker compose exec backend python manage.py shell
