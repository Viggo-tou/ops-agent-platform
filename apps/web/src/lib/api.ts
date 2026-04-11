import type {
  Approval,
  EventRecord,
  KnowledgeDocumentSummary,
  KnowledgeSourceDescriptor,
  KnowledgeSyncResponse,
  TaskCreateInput,
  TaskDetail,
  TaskListFilters,
  TaskSummary,
  ToolExecutionRecord,
  ToolRegistryEntry,
} from "../types";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    ...init,
  });

  if (!response.ok) {
    const detail = await response
      .json()
      .then((payload) => payload.detail ?? response.statusText)
      .catch(() => response.statusText);
    throw new Error(String(detail));
  }

  return (await response.json()) as T;
}

export const api = {
  baseUrl: API_BASE_URL,
  listTasks: (filters: TaskListFilters = {}) => {
    const params = new URLSearchParams();

    if (filters.search) {
      params.set("search", filters.search);
    }
    if (filters.sessionId) {
      params.set("session_id", filters.sessionId);
    }
    if (filters.status) {
      params.set("status", filters.status);
    }
    if (filters.provider) {
      params.set("provider", filters.provider);
    }
    if (filters.actorRole) {
      params.set("actor_role", filters.actorRole);
    }
    if (filters.riskCategory) {
      params.set("risk_category", filters.riskCategory);
    }

    const query = params.toString();
    return request<TaskSummary[]>(query ? `/tasks?${query}` : "/tasks");
  },
  getTask: (taskId: string) => request<TaskDetail>(`/tasks/${taskId}`),
  getTaskEvents: (taskId: string) => request<EventRecord[]>(`/tasks/${taskId}/events`),
  getTaskToolExecutions: (taskId: string) => request<ToolExecutionRecord[]>(`/tasks/${taskId}/tool-executions`),
  getToolRegistry: () => request<ToolRegistryEntry[]>("/tools/registry"),
  getKnowledgeSources: () => request<KnowledgeSourceDescriptor[]>("/knowledge/sources"),
  getKnowledgeDocuments: (sourceName?: string, limit = 100) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (sourceName) {
      params.set("source_name", sourceName);
    }
    return request<KnowledgeDocumentSummary[]>(`/knowledge/documents?${params.toString()}`);
  },
  syncKnowledge: (sourceName?: string) => {
    const params = new URLSearchParams();
    if (sourceName) {
      params.set("source_name", sourceName);
    }
    const query = params.toString();
    return request<KnowledgeSyncResponse>(query ? `/knowledge/sync?${query}` : "/knowledge/sync", {
      method: "POST",
    });
  },
  createTask: (input: TaskCreateInput) =>
    request<TaskDetail>("/tasks", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  grantApproval: (approvalId: string, actorName: string, notes?: string) =>
    request<Approval>(`/approvals/${approvalId}/grant`, {
      method: "POST",
      body: JSON.stringify({ actor_name: actorName, notes }),
    }),
  rejectApproval: (approvalId: string, actorName: string, notes?: string) =>
    request<Approval>(`/approvals/${approvalId}/reject`, {
      method: "POST",
      body: JSON.stringify({ actor_name: actorName, notes }),
    }),
  rollbackTask: (taskId: string, actorName: string, reason: string) =>
    request<TaskDetail>(`/tasks/${taskId}/rollback`, {
      method: "POST",
      body: JSON.stringify({ actor_name: actorName, reason }),
    }),
};
