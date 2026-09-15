# MTProto Proxy Intelligence Platform (MVP)

## Purpose
An automation platform to continuously discover, validate, observe, and score public MTProto proxies to find usable candidates for Telegram censorship evasion.

## Current MVP Scope
- **Discovery**: Scrapes predefined sources for MTProto proxy links.
- **Normalization & Deduplication**: Parses and hashes proxies to avoid redundant testing.
- **Testing**: Validates protocol correctness and connectivity using a bare MTProto client.
- **Observation**: Records historical success/failure and latency.
- **Scoring**: Calculates moving averages for proxy reliability.

## Technology Stack
- **Language**: Python 3.11+ (Managed with `uv`)
- **Database**: Managed PostgreSQL (SQLAlchemy 2.x + asyncpg)
- **Migrations**: Alembic
- **Logging**: structlog

## Local Setup

1. **Install uv**:
   `curl -LsSf https://astral.sh/uv/install.sh | sh`
   
2. **Install dependencies**:
   `uv sync`
   
3. **Environment**:
   Copy `.env.example` to `.env` and configure accordingly.

## Operations

**Run Tests**:
`uv run pytest`

**Run Linting**:
`uv run ruff check .`
`uv run mypy .`

**Run Workers (Locally)**:
`uv run python -m src.workers.discovery`
`uv run python -m src.workers.tester`
`uv run python -m src.workers.scorer`

## Upcoming Tasks
- **Task 002**: Database models and migrations configuration.
- **Task 003**: Implement Discovery worker.
- **Task 004**: Implement Tester worker (Telethon integration).