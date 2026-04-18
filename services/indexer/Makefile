dev:
	docker compose up --build

worker:
	docker compose up worker --build

test:
	docker compose run --rm app python -m pytest tests/ -v

test-unit:
	docker compose run --rm app python -m pytest \
		tests/test_hashing.py tests/test_chunker.py \
		tests/test_validate.py tests/test_parse.py tests/test_cache.py -v

bench:
	python scripts/benchmark.py --group all --limit 3 --out results_latest.json

bulk:
	python scripts/bulk_index.py --group all --limit 10 --concurrency 2

migrate:
	docker compose exec postgres psql -U vault -d vault -f /app/app/db/schema.sql

logs:
	docker compose logs -f app worker

clean:
	docker compose down -v
