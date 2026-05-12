# Ops Agent Platform Backend

Phase 1 MVP backend for the Enterprise Ops Agent Platform.

## Scope

- FastAPI application
- Persistent `task`, `event`, and `approval` models
- Minimal task, event, approval, and rollback APIs
- Single-runtime orchestrator with a mock tool gateway

## Local Development

1. Install dependencies:

```bash
python -m pip install -r requirements.txt
```

2. Start the API from `apps/backend`:

```bash
uvicorn app.main:app --reload
```

3. Open:

- `http://127.0.0.1:8000/docs`
- `http://127.0.0.1:8000/api/tasks`

## Configuration

Environment variables are optional for local development.

- `OPS_AGENT_APP_NAME`
- `OPS_AGENT_DEBUG`
- `OPS_AGENT_DATABASE_URL`
- `OPS_AGENT_PRIMARY_AGENT_PROVIDER`
- `OPS_AGENT_PRIMARY_AGENT_MODEL`
- `OPS_AGENT_OPENAI_API_KEY`
- `OPS_AGENT_OPENAI_BASE_URL`

Default local database:

- `sqlite:///./ops_agent_platform.db`

The backend is intentionally simple for Phase 1. It can later be pointed to PostgreSQL without changing the API layer.

## Primary Agent Modes

`OPS_AGENT_PRIMARY_AGENT_PROVIDER` supports:

- `auto`: use OpenAI when an API key is present, otherwise fallback to the deterministic planner
- `mock`: always use the deterministic planner
- `openai`: try OpenAI first and fallback safely if the call fails

Default model:

- `OPS_AGENT_PRIMARY_AGENT_MODEL=gpt-4o-mini`
