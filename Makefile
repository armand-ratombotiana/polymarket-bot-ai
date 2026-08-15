# Polymarket Bot + Web UI — Makefile

BOT_IMAGE  := polymarket-bot
UI_IMAGE   := polymarket-webui
TAG        := latest

.PHONY: help build build-bot build-ui up down paper live logs shell \
        lint lint-bot lint-ui cancel status clean

help:
	@echo ""
	@echo "  Polymarket Bot — Available commands"
	@echo "  ────────────────────────────────────────────────────────────────"
	@echo "  make build        Build both bot and webui Docker images"
	@echo "  make up           Start bot (paper) + web UI  → http://localhost:3000"
	@echo "  make live         Start bot (LIVE) + web UI   (real money!)"
	@echo "  make down         Stop all services"
	@echo "  make logs         Tail logs from all containers"
	@echo "  make cancel       Emergency: cancel ALL open orders"
	@echo "  make status       Print risk status from running bot"
	@echo "  make lint         Run all linters (Python + TypeScript)"
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
	  --build-arg NEXT_PUBLIC_API_URL=http://localhost:8000 \
	  --build-arg NEXT_PUBLIC_WS_URL=ws://localhost:8000/ws \
	  -t $(UI_IMAGE):$(TAG) ./webui

# ── Run ───────────────────────────────────────────────────────────────────────

up: build
	@echo "🚀 Starting paper-trade bot + web UI → http://localhost:3000"
	docker compose up -d bot webui
	@echo "📺 Dashboard: http://localhost:3000"
	@echo "🔌 API:       http://localhost:8000"

live: build
	@echo "⚠️  Starting LIVE trading bot + web UI — REAL MONEY AT RISK!"
	docker compose --profile live up -d bot-live webui

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

# ── Emergency ─────────────────────────────────────────────────────────────────

cancel:
	@echo "⚠️  Cancelling ALL open orders..."
	curl -s -X DELETE http://localhost:8000/api/orders | python -m json.tool

status:
	curl -s http://localhost:8000/api/status | python -m json.tool

# ── Lint ──────────────────────────────────────────────────────────────────────

lint: lint-bot lint-ui

lint-bot:
	& "C:\Users\All in one\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe" -m py_compile \
	  config.py main.py \
	  core/clob_client.py core/gamma_client.py core/ws_client.py core/data_store.py \
	  strategies/base.py strategies/market_maker.py strategies/arb_scanner.py strategies/signal_trader.py \
	  risk/manager.py paper/simulator.py dashboard/app.py api/server.py
	@echo "✅ Python: no syntax errors"

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
