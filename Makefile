.PHONY: test up down logs ps smoke backup

test:
	python3 -m pytest -q

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f app

ps:
	docker compose ps

smoke:
	./scripts/smoke.sh

backup:
	docker compose exec db pg_dump -U $${POSTGRES_USER:-attribution} $${POSTGRES_DB:-attribution} > backup_$$(date +%F).sql
