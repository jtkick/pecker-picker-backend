PORT ?= 46696

run:
	source venv/bin/activate && python manage.py runserver 0.0.0.0:$(PORT)

migrate:
	source venv/bin/activate && python manage.py migrate

migrations:
	source venv/bin/activate && python manage.py makemigrations

shell:
	source venv/bin/activate && python manage.py shell
