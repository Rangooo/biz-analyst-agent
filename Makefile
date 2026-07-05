PYTHON ?= python
PIP ?= $(PYTHON) -m pip
FRONTEND_DIR := frontend

.PHONY: install install-optional install-frontend test test-backend test-frontend eval-golden dev backend frontend

install:
	$(PIP) install -r backend/requirements.txt

install-optional:
	$(PIP) install -r backend/requirements-optional.txt

install-frontend:
	cd $(FRONTEND_DIR) && npm ci

test: test-backend test-frontend

test-backend:
	cd backend && $(PYTHON) run_all_tests.py --quick

test-frontend:
	cd $(FRONTEND_DIR) && npm run build

eval-golden:
	cd backend && $(PYTHON) -m evals.golden_answers --report

dev:
	@echo "Start backend and frontend in two terminals:"
	@echo "  make backend"
	@echo "  make frontend"

backend:
	cd backend && $(PYTHON) -m uvicorn main:app --host 127.0.0.1 --port 8000

frontend:
	cd $(FRONTEND_DIR) && npm run dev -- --host 127.0.0.1
