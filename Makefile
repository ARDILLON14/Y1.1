.PHONY: install test test-pg lint format typecheck demo run secrets up up-live up-monitoring down logs backup

install:            ## entorno de desarrollo
	python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev,perf]"

test:               ## tests (SQLite)
	.venv/bin/pytest -q

test-pg:            ## tests de integración contra PostgreSQL (COPYTRADER_TEST_PG=postgresql+asyncpg://...)
	.venv/bin/pytest -q tests/integration

lint:
	.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests

format:
	.venv/bin/ruff format src tests && .venv/bin/ruff check --fix src tests

typecheck:
	.venv/bin/mypy src

demo:               ## demo local con mercado simulado (sin claves)
	COPYTRADER__APP__OPERATING_LEVEL=3 COPYTRADER__PROVIDERS__MODE=simulated \
	COPYTRADER__API__SECURE_COOKIES=false .venv/bin/copytrader run

run:
	.venv/bin/copytrader run

secrets:            ## genera ./secrets para Docker
	./scripts/init-secrets.sh

up:                 ## niveles 1-3 (sin firmador)
	docker compose up -d --build

up-live:            ## añade el firmador (niveles 4-5)
	docker compose --profile live up -d --build

up-monitoring:
	docker compose --profile monitoring up -d

down:
	docker compose --profile live --profile monitoring down

logs:
	docker compose logs -f app

backup:             ## copia de seguridad de PostgreSQL
	mkdir -p backups && docker compose exec -T postgres pg_dump -U copytrader copytrader | gzip > backups/copytrader-$$(date +%Y%m%d-%H%M%S).sql.gz
