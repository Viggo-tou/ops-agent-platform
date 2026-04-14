# Repo Index

Lightweight map of key modules so new sessions don't need to read the whole repo.
Last updated: 2026-04-12

## Backend (`apps/backend/`)

### Core
| File | Purpose |
|------|---------|
| `app/core/config.py` | Settings (Pydantic BaseSettings): DB, providers, sandbox config |
| `app/core/enums.py` | Shared enums: RiskLevel, RiskCategory, ActorRole, PolicyDecision |
| `app/core/database.py` | SQLite via SQLAlchemy, session factory |

### API Layer
| File | Purpose |
|------|---------|
| `app/api/tasks.py` | POST /api/tasks, GET /api/tasks/{id}, approval endpoints |
| `app/api/knowledge.py` | Knowledge search/import endpoints |
| `app/api/governance.py` | Policy rules, audit log read APIs |
| `app/api/health.py` | GET /health |

### Services
| File | Purpose |
|------|---------|
| `app/services/tasks.py` | Task CRUD, status transitions, _build_title, _infer_risk_category |
| `app/services/knowledge.py` | Knowledge search, document indexing |
| `app/services/governance.py` | Policy evaluation, DEFAULT_POLICY_RULES seed data |
| `app/services/sandbox.py` | ExecutionSandbox: clone, run, apply_patch, teardown |
| `app/services/test_pipeline.py` | TestPipeline: reads tests.yaml, runs steps via sandbox |

### Agents
| File | Purpose |
|------|---------|
| `app/agents/service.py` | PrimaryAgentPlanner.generate_plan, ActionAgent.build_payload |
| `app/agents/translation.py` | SemanticTranslator: request → GeneratedSemanticTranslation |
| `app/agents/schemas.py` | Dataclasses: GeneratedSemanticTranslation, PlanResult, ToolStep |

### Orchestrator
| File | Purpose |
|------|---------|
| `app/orchestrator/service.py` | PrimaryOrchestrator: classify_request → bootstrap_task → execute |

### Tools
| File | Purpose |
|------|---------|
| `app/tools/registry.py` | ToolRegistry: ToolDefinition catalog, _seed_tools() |
| `app/tools/gateway.py` | ToolGateway: dispatcher → _execute_*() methods |

### Tests
| Directory | Coverage |
|-----------|----------|
| `tests/orchestrator/` | test_jira_writeback_scenario (6 tests) |
| `tests/services/` | test_sandbox (6), test_sandbox_patch (4), test_pipeline (5) |
| `tests/tools/` | test_jira_writeback (5) |

## Frontend (`apps/web/`)

### Pages
| File | Purpose |
|------|---------|
| `src/pages/chat/ChatPage.tsx` | Main chat interface, session management |
| `src/pages/home/HomePage.tsx` | Landing/dashboard |
| `src/pages/knowledge/KnowledgePage.tsx` | Knowledge base management |
| `src/pages/memory/MemoryPage.tsx` | Memory/context management |
| `src/pages/settings/SettingsPage.tsx` | Model/provider config |

### Key Components
| File | Purpose |
|------|---------|
| `src/components/layout/AppShell.tsx` | Main layout, sidebar, navigation |
| `src/components/chat/MessageList.tsx` | Chat message rendering |
| `src/components/chat/ChatInput.tsx` | Chat input box |

## Interface Contracts

### Scenario routing
```
classify_request(text) → scenario string
  "jira_issue_plan" | "jira_issue_create" | "jira_issue_writeback" | "process_question"
```

### Tool registry pattern
```python
ToolDefinition(name, display_name, description, provider_name,
               permission_category, enabled, status_message,
               missing_configuration, requires_network,
               timeout_seconds, retry_count, tags)
```

### Gateway dispatch pattern
```python
# In _execute_tool_impl:
if definition.name == "tool.name":
    return self._execute_tool_name(definition=definition, payload=payload)
```

### Governance seed pattern
```python
# In DEFAULT_POLICY_RULES:
"tool.name.role.decision.v1": { rule_key, subject_role, decision, risk_level, ... }
```

### Sandbox interface
```python
sandbox = ExecutionSandbox(task_id, base_dir=settings.sandbox_base_dir)
sandbox.clone(repo_url, branch=None, timeout_seconds=120) → dict
sandbox.run(command, cwd=None, timeout_seconds=60) → dict{exit_code, stdout, stderr, duration_ms, timed_out}
sandbox.apply_patch(patch, commit=True, commit_message=...) → dict{before_sha, after_sha, committed}
sandbox.teardown()
```

### TestPipeline interface
```python
pipeline = TestPipeline(sandbox)
result = pipeline.run(config_path="tests.yaml") → TestRunResult{steps, overall_passed, total_steps, ...}
```

## Roadmap Phase Status (2026-04-12)

| Phase | Status | Tasks | Tests |
|-------|--------|-------|-------|
| A (Workbench persistence) | Partial | T-026-01, T-026-02 done; A-3/4/5 pending | — |
| B (Jira writeback) | Done | T-B1, T-B2 | 6 |
| C (Sandbox) | Done | T-C1, T-C2 | 10 |
| D (Test pipeline) | Done | T-D1 | 5 |
| E (Diff reviewer) | Done | T-E1 | 8 |
| F (Approval gate) | Done | T-F1 | 6 |
| G (Rollback) | In progress | T-G1 dispatched | — |
| H (Chat lifecycle) | Pending | Not spec'd | — |
| I (structlog) | Pending | Not spec'd | — |
| J (OpenTelemetry) | Pending | Not spec'd | — |
| K (Metrics + cost) | Pending | Not spec'd | — |
| L (Alerting + health) | Pending | Not spec'd | — |
