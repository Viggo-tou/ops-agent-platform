import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { startTransition, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";

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
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamError, setStreamError] = useState<string | null>(null);
  // When set, the next submit will dispatch as a continuation of this task.
  // Activated by the "继续修复" button on a failed task; cleared after submit.
  const [continueFromTaskId, setContinueFromTaskId] = useState<string | null>(null);
  // Per-conversation source override; persists in localStorage so a user's
  // choice survives navigation. Empty string = use env default.
  const [sourceName, setSourceName] = useState<string>(() => {
    return window.localStorage.getItem("ops-agent-chat-source") ?? "";
  });
  const [searchParams, setSearchParams] = useSearchParams();

  useEffect(() => {
    if (sourceName) {
      window.localStorage.setItem("ops-agent-chat-source", sourceName);
    } else {
      window.localStorage.removeItem("ops-agent-chat-source");
    }
  }, [sourceName]);

  const sourcesQuery = useQuery({
    queryKey: ["repository-sources"],
    queryFn: () => api.listRepositorySources(),
    refetchInterval: 60_000,
  });

  // Model picker (global setting; PATCH affects all future tasks).
  const modelProvidersQuery = useQuery({
    queryKey: ["model-providers"],
    queryFn: () => api.getModelProviders(),
    refetchInterval: 5 * 60_000,
  });
  const selectedModelQuery = useQuery({
    queryKey: ["selected-model"],
    queryFn: () => api.getSelectedModel(),
    refetchInterval: 30_000,
  });
  const selectedModelMutation = useMutation({
    mutationFn: (modelId: string) => api.setSelectedModel({ model_id: modelId }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["selected-model"] });
    },
  });
  const modelOptions = useMemo(() => {
    const out: { provider: string; id: string; display: string }[] = [];
    for (const p of modelProvidersQuery.data ?? []) {
      for (const m of p.models ?? []) {
        out.push({ provider: p.name, id: m.id, display: m.display_name });
      }
    }
    return out;
  }, [modelProvidersQuery.data]);

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

  async function submitMessage(
    message: string,
    files: File[] = [],
    options: { previousTaskId?: string | null } = {},
  ) {
    if (!can("task:create")) {
      return;
    }
    const attachmentNote =
      files.length > 0
        ? `\n\nAttached context: ${files.map((file) => `${file.name} (${file.type || "file"})`).join(", ")}`
        : "";
    const userMessage = `${message}${attachmentNote}`;

    // Streaming path: every chat message goes through /api/chat/send. The
    // backend's primary model decides whether it's a question (answer inline)
    // or a real task (kicks off the heavy pipeline). We stream tokens into
    // the optimistic task's latest_result_json so MessageList renders the
    // answer as it arrives.
    const tempTask: TaskDetail = {
      ...buildOptimisticTask(`temp-${Date.now()}`, userMessage),
      actor_name: user?.name ?? "member",
      actor_role: backendActorRole,
      session_id: task?.session_id ?? null,
      title: task?.title ?? "New request",
      status: "running",
      scenario: "process_question",
    };

    setOptimisticTask(tempTask);
    setIsStreaming(true);
    setStreamError(null);
    scrollToLatestMessage();

    let answerSoFar = "";
    let visibleAnswerSoFar = "";
    let finalTaskCreated = false;
    let kickedOffPipeline = false;
    let createdTaskId: string | null = null;

    const stripIntent = (text: string) =>
      text
        .split("\n")
        .filter((line) => !line.trim().startsWith("TASK_INTENT|"))
        .join("\n")
        .trim();

    try {
      const stream = api.chatSendStream({
        message: userMessage,
        session_id: task?.session_id ?? null,
        source_name: sourceName || null,
        actor_name: user?.name ?? null,
        previous_task_id: options.previousTaskId ?? null,
      });

      for await (const event of stream) {
        switch (event.type) {
          case "token":
            answerSoFar += event.text;
            visibleAnswerSoFar = stripIntent(answerSoFar);
            setOptimisticTask((prev) =>
              prev
                ? {
                    ...prev,
                    status: "running",
                    latest_result_json: {
                      kind: "chat_answer",
                      answer: visibleAnswerSoFar,
                    },
                    updated_at: new Date().toISOString(),
                  }
                : prev,
            );
            scrollToLatestMessage();
            break;
          case "task_created":
            finalTaskCreated = true;
            kickedOffPipeline = event.kicked_off_pipeline;
            createdTaskId = event.task_id;
            break;
          case "provider_failed":
            // Transient — chain falls through automatically. No-op.
            break;
          case "error":
            setStreamError(event.message);
            setOptimisticTask((prev) =>
              prev
                ? {
                    ...prev,
                    status: "failed",
                    updated_at: new Date().toISOString(),
                    latest_result_json: {
                      kind: "chat_answer",
                      answer: visibleAnswerSoFar,
                      message: event.message,
                    },
                  }
                : prev,
            );
            setIsStreaming(false);
            return;
          case "session":
          case "end":
            break;
        }
      }
    } catch (error) {
      const msg = toErrorMessage(error);
      setStreamError(msg);
      setOptimisticTask((prev) =>
        prev
          ? {
              ...prev,
              status: "failed",
              updated_at: new Date().toISOString(),
              latest_result_json: {
                kind: "chat_answer",
                answer: visibleAnswerSoFar,
                message: msg,
              },
            }
          : prev,
      );
      setIsStreaming(false);
      return;
    } finally {
      setIsStreaming(false);
    }

    if (finalTaskCreated && createdTaskId) {
      // Refresh task lists so sidebar + thread queries pick up the new task.
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["tasks"] }),
        queryClient.invalidateQueries({ queryKey: ["tasks", "sidebar"] }),
        queryClient.invalidateQueries({ queryKey: ["tasks", "session"] }),
      ]);

      if (kickedOffPipeline) {
        // Real task: navigate to its detail page so the user can watch the
        // pipeline run live (SSE-driven).
        startTransition(() => {
          void navigate(`/chat/${createdTaskId}`);
        });
        return;
      }

      // Question: fetch the persisted task so the optimistic temp task gets
      // replaced with the canonical row (preserves on reload).
      try {
        const realTask = await api.getTask(createdTaskId);
        setOptimisticTask({
          ...realTask,
          // Merge in case backend's persisted answer is shorter than what we
          // streamed (rare race).
          latest_result_json: realTask.latest_result_json ?? {
            kind: "chat_answer",
            answer: visibleAnswerSoFar,
          },
        });
        scrollToLatestMessage();
      } catch {
        // Keep the optimistic task with status=completed.
        setOptimisticTask((prev) =>
          prev
            ? {
                ...prev,
                id: createdTaskId!,
                status: "completed",
                updated_at: new Date().toISOString(),
              }
            : prev,
        );
      }
    } else {
      // Stream ended without a task_created event — keep what we have but
      // mark it completed.
      setOptimisticTask((prev) =>
        prev
          ? {
              ...prev,
              status: "completed",
              latest_result_json: {
                kind: "chat_answer",
                answer: visibleAnswerSoFar,
              },
            }
          : prev,
      );
    }
  }

  // Reference legacy buildThreadRequest only when needed; streaming path no
  // longer pre-folds previous conversation since SYSTEM_PROMPT + the chat
  // model handle context naturally. Keeping it referenced silences unused
  // import warnings during the transition.
  void buildThreadRequest;

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

  // When the user navigates here from sidebar's "继续修复 →" link
  // (?continue=1), pre-activate continuation mode for the latest failed
  // task in the thread. This lets sidebar quick-action skip the in-chat
  // toggle click.
  useEffect(() => {
    if (searchParams.get("continue") !== "1") {
      return;
    }
    const latest = visibleThreadTasks[visibleThreadTasks.length - 1];
    if (latest && latest.status === "failed") {
      setContinueFromTaskId(latest.id);
    }
    // Clear the query param so reloading doesn't keep re-activating.
    const next = new URLSearchParams(searchParams);
    next.delete("continue");
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams, visibleThreadTasks]);
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

      {(() => {
        const latest = visibleThreadTasks[visibleThreadTasks.length - 1];
        if (!latest || latest.status !== "failed") {
          return null;
        }
        const reason =
          (latest.latest_result_json as { reason?: string } | null)?.reason ??
          (latest.review_summary || latest.title || "上次任务失败");
        return (
          <div className="continue-banner">
            <div className="continue-banner-text">
              上次任务失败：<code>{String(reason).slice(0, 80)}</code>
            </div>
            <div className="continue-banner-actions">
              <button
                type="button"
                className={`continue-toggle ${continueFromTaskId === latest.id ? "active" : ""}`}
                onClick={() =>
                  setContinueFromTaskId(continueFromTaskId === latest.id ? null : latest.id)
                }
                disabled={!can("task:create")}
                title="下一条消息会带上失败上下文一起发"
              >
                {continueFromTaskId === latest.id ? "✓ 继续模式已开" : "继续修复"}
              </button>
            </div>
          </div>
        );
      })()}

      <ChatInput
        onSubmit={(message, files) =>
          submitMessage(message, files, { previousTaskId: continueFromTaskId }).finally(() =>
            setContinueFromTaskId(null),
          )
        }
        isSubmitting={isStreaming}
        disabled={!can("task:create")}
        permissionDenied={!can("task:create") ? "Your current role can view conversations but cannot create new tasks." : null}
        sources={(sourcesQuery.data?.sources ?? []).map((s) => ({ name: s.name, origin: s.origin }))}
        sourceValue={sourceName}
        onSourceChange={setSourceName}
        models={modelOptions}
        modelValue={selectedModelQuery.data?.model_id ?? ""}
        onModelChange={(id) => selectedModelMutation.mutate(id)}
      />
      <div className="chat-footer-hint">AI 生成内容仅供参考</div>

      {streamError ? <div className="chat-error">{streamError}</div> : null}
    </div>
  );
}
