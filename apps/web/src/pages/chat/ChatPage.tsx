import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { startTransition, useMemo } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { ChatInput } from "../../components/chat/ChatInput";
import { buildAgentReply, FOLLOW_UP_MARKER, MessageList, readDisplayRequestText } from "../../components/chat/MessageList";
import { PermissionGuard } from "../../components/auth/PermissionGuard";
import { useAuth } from "../../lib/auth";
import { api } from "../../lib/api";
import { toErrorMessage } from "../../lib/format";

const suggestedPrompts = [
  "Plan this Jira task for implementation: https://p69projecta.atlassian.net/jira/software/projects/P69/boards/34/backlog?selectedIssue=P69-10",
  "Where should I look to debug customer login in the Handyman app?",
  "Create a Jira bug for the login regression in project P69.",
];

export function ChatPage() {
  const { taskId } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { user, backendActorRole, can } = useAuth();

  const taskQuery = useQuery({
    queryKey: ["task", taskId],
    queryFn: () => api.getTask(taskId!),
    enabled: Boolean(taskId),
    refetchInterval: taskId ? 5_000 : false,
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

  function submitMessage(message: string, files: File[] = []) {
    if (!can("task:create")) {
      return;
    }
    const attachmentNote =
      files.length > 0
        ? `\n\nAttached context: ${files.map((file) => `${file.name} (${file.type || "file"})`).join(", ")}`
        : "";
    const userMessage = `${message}${attachmentNote}`;
    createTaskMutation.mutate({
      title: task?.title,
      request: buildThreadRequest(userMessage),
      actor_name: user?.name ?? "member",
      actor_role: backendActorRole,
      session_id: task?.session_id ?? undefined,
    });
  }

  const threadTasks = useMemo(
    () =>
      threadDetailQueries
        .map((query) => query.data)
        .filter((candidate): candidate is NonNullable<typeof candidate> => Boolean(candidate))
        .sort((left, right) => new Date(left.created_at).getTime() - new Date(right.created_at).getTime()),
    [threadDetailQueries],
  );
  const visibleThreadTasks = threadTasks.length > 0 ? threadTasks : task ? [task] : [];

  return (
    <div className="chat-page">
      <header className="chat-header">
        <div>
          <span>Knowledge Assistant</span>
          <h1>{task?.title ?? "New conversation"}</h1>
        </div>
        <div className="chat-header-actions">
          <span className="model-select-pill">GLM-5 Zhipu AI</span>
          <PermissionGuard
            permission="settings:view"
            fallback={<span className="muted-inline">Ready</span>}
          >
            <Link to="/settings" className="small-link">
              Settings
            </Link>
          </PermissionGuard>
        </div>
      </header>

      <section className="chat-scroll">
        {taskQuery.isError ? <div className="error-banner">{toErrorMessage(taskQuery.error)}</div> : null}
        {taskQuery.isLoading ? <div className="loading-panel minimal">Loading conversation...</div> : null}
        {!taskId ? (
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
        {task ? <MessageList tasks={visibleThreadTasks} /> : null}
      </section>

      <ChatInput
        onSubmit={(message, files) => submitMessage(message, files)}
        isSubmitting={createTaskMutation.isPending}
        disabled={!can("task:create")}
        permissionDenied={!can("task:create") ? "Your current role can view conversations but cannot create new tasks." : null}
      />

      {createTaskMutation.isError ? <div className="chat-error">{toErrorMessage(createTaskMutation.error)}</div> : null}
    </div>
  );
}
