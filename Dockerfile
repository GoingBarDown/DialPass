FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv

WORKDIR /app
ENV UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv

COPY pyproject.toml README.md ./
COPY src ./src
RUN uv sync --no-dev

COPY . .

EXPOSE 8000
CMD ["uv", "run", "--no-dev", "uvicorn", "dialpass.main:app", "--host", "0.0.0.0", "--port", "8000"]
