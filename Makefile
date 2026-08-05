.PHONY: test run build up metrics

test:
	python3 -m unittest discover -s tests -t . -v

run:
	python3 exporter.py

build:
	docker compose build

up:
	docker compose up --build -d

metrics:
	curl -sf http://127.0.0.1:9488/metrics | head -n 40
