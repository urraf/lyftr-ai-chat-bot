.PHONY: up down logs build test dev-backend dev-frontend clean

# Use the compose v2 plugin if present, otherwise the standalone docker-compose binary.
COMPOSE := $(shell docker compose version >/dev/null 2>&1 && echo "docker compose" || echo docker-compose)

up:            ## Build and start backend (:8000) + frontend (:3000)
	$(COMPOSE) up -d --build
	@echo "Backend:  http://localhost:8000/health"
	@echo "Frontend: http://localhost:3000"

down:          ## Stop the stack (keeps the SQLite volume)
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f

build:
	$(COMPOSE) build

test:          ## Run backend tests locally
	cd backend && python -m pytest -q

dev-backend:   ## Run backend without Docker (needs .env exported)
	cd backend && set -a && . ../.env && set +a && uvicorn app.main:app --reload --port 8000

dev-frontend:  ## Run frontend without Docker
	cd frontend && npm install && npm run dev

clean:         ## Stop and delete the SQLite volume
	$(COMPOSE) down -v
