.PHONY: install dev sim test lint fmt typecheck call clean

install:
	uv sync --extra dev

dev:
	uv run uvicorn dialpass.main:app --reload --host 0.0.0.0 --port 8000

# Offline pipeline: synthesize a menu->hold->interjection->human call and run it
# through the real detection loop + FSM. No phone, no paid APIs.
sim:
	uv run python scripts/simulate_call.py $(ARGS)

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff format .

typecheck:
	uv run mypy src

# Trigger a real outbound call once M2 lands: make call TO=+18005550100 FROM=+15145550123
call:
	curl -s -X POST localhost:8000/calls \
		-H 'content-type: application/json' \
		-d '{"business_number":"$(TO)","user_number":"$(FROM)","goal":"$(GOAL)"}' | python3 -m json.tool

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache dist *.egg-info
	find . -name __pycache__ -type d -exec rm -rf {} +
