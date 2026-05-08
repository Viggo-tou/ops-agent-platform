import type {
  Approval,
  ActorRole,
  EventRecord,
  KnowledgeDeleteResponse,
  KnowledgeDocumentSummary,
  KnowledgeSourceDescriptor,
  KnowledgeSyncResponse,
  KnowledgeUploadResponse,
  MemoryItem,
  MemoryItemCreate,
  MemoryItemUpdate,
  MemorySettings,
  MemorySettingsUpdate,
  ModelProvider,
  SelectedModel,
  SelectedModelUpdate,
  TaskCreateInput,
  TaskDetail,
  TaskListFilters,
  TaskSummary,
  ToolExecutionRecord,
  ToolRegistryEntry,
} from "../types";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000/api";

type ApprovalDecisionPayload = {
  actor_name: string;
  actor_role: ActorRole | string;
  notes?: string;
};

let currentActorRole: string | null = null;
let currentAppRole: string | null = null;

export function setApiActor(actorRole: string | null, appRole: string | null) {
  currentActorRole = actorRole;
  currentAppRole = appRole;
}

function buildHeaders(headers?: HeadersInit, includeJson = false): Headers {
  const nextHeaders = new Headers(headers);
  if (includeJson && !nextHeaders.has("Content-Type")) {
    nextHeaders.set("Content-Type", "application/json");
  }
  if (currentActorRole) {
    nextHeaders.set("X-Actor-Role", currentActorRole);
    if (currentAppRole !== null) {
      nextHeaders.set("X-Actor-App-Role", currentAppRole);
    }
  }
  return nextHeaders;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: buildHeaders(init?.headers, true),
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

async function requestMultipart<T>(path: string, body: FormData): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    headers: buildHeaders(),
    body,
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

function buildApprovalDecisionPayload(
  decision: ApprovalDecisionPayload | string,
  notes?: string,
): ApprovalDecisionPayload {
  if (typeof decision === "string") {
    return { actor_name: decision, actor_role: "team_lead", notes };
  }
  return decision;
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
  listRepositorySources: () =>
    request<{
      sources: {
        name: string;
        path: string;
        description: string;
        origin: string;
        git_url: string;
        added_at: string;
      }[];
      multi_source_enabled: boolean;
    }>("/repositories/sources"),
  uploadRepositoryZip: ({
    name,
    description,
    file,
  }: {
    name: string;
    description: string;
    file: File;
  }) => {
    const fd = new FormData();
    fd.append("name", name);
    fd.append("description", description);
    fd.append("file", file);
    return requestMultipart<{
      name: string;
      path: string;
      origin: string;
      description: string;
    }>("/repositories/upload", fd);
  },
  cloneRepositoryGit: (body: { name: string; git_url: string; description: string }) =>
    request<{
      name: string;
      path: string;
      origin: string;
      description: string;
      git_url: string;
    }>("/repositories/clone", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  deleteRepositorySource: (name: string) =>
    request<{ removed: boolean }>(`/repositories/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),
  listRbacRoles: () =>
    request<
      {
        role_key: string;
        display_name: string;
        description: string;
        is_human: boolean;
        is_system: boolean;
        is_active: boolean;
      }[]
    >("/governance/roles"),
  listPolicyRules: () =>
    request<
      {
        id: string;
        rule_key: string;
        title: string;
        description: string;
        subject_role: string;
        resource_type: string;
        action_key: string;
        tool_name: string | null;
        decision: string;
        risk_level: string;
        risk_category: string;
        required_approver_role: string | null;
        priority: number;
        is_active: boolean;
      }[]
    >("/governance/policy-rules"),
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
  uploadKnowledgeFiles: (files: File[], sourceName?: string) => {
    const body = new FormData();
    for (const file of files) {
      body.append("files", file, file.name);
    }
    if (sourceName) {
      body.append("source_name", sourceName);
    }
    return requestMultipart<KnowledgeUploadResponse>("/knowledge/upload", body);
  },
  deleteKnowledgeDocument: (documentId: string) =>
    request<KnowledgeDeleteResponse>(`/knowledge/documents/${documentId}`, {
      method: "DELETE",
    }),
  deleteKnowledgeSource: (sourceName: string) =>
    request<KnowledgeDeleteResponse>(`/knowledge/sources/${encodeURIComponent(sourceName)}`, {
      method: "DELETE",
    }),
  listMemoryItems: (search?: string) => {
    const params = new URLSearchParams();
    const normalized = search?.trim();
    if (normalized) {
      params.set("search", normalized);
    }
    const query = params.toString();
    return request<MemoryItem[]>(query ? `/memory/items?${query}` : "/memory/items");
  },
  createMemoryItem: (payload: MemoryItemCreate) =>
    request<MemoryItem>("/memory/items", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  updateMemoryItem: (itemId: string, payload: MemoryItemUpdate) =>
    request<MemoryItem>(`/memory/items/${encodeURIComponent(itemId)}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  deleteMemoryItem: (itemId: string) =>
    request<{ ok: boolean }>(`/memory/items/${encodeURIComponent(itemId)}`, {
      method: "DELETE",
    }),
  getMemorySettings: () => request<MemorySettings>("/memory/settings"),
  updateMemorySettings: (payload: MemorySettingsUpdate) =>
    request<MemorySettings>("/memory/settings", {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  getModelProviders: () => request<ModelProvider[]>("/model-config/providers"),
  getSelectedModel: () => request<SelectedModel>("/model-config/selected"),
  setSelectedModel: (payload: SelectedModelUpdate) =>
    request<SelectedModel>("/model-config/selected", {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  createTask: (input: TaskCreateInput) =>
    request<TaskDetail>("/tasks", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  grantApproval: (approvalId: string, decision: ApprovalDecisionPayload | string, notes?: string) =>
    request<Approval>(`/approvals/${approvalId}/grant`, {
      method: "POST",
      body: JSON.stringify(buildApprovalDecisionPayload(decision, notes)),
    }),
  rejectApproval: (approvalId: string, decision: ApprovalDecisionPayload | string, notes?: string) =>
    request<Approval>(`/approvals/${approvalId}/reject`, {
      method: "POST",
      body: JSON.stringify(buildApprovalDecisionPayload(decision, notes)),
    }),
  rollbackTask: (taskId: string, actorName: string, reason: string) =>
    request<TaskDetail>(`/tasks/${taskId}/rollback`, {
      method: "POST",
      body: JSON.stringify({ actor_name: actorName, reason }),
    }),
};
