.PHONY: up down logs build test dev-backend dev-frontend clean

up:            ## Build and start backend (:8000) + frontend (:3000)
	docker compose up -d --build
	@echo "Backend:  http://localhost:8000/health"
	@echo "Frontend: http://localhost:3000"

down:          ## Stop the stack (keeps the SQLite volume)
	docker compose down

logs:
	docker compose logs -f

build:
	docker compose build

test:          ## Run backend tests locally
	cd backend && python -m pytest -q

dev-backend:   ## Run backend without Docker (needs .env exported)
	cd backend && set -a && . ../.env && set +a && uvicorn app.main:app --reload --port 8000

dev-frontend:  ## Run frontend without Docker
	cd frontend && npm install && npm run dev

clean:         ## Stop and delete the SQLite volume
	docker compose down -v
