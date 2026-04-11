import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useDeferredValue, useState } from "react";

import { TaskSummaryMetrics } from "../../components/tasks/TaskSummaryMetrics";
import { TaskTable } from "../../components/tasks/TaskTable";
import { ToolRegistryPanel } from "../../components/tasks/ToolRegistryPanel";
import { api } from "../../lib/api";
import { formatSyncTime, toErrorMessage } from "../../lib/format";
import type { TaskStatus } from "../../types";

const STATUS_OPTIONS: Array<{ label: string; value: "all" | TaskStatus }> = [
  { label: "All Statuses", value: "all" },
  { label: "Created", value: "created" },
  { label: "Planning", value: "planning" },
  { label: "Reviewing", value: "reviewing" },
  { label: "Awaiting Approval", value: "awaiting_approval" },
  { label: "Executing", value: "executing" },
  { label: "Completed", value: "completed" },
  { label: "Failed", value: "failed" },
  { label: "Rolled Back", value: "rolled_back" },
];

const PROVIDER_OPTIONS = [
  { label: "All Providers", value: "all" },
  { label: "Mock", value: "mock" },
  { label: "MiniMax", value: "minimax" },
  { label: "OpenAI", value: "openai" },
  { label: "Unknown", value: "unknown" },
] as const;

export function TaskListPage() {
  const [search, setSearch] = useState("");
  const [sessionFilter, setSessionFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState<"all" | TaskStatus>("all");
  const [providerFilter, setProviderFilter] = useState<(typeof PROVIDER_OPTIONS)[number]["value"]>("all");

  const deferredSearch = useDeferredValue(search.trim());
  const deferredSessionFilter = useDeferredValue(sessionFilter.trim());
  const filters = {
    search: deferredSearch || undefined,
    sessionId: deferredSessionFilter || undefined,
    status: statusFilter === "all" ? undefined : statusFilter,
    provider: providerFilter === "all" ? undefined : providerFilter,
  };
  const hasActiveFilters = Boolean(filters.search || filters.sessionId || filters.status || filters.provider);

  const tasksQuery = useQuery({
    queryKey: ["tasks", filters],
    queryFn: () => api.listTasks(filters),
    refetchInterval: 5_000,
  });

  const registryQuery = useQuery({
    queryKey: ["tool-registry"],
    queryFn: () => api.getToolRegistry(),
    refetchInterval: 15_000,
  });

  function clearFilters() {
    setSearch("");
    setSessionFilter("");
    setStatusFilter("all");
    setProviderFilter("all");
  }

  return (
    <div className="stack">
      <section className="page-header-card">
        <div>
          <div className="eyebrow">Dashboard</div>
          <h2>Task List</h2>
          <p>Browse persisted tasks, execution stage, approval state, and recent updates.</p>
        </div>
        <div className="header-actions">
          <div className="live-hint">{formatSyncTime(tasksQuery.dataUpdatedAt)}</div>
          <Link to="/submit" className="button primary link-button">
            New Request
          </Link>
        </div>
      </section>

      {tasksQuery.data ? <TaskSummaryMetrics tasks={tasksQuery.data} /> : null}
      {registryQuery.data ? <ToolRegistryPanel tools={registryQuery.data} compact /> : null}
      {registryQuery.isError ? <div className="error-banner">{toErrorMessage(registryQuery.error)}</div> : null}

      <section className="detail-card">
        <div className="toolbar filter-toolbar">
          <div className="filter-grid">
            <label className="field">
              <span>Search</span>
              <input
                className="text-input search-input"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Title, task id, stage, or model"
              />
            </label>

            <label className="field">
              <span>Session ID</span>
              <input
                className="text-input"
                value={sessionFilter}
                onChange={(event) => setSessionFilter(event.target.value)}
                placeholder="Filter by session id"
              />
            </label>

            <label className="field">
              <span>Status</span>
              <select
                className="select-input"
                value={statusFilter}
                onChange={(event) => setStatusFilter(event.target.value as "all" | TaskStatus)}
              >
                {STATUS_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>

            <label className="field">
              <span>Plan Provider</span>
              <select
                className="select-input"
                value={providerFilter}
                onChange={(event) =>
                  setProviderFilter(event.target.value as (typeof PROVIDER_OPTIONS)[number]["value"])
                }
              >
                {PROVIDER_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <div className="filter-actions">
            <div className="muted-text">
              {tasksQuery.data ? `${tasksQuery.data.length} matching task${tasksQuery.data.length === 1 ? "" : "s"}` : "Loading..."}
            </div>
            <button type="button" className="button ghost" onClick={clearFilters} disabled={!hasActiveFilters}>
              Clear Filters
            </button>
          </div>
        </div>

        {tasksQuery.isLoading ? <div className="loading-panel">Loading tasks...</div> : null}
        {tasksQuery.isError ? <div className="error-banner">{toErrorMessage(tasksQuery.error)}</div> : null}
        {tasksQuery.data ? (
          <TaskTable
            tasks={tasksQuery.data}
            emptyTitle={hasActiveFilters ? "No tasks match current filters" : "No tasks yet"}
            emptyMessage={
              hasActiveFilters
                ? "Try clearing one or more filters to broaden the current dashboard view."
                : "Submit a request to create the first task in this local environment."
            }
          />
        ) : null}
      </section>
    </div>
  );
}
