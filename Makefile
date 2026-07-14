# Convenience targets. On Windows use: make via Git Bash, or run the commands directly.

.PHONY: up down logs build sample lint test smoke fresh

up:            ## Spin up the whole loop
	docker compose up --build

down:          ## Stop + remove containers
	docker compose down

logs:          ## Tail logs from all services
	docker compose logs -f

build:         ## Rebuild images
	docker compose build

sample:        ## Generate synthetic Jakarta AQ dataset for replay/demo
	python -m ingestion.gen_sample

lint:          ## Ruff lint
	ruff check .

test:          ## Run pytest
	pytest -q

smoke:         ## End-to-end smoke test (needs redis running)
	python -m scripts.smoke

fresh:         ## Nuke data + volumes and start clean
	docker compose down -v
	rm -rf data/*.csv data/registry
