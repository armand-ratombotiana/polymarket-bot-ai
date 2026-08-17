# Polymarket Bot + Web UI — Makefile

BOT_IMAGE  := polymarket-bot
UI_IMAGE   := polymarket-webui
TAG        := latest
API_PORT   := 8080
UI_PORT    := 3010

.PHONY: help build build-bot build-ui up live down logs cancel status lint \
        lint-bot lint-ui test shell-bot shell-ui clean

help:
	@echo ""
	@echo "  Polymarket Bot — Available commands"
	@echo "  ────────────────────────────────────────────────────────────────"
	@echo "  make build        Build both bot and webui Docker images"
	@echo "  make up           Start bot (paper) + web UI  → http://localhost:$(UI_PORT)"
	@echo "  make live         Start bot (LIVE) + web UI   (real money!)"
	@echo "  make down         Stop all services"
	@echo "  make logs         Tail logs from all containers"
	@echo "  make cancel       Emergency: cancel ALL open orders"
	@echo "  make status       Print risk status from running bot"
	@echo "  make lint         Run all linters (Python + TypeScript)"
	@echo "  make test         Run Python test suite (pytest) in .venv"
	@echo "  make shell-bot    Open bash in the bot container"
	@echo "  make shell-ui     Open sh in the webui container"
	@echo "  make clean        Remove containers and images"
	@echo ""

# ── Build ─────────────────────────────────────────────────────────────────────

build: build-bot build-ui

build-bot:
	docker build -t $(BOT_IMAGE):$(TAG) .

build-ui:
	docker build \
	  --build-arg NEXT_PUBLIC_API_URL=http://localhost:$(API_PORT) \
	  --build-arg NEXT_PUBLIC_WS_URL=ws://localhost:$(API_PORT)/ws \
	  -t $(UI_IMAGE):$(TAG) ./webui

# ── Run ───────────────────────────────────────────────────────────────────────

up: build
	@echo "🚀 Starting paper-trade bot + web UI → http://localhost:$(UI_PORT)"
	docker compose --profile paper up -d
	@echo "📺 Dashboard: http://localhost:$(UI_PORT)"
	@echo "🔌 API:       http://localhost:$(API_PORT)"

live: build
	@echo "⚠️  Starting LIVE trading bot + web UI — REAL MONEY AT RISK!"
	docker compose --profile live up -d
	@echo "📺 Dashboard: http://localhost:$(UI_PORT)"
	@echo "🔌 API:       http://localhost:$(API_PORT)"

down:
	docker compose --profile paper --profile live down

logs:
	docker compose --profile paper --profile live logs -f --tail=100

# ── Emergency ─────────────────────────────────────────────────────────────────

cancel:
	@echo "⚠️  Cancelling ALL open orders..."
	curl -s -X DELETE http://localhost:$(API_PORT)/api/orders | python -m json.tool

status:
	curl -s http://localhost:$(API_PORT)/api/status | python -m json.tool

# ── Lint & Test ───────────────────────────────────────────────────────────────

lint: lint-bot lint-ui

lint-bot:
	python -m compileall -q \
	  config.py main.py watchdog.py \
	  core ml strategies risk paper api dashboard backtesting execution
	ruff check config.py main.py watchdog.py core ml strategies risk paper api dashboard backtesting execution tests
	@echo "✅ Python: no syntax errors"

test:
	.venv/Scripts/python.exe -m pytest

lint-ui:
	cd webui && npm run build -- --no-lint 2>&1 | tail -5

# ── Shell ─────────────────────────────────────────────────────────────────────

shell-bot:
	docker exec -it polymarket-bot bash

shell-ui:
	docker exec -it polymarket-webui sh

# ── Clean ─────────────────────────────────────────────────────────────────────

clean: down
	docker rmi $(BOT_IMAGE):$(TAG) $(UI_IMAGE):$(TAG) 2>/dev/null || true
	docker volume rm polymarket-bot_bot-logs 2>/dev/null || true
