import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { startTransition, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { ChatInput } from "../../components/chat/ChatInput";
import { buildAgentReply, FOLLOW_UP_MARKER, MessageList, readDisplayRequestText } from "../../components/chat/MessageList";
import { useAuth } from "../../lib/auth";
import { api } from "../../lib/api";
import { toErrorMessage } from "../../lib/format";
import type { EventRecord, TaskDetail } from "../../types";

const suggestedPrompts = [
  "Plan this Jira task for implementation: https://p69projecta.atlassian.net/jira/software/projects/P69/boards/34/backlog?selectedIssue=P69-10",
  "Where should I look to debug customer login in the Handyman app?",
  "Create a Jira bug for the login regression in project P69.",
];

function buildOptimisticTask(id: string, requestText: string, status: TaskDetail["status"] = "created"): TaskDetail {
  const now = new Date().toISOString();
  return {
    id,
    session_id: null,
    actor_name: "member",
    actor_role: "employee",
    title: "New request",
    scenario: "process_question",
    status,
    workflow_stage: "intake",
    current_role: null,
    risk_level: "low",
    risk_category: "general",
    pending_approval: false,
    retry_count: 0,
    plan_provider_name: null,
    plan_provider_mode: null,
    plan_model_name: null,
    plan_used_fallback: false,
    plan_fallback_reason: null,
    review_stage: null,
    review_verdict: null,
    review_summary: null,
    created_at: now,
    updated_at: now,
    request_text: requestText,
    governance_json: null,
    translation_json: null,
    plan_json: null,
    review_json: null,
    latest_result_json: null,
    approvals: [],
  };
}

function isOptimisticTaskId(taskId: string): boolean {
  return taskId.startsWith("temp-");
}

export function ChatPage() {
  const { taskId } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { user, backendActorRole, can } = useAuth();
  const chatScrollRef = useRef<HTMLElement | null>(null);
  const [optimisticTask, setOptimisticTask] = useState<TaskDetail | null>(null);

  const taskQuery = useQuery({
    queryKey: ["task", taskId],
    queryFn: () => api.getTask(taskId!),
    enabled: Boolean(taskId),
    refetchInterval: taskId ? 3_000 : false,
  });
  const task = taskQuery.data ?? null;

  const sessionTasksQuery = useQuery({
    queryKey: ["tasks", "session", task?.session_id],
    queryFn: () => api.listTasks({ sessionId: task!.session_id! }),
    enabled: Boolean(task?.session_id),
    refetchInterval: taskId ? 5_000 : false,
  });

  const threadDetailQueries = useQueries({
    queries: (sessionTasksQuery.data ?? []).map((summary) => ({
      queryKey: ["task", summary.id],
      queryFn: () => api.getTask(summary.id),
      enabled: Boolean(task?.session_id),
      refetchInterval:
        summary.status === "completed" || summary.status === "failed" || summary.status === "rolled_back" ? false : 3_000,
    })),
  });

  const createTaskMutation = useMutation({
    mutationFn: api.createTask,
    onSuccess: async (task) => {
      await queryClient.invalidateQueries({ queryKey: ["tasks"] });
      await queryClient.invalidateQueries({ queryKey: ["tasks", "sidebar"] });
      await queryClient.invalidateQueries({ queryKey: ["tasks", "session"] });
      startTransition(() => {
        void navigate(`/chat/${task.id}`);
      });
    },
  });

  function buildThreadRequest(message: string) {
    if (!task) {
      return message;
    }

    const previousUser = readDisplayRequestText(task.request_text);
    const previousAssistant = buildAgentReply(task);
    return `Conversation context for follow-up. Use this context to answer the follow-up naturally.

Previous user request:
${previousUser}

Previous assistant answer:
${previousAssistant.slice(0, 3000)}${FOLLOW_UP_MARKER}${message}`;
  }

  function scrollToLatestMessage() {
    window.requestAnimationFrame(() => {
      const scrollElement = chatScrollRef.current;
      if (scrollElement) {
        scrollElement.scrollTo({ top: scrollElement.scrollHeight, behavior: "smooth" });
      }
    });
  }

  async function submitMessage(message: string, files: File[] = []) {
    if (!can("task:create")) {
      return;
    }
    const attachmentNote =
      files.length > 0
        ? `\n\nAttached context: ${files.map((file) => `${file.name} (${file.type || "file"})`).join(", ")}`
        : "";
    const userMessage = `${message}${attachmentNote}`;
    const request = buildThreadRequest(userMessage);
    const tempTask = {
      ...buildOptimisticTask(`temp-${Date.now()}`, request),
      actor_name: user?.name ?? "member",
      actor_role: backendActorRole,
      session_id: task?.session_id ?? null,
      title: task?.title ?? "New request",
    };

    setOptimisticTask(tempTask);
    scrollToLatestMessage();

    try {
      const createdTask = await createTaskMutation.mutateAsync({
        title: task?.title,
        request,
        actor_name: user?.name ?? "member",
        actor_role: backendActorRole,
        session_id: task?.session_id ?? undefined,
      });
      setOptimisticTask(createdTask);
      scrollToLatestMessage();
    } catch (error) {
      setOptimisticTask({
        ...tempTask,
        status: "failed",
        updated_at: new Date().toISOString(),
        latest_result_json: { message: toErrorMessage(error) },
      });
    }
  }

  const threadTasks = useMemo(
    () =>
      threadDetailQueries
        .map((query) => query.data)
        .filter((candidate): candidate is NonNullable<typeof candidate> => Boolean(candidate))
        .sort((left, right) => new Date(left.created_at).getTime() - new Date(right.created_at).getTime()),
    [threadDetailQueries],
  );
  const visibleThreadTasks = useMemo(() => {
    const baseTasks = threadTasks.length > 0 ? threadTasks : task ? [task] : [];
    if (!optimisticTask || baseTasks.some((baseTask) => baseTask.id === optimisticTask.id)) {
      return baseTasks;
    }
    return [...baseTasks, optimisticTask];
  }, [optimisticTask, task, threadTasks]);
  const eventQueries = useQueries({
    queries: visibleThreadTasks.map((messageTask) => ({
      queryKey: ["task-events", messageTask.id],
      queryFn: () => api.getTaskEvents(messageTask.id),
      enabled: !isOptimisticTaskId(messageTask.id),
      refetchInterval:
        isOptimisticTaskId(messageTask.id) ||
        messageTask.status === "completed" ||
        messageTask.status === "failed" ||
        messageTask.status === "rolled_back"
          ? false
          : 2_000,
    })),
  });
  const eventsMap = visibleThreadTasks.reduce<Record<string, EventRecord[]>>((accumulator, messageTask, index) => {
    const events = eventQueries[index]?.data;
    if (events?.length) {
      accumulator[messageTask.id] = events;
    }
    return accumulator;
  }, {});

  return (
    <div className="chat-page">
      <header className="chat-header">
        <div className="chat-brand">
          <button type="button" className="chat-close-button" onClick={() => void navigate("/home")} aria-label="返回首页">
            ×
          </button>
          <strong>Knowledge Assistant</strong>
        </div>
        <div className="chat-header-actions">
          <button type="button" className="model-select-pill">
            GLM-5 智谱 AI ▼
          </button>
        </div>
      </header>

      <section className="chat-scroll" ref={chatScrollRef}>
        {taskQuery.isError ? <div className="error-banner">{toErrorMessage(taskQuery.error)}</div> : null}
        {taskQuery.isLoading ? <div className="loading-panel minimal">Loading conversation...</div> : null}
        {!taskId && !optimisticTask ? (
          <div className="starter-panel">
            <MessageList task={null} />
            <div className="starter-prompts">
              {suggestedPrompts.map((prompt) => (
                <button key={prompt} type="button" onClick={() => submitMessage(prompt)} disabled={!can("task:create")}>
                  {prompt}
                </button>
              ))}
            </div>
          </div>
        ) : null}
        {visibleThreadTasks.length > 0 ? <MessageList tasks={visibleThreadTasks} eventsMap={eventsMap} /> : null}
      </section>

      <ChatInput
        onSubmit={(message, files) => submitMessage(message, files)}
        isSubmitting={createTaskMutation.isPending}
        disabled={!can("task:create")}
        permissionDenied={!can("task:create") ? "Your current role can view conversations but cannot create new tasks." : null}
      />
      <div className="chat-footer-hint">AI 生成内容仅供参考</div>

      {createTaskMutation.isError ? <div className="chat-error">{toErrorMessage(createTaskMutation.error)}</div> : null}
    </div>
  );
}
