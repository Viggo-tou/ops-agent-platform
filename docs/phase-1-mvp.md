# Phase 1 MVP

## Goal
Deliver a single-runtime, single-primary-agent MVP with persistent tasks and visible execution state.

## User flow
1. user submits request
2. backend creates task
3. primary agent generates a plan
4. plan is stored
5. events are recorded
6. dashboard shows task + plan + event history

## Included
- task submission page
- task list page
- task detail/log page
- task API
- event API
- task/event/approval tables
- mock tools only

## Not included
- real Jira integration
- multiple agents
- background workers
- advanced approval engine
- production-grade RBAC engine