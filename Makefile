# BetBot — common operational targets.
# Works with both `make` (Unix) and `make` shipped with Git Bash on Windows.

.PHONY: help install test up down logs migrate enrich resolve scan dry-run \
        backtest dashboard prod-up prod-down clean fresh \
        docker-stats docker-clean docker-nuke rebuild

help:
	@echo "BetBot — Make targets"
	@echo ""
	@echo "  Local development:"
	@echo "    make install        Install Python dependencies"
	@echo "    make test           Run pytest (39 tests)"
	@echo "    make up             Start dev stack (db + redis + worker + api + dashboard)"
	@echo "    make down           Stop dev stack"
	@echo "    make logs           Tail combined logs"
	@echo "    make dashboard      Open dashboard in browser"
	@echo ""
	@echo "  Data pipeline:"
	@echo "    make migrate        Apply Alembic migrations"
	@echo "    make scan           Run a real scan + email"
	@echo "    make dry-run        Run a scan without sending email"
	@echo "    make enrich         Refresh ELO + xG enrichment"
	@echo "    make resolve        Resolve pending predictions"
	@echo "    make backtest       Backtest the model on EPL (last 100 matches)"
	@echo ""
	@echo "  Production:"
	@echo "    make prod-up        Start prod stack with Caddy + HTTPS"
	@echo "    make prod-down      Stop prod stack"
	@echo ""
	@echo "  Clean:"
	@echo "    make clean          Remove Python caches + logs"
	@echo "    make fresh          DELETE all data and rebuild from scratch"
	@echo ""
	@echo "  Docker disk hygiene:"
	@echo "    make docker-stats   Show Docker disk usage breakdown"
	@echo "    make docker-clean   Reclaim build cache + dangling images (safe)"
	@echo "    make docker-nuke    Aggressive: also remove unused images (CONFIRM)"
	@echo "    make rebuild        Rebuild images and immediately reclaim cache"

install:
	pip install -r requirements.txt

test:
	pytest tests/ -q

up:
	docker compose up -d
	@echo ""
	@echo "  → Dashboard : http://localhost:8501"
	@echo "  → API docs  : http://localhost:8000/docs"

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

migrate:
	docker compose run --rm migrate

scan:
	docker compose exec worker python -m betbot.main --once

dry-run:
	docker compose exec worker python -m betbot.main --dry-run

enrich:
	docker compose exec worker python -m betbot.main --enrich

resolve:
	docker compose exec worker python -m betbot.main --resolve

backtest:
	docker compose exec worker python -m betbot.main --backtest soccer_epl --backtest-n 100

dashboard:
	@python -c "import webbrowser; webbrowser.open('http://localhost:8501')"

prod-up:
	@test -f .env || (echo 'ERROR: .env required (see .env.example)'; exit 1)
	@grep -q 'BETBOT_DOMAIN=' .env || (echo 'ERROR: BETBOT_DOMAIN missing in .env'; exit 1)
	docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
	@echo ""
	@echo "  → Production stack started — HTTPS will provision automatically"
	@echo "  → Wait ~30s for Let's Encrypt then check https://$$(grep BETBOT_DOMAIN .env | cut -d= -f2)"

prod-down:
	docker compose -f docker-compose.yml -f docker-compose.prod.yml down

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name .pytest_cache -prune -exec rm -rf {} +
	rm -f betbot.log*

fresh:
	docker compose down -v
	@echo "All volumes wiped. Re-run: make up"

# ----------------------------------------------------------------------------
# Docker disk hygiene
# ----------------------------------------------------------------------------

docker-stats:
	@docker system df

docker-clean:
	@echo "Reclaiming Docker build cache + dangling layers..."
	@docker builder prune -af
	@docker image prune -f
	@docker system df

docker-nuke:
	@echo "AGGRESSIVE cleanup — also removes images NOT currently used by a container."
	@echo "This will NOT touch your running services (db, redis, betbot, lean-server, etc.)"
	@docker builder prune -af
	@docker image prune -af
	@docker container prune -f
	@docker volume prune -f
	@docker system df

# Rebuild without cache then reclaim the temp build cache in one shot.
# Use this instead of `docker compose build --no-cache` when you really need
# a fresh build but don't want 10 GB of build cache to linger.
rebuild:
	@echo "Rebuilding image (no cache)..."
	@docker compose build --no-cache
	@echo "Reclaiming the build cache that just got created..."
	@docker builder prune -af
	@echo "Done. Run: make up"
