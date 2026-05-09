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


/**
 * Map an EventRecord to an intermediate-pipeline milestone, if it should
 * surface as a chat status pill. Returns null for events the user
 * doesn't need to see in chat (most low-level lifecycle / debug rows).
 *
 * Each milestone key must be stable (so dedup across event refetch
 * works) — built from event_type + tool_name where needed.
 */
function milestoneFromEvent(
  ev: EventRecord,
): { key: string; label: string; detail?: string } | null {
  const et = ev.event_type;
  const tool = ev.tool_name || "";
  const msg = ev.message || "";

  if (et === "plan_generated") {
    return { key: "plan_generated", label: "生成执行计划", detail: "planner ✓" };
  }
  if (et === "knowledge_retrieved") {
    return { key: "knowledge_retrieved", label: "知识检索完成", detail: "knowledge ✓" };
  }
  if (et === "tool_succeeded" && tool === "codegen.generate_patch") {
    // Each codegen success uses a distinct key (the event id) so
    // multiple rounds (initial + repair) all show.
    return {
      key: `codegen_${ev.id}`,
      label: "代码 patch 生成完成",
      detail: msg.slice(0, 90) || "codegen ✓",
    };
  }
  if (et === "tool_succeeded" && tool === "sandbox.apply_patch") {
    return {
      key: `sandbox_apply_${ev.id}`,
      label: "patch 已应用到 sandbox",
      detail: "sandbox ✓",
    };
  }
  if (et === "tool_succeeded" && /Diff shape check passed/i.test(msg)) {
    return { key: `diff_shape_${ev.id}`, label: "diff 形态检查通过" };
  }
  if (et === "tool_succeeded" && /compile-only verification|compile.*passed/i.test(msg)) {
    return { key: `compile_${ev.id}`, label: "编译通过" };
  }
  if (et === "compile_failed") {
    return { key: `compile_failed_${ev.id}`, label: "编译失败,进入修复轮", detail: msg.slice(0, 90) };
  }
  if (et === "tool_call_requested" && /Compile repair round (\d+) starting/i.test(msg)) {
    const m = msg.match(/Compile repair round (\d+)/i);
    return {
      key: `repair_round_${m?.[1] ?? "x"}_${ev.id}`,
      label: `修复 round ${m?.[1] ?? "?"} 启动`,
      detail: msg.slice(0, 90),
    };
  }
  if (et === "review_passed") {
    return { key: "review_passed", label: "审查通过", detail: msg.slice(0, 90) };
  }
  if (et === "tool_succeeded" && tool === "diff_reviewer.review") {
    return { key: "diff_reviewer", label: "diff reviewer 通过" };
  }
  if (et === "tool_succeeded" && /Spec conformance passed/i.test(msg)) {
    return { key: "spec_conformance", label: "spec 一致性通过" };
  }
  if (et === "tool_succeeded" && /Goal attestation/i.test(msg)) {
    return { key: "goal_attestation", label: "goal attestation ✓" };
  }
  if (et === "tool_succeeded" && /Evidence chain closed/i.test(msg)) {
    return { key: "evidence_chain", label: "evidence chain 闭环 ✓" };
  }
  if (et === "approval_requested") {
    return { key: "approval_requested", label: "已请求人工审批", detail: msg.slice(0, 90) };
  }
  return null;
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
  const abortRef = useRef<AbortController | null>(null);

  // Append-only feed of status transitions detected while user is on this
  // page. When a background task changes status (e.g. P69-21 finishes
  // codegen → enters awaiting_approval), we push a system bubble onto
  // this list and render it at the BOTTOM of the chat scroll. That way
  // the latest event is always at the bottom (matching every other chat
  // app's mental model) — without it, the EventTimeline of the OLD turn
  // updates in place and the user has to scroll up to find what changed.
  type ReservationDetail = {
    text: string;
    severity: "bug" | "missing_test" | "security" | "policy" | "style" | string;
    auto_fixable?: boolean;
    blocking?: boolean;
  };
  type StatusUpdate = {
    id: string;                     // unique key (task_id + kind + label)
    kind: "status" | "milestone";   // status = terminal transition; milestone = intermediate progress
    task_id: string;
    new_status: string;             // for status: TaskStatus; for milestone: short label like 'codegen_done'
    prev_status: string | null;
    scenario: string | null;
    title: string | null;
    timestamp: number;
    label?: string;                 // human-readable label for milestones (e.g. "代码生成完成")
    detail?: string;                // optional one-liner for milestones
    reservations?: string[];        // back-compat plain strings (status only)
    reservations_detailed?: ReservationDetail[];  // tagged with severity (status only)
  };
  const [statusUpdates, setStatusUpdates] = useState<StatusUpdate[]>([]);
  // Map of task_id → last-seen status, used to detect transitions.
  const lastStatusByIdRef = useRef<Record<string, string>>({});
  // Map of task_id → set of milestone keys already pushed, used to dedup
  // intermediate-progress notifications (otherwise compile_passed could
  // fire many times if the events list is re-fetched).
  const milestonesByIdRef = useRef<Record<string, Set<string>>>({});
  const stopStreaming = () => {
    const c = abortRef.current;
    if (c && !c.signal.aborted) {
      try { c.abort(); } catch { /* noop */ }
    }
  };
  // When set, the next submit will dispatch as a continuation of this task.
  // Activated by the "继续修复" button on a failed task; cleared after submit.
  const [continueFromTaskId, setContinueFromTaskId] = useState<string | null>(null);

  // Reset per-conversation transient state when the URL switches between
  // /chat/A and /chat/B. Without this, the optimisticTask from the previous
  // thread bleeds into the new thread and the user sees the message they
  // just sent appear inside an unrelated conversation. Also clear streaming
  // and continue-mode flags so the new thread starts clean.
  useEffect(() => {
    setOptimisticTask(null);
    setIsStreaming(false);
    setStreamError(null);
    setContinueFromTaskId(null);
    // Cancel any in-flight stream owned by the previous thread so it can't
    // mutate state after we've moved on.
    if (abortRef.current && !abortRef.current.signal.aborted) {
      try { abortRef.current.abort(); } catch { /* noop */ }
    }
    abortRef.current = null;
  }, [taskId]);
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
      // Seed empty chat_answer so buildAgentReply takes the chat path
      // immediately and doesn't flash the process_question fallback
      // 'I could not produce a grounded repository answer...' message.
      // creation_status drives the inline status block (pending → created
      // / failed). It's "pending" until backend confirms via
      // task_created / task_create_failed event.
      latest_result_json: {
        kind: "chat_answer",
        answer: "",
        creation_status: "pending",
      },
    };

    setOptimisticTask(tempTask);
    setIsStreaming(true);
    setStreamError(null);
    scrollToLatestMessage();

    // Abort plumbing: lets the user click "停止" mid-stream AND auto-aborts
    // if no event arrives within 30s (handles backend hangs — e.g.
    // 'database is locked' on persistence — without leaving the input
    // permanently disabled).
    const abortController = new AbortController();
    abortRef.current = abortController;
    let watchdog: number | null = null;
    const armWatchdog = () => {
      if (watchdog !== null) window.clearTimeout(watchdog);
      watchdog = window.setTimeout(() => {
        try { abortController.abort(); } catch { /* noop */ }
      }, 30_000);
    };
    armWatchdog();

    let answerSoFar = "";
    let visibleAnswerSoFar = "";
    let finalTaskCreated = false;
    let kickedOffPipeline = false;
    let createdTaskId: string | null = null;
    let createFailureReason: string | null = null;
    let createFailureAdvice: string | null = null;

    type ChatToolCall = {
      id: string;
      name: string;
      arguments: Record<string, unknown>;
      status: "running" | "done" | "error";
      result?: string;
    };
    const chatToolCalls: ChatToolCall[] = [];
    const pushChatToolCalls = () => {
      setOptimisticTask((prev) =>
        prev
          ? {
              ...prev,
              latest_result_json: {
                ...(prev.latest_result_json ?? {}),
                tool_calls: chatToolCalls.map((c) => ({ ...c })),
              },
              updated_at: new Date().toISOString(),
            }
          : prev,
      );
    };

    /** Apply a creation_status block onto the optimistic task so the inline
     *  TaskCreationStatusBlock renders the right state. The 3 states are:
     *  - 'pending' (default while we're streaming, before backend persists)
     *  - 'created' (task_created event arrived)
     *  - 'failed'  (task_create_failed event arrived) */
    const setCreationStatus = (
      status: "pending" | "created" | "failed",
      extras: Record<string, unknown> = {},
    ) => {
      setOptimisticTask((prev) =>
        prev
          ? {
              ...prev,
              latest_result_json: {
                ...(prev.latest_result_json ?? {}),
                creation_status: status,
                ...extras,
              },
              updated_at: new Date().toISOString(),
            }
          : prev,
      );
    };

    const stripIntent = (text: string) =>
      text
        .split("\n")
        .filter((line) => !line.trim().startsWith("TASK_INTENT|"))
        .join("\n")
        .trim();

    // RPG-style typewriter animator. The base pace is 1 character per tick
    // so the text genuinely "崩" out one by one. If the buffer grows large
    // (backend pushed a long chunk), we accelerate to catch up — otherwise
    // a 200-char paragraph would take ~5s to render which feels sluggish.
    //
    // Pacing curve (chars-per-tick by buffer length):
    //   buffer <  40 chars → 1 char/tick     (~28 chars/sec, RPG feel)
    //   buffer < 120 chars → 2 chars/tick    (~56 chars/sec)
    //   buffer < 300 chars → 4 chars/tick    (~112 chars/sec)
    //   buffer >= 300      → 8 chars/tick    (catch-up, never freezes)
    //
    // Plus we add a tiny extra tick of dwell after end-of-sentence punctuation
    // (。.!?) so periods and pauses feel natural like an RPG dialog box.
    const TICK_MS = 35;
    const SENTENCE_END = new Set(["。", ".", "!", "?", "!", "?"]);
    let renderedSoFar = "";
    let pendingBuffer = "";
    let dwellTicks = 0;
    let streamDone = false;
    const flushTick = window.setInterval(() => {
      if (pendingBuffer.length === 0) return;
      if (dwellTicks > 0) {
        dwellTicks -= 1;
        return;
      }
      let burst = 1;
      if (pendingBuffer.length >= 300) burst = 8;
      else if (pendingBuffer.length >= 120) burst = 4;
      else if (pendingBuffer.length >= 40) burst = 2;
      const take = Math.min(burst, pendingBuffer.length);
      const chunk = pendingBuffer.slice(0, take);
      renderedSoFar += chunk;
      pendingBuffer = pendingBuffer.slice(take);
      // Dwell on sentence endings so periods feel natural.
      if (SENTENCE_END.has(chunk[chunk.length - 1])) {
        dwellTicks = 4; // ~140ms pause after a sentence
      }
      visibleAnswerSoFar = stripIntent(renderedSoFar);
      setOptimisticTask((prev) =>
        prev
          ? {
              ...prev,
              status: "running",
              latest_result_json: {
                ...(prev.latest_result_json ?? {}),
                kind: "chat_answer",
                answer: visibleAnswerSoFar,
              },
              updated_at: new Date().toISOString(),
            }
          : prev,
      );
      scrollToLatestMessage();
    }, TICK_MS);
    const drainAndStop = () => {
      streamDone = true;
      // Final flush: dump everything remaining.
      if (pendingBuffer.length > 0) {
        renderedSoFar += pendingBuffer;
        pendingBuffer = "";
        visibleAnswerSoFar = stripIntent(renderedSoFar);
        setOptimisticTask((prev) =>
          prev
            ? {
                ...prev,
                latest_result_json: {
                  ...(prev.latest_result_json ?? {}),
                  kind: "chat_answer",
                  answer: visibleAnswerSoFar,
                },
                updated_at: new Date().toISOString(),
              }
            : prev,
        );
      }
      window.clearInterval(flushTick);
    };
    void streamDone;  // referenced via closure for future cancel logic

    try {
      const stream = api.chatSendStream({
        message: userMessage,
        session_id: task?.session_id ?? null,
        source_name: sourceName || null,
        actor_name: user?.name ?? null,
        previous_task_id: options.previousTaskId ?? null,
        signal: abortController.signal,
      });

      for await (const event of stream) {
        // Reset the inactivity watchdog on every event from the backend.
        armWatchdog();
        switch (event.type) {
          case "token":
            // Push raw text into the smoother buffer; the interval ticker
            // drains it character-by-character.
            answerSoFar += event.text;
            pendingBuffer += event.text;
            break;
          case "tool_call":
            chatToolCalls.push({
              id: event.id,
              name: event.name,
              arguments: event.arguments ?? {},
              status: "running",
            });
            pushChatToolCalls();
            break;
          case "tool_result": {
            const idx = chatToolCalls.findIndex((c) => c.id === event.tool_call_id);
            if (idx !== -1) {
              chatToolCalls[idx] = {
                ...chatToolCalls[idx],
                status: event.is_error ? "error" : "done",
                result: event.content,
              };
              pushChatToolCalls();
            }
            break;
          }
          case "task_created":
            finalTaskCreated = true;
            kickedOffPipeline = event.kicked_off_pipeline;
            createdTaskId = event.task_id;
            // Render green "✓ 任务已创建" block.
            setCreationStatus("created", {
              task_id: event.task_id,
              scenario_created: event.scenario,
              kicked_off_pipeline: event.kicked_off_pipeline,
            });
            break;
          case "task_create_failed":
            // Render red "✗ 任务未创建" block. We still keep the model's
            // streamed answer text (answer_kept=true).
            createFailureReason = event.reason ?? null;
            createFailureAdvice = event.user_advice ?? null;
            setCreationStatus("failed", {
              create_failure_reason: event.reason,
              create_failure_kind: event.reason_kind,
              create_failure_advice: event.user_advice,
              scenario_intended: event.scenario_intended,
            });
            break;
          case "provider_failed":
            // Transient — chain falls through automatically. No-op.
            break;
          case "error":
            drainAndStop();
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
      // Stream finished cleanly — flush any pending buffered chars.
      drainAndStop();
    } catch (error) {
      drainAndStop();
      const wasAborted =
        abortController.signal.aborted ||
        (error instanceof Error && (error.name === "AbortError" || /aborted/i.test(error.message)));
      const msg = wasAborted
        ? "响应超时或被取消(后端可能 DB 锁住或网络中断)。再发一次就行。"
        : toErrorMessage(error);
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
      // Defensive cleanup — ensures the input is always re-enabled and
      // background timers are released even if the for-await above bails
      // out via an unexpected path.
      if (watchdog !== null) {
        window.clearTimeout(watchdog);
        watchdog = null;
      }
      try { window.clearInterval(flushTick); } catch { /* noop */ }
      if (abortRef.current === abortController) {
        abortRef.current = null;
      }
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

      // Question (no pipeline): replace optimistic temp task with the real
      // persisted row, then navigate to /chat/{taskId} so the URL carries
      // the session_id forward — without this, the user's follow-up message
      // submits with session_id=null and starts a fresh conversation.
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
        // Only navigate if we're currently at /chat (no taskId in URL),
        // because the user's first message just bound a session. Existing
        // /chat/{id} threads stay on their current URL.
        if (!taskId) {
          startTransition(() => {
            void navigate(`/chat/${createdTaskId}`, { replace: true });
          });
        }
      } catch {
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

  // (Status / milestone watcher moved BELOW eventsMap declaration to
  // avoid a TDZ "Cannot access 'eventsMap' before initialization" error.)

  // When taskId switches (cross-conversation reset), wipe status updates.
  useEffect(() => {
    setStatusUpdates([]);
    lastStatusByIdRef.current = {};
    milestonesByIdRef.current = {};
  }, [taskId]);

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

  // Watch all visible tasks for status transitions AND intermediate
  // pipeline milestones. Two kinds of updates land on `statusUpdates`:
  //   - "status"    — terminal/awaiting transitions (full bubble with
  //                   reservations + auto-fix button)
  //   - "milestone" — intermediate progress (planner done, codegen
  //                   done, compile passed/failed, repair starts,
  //                   review passed). Renders as a slim inline pill so
  //                   the user sees the pipeline IS moving in chat,
  //                   without needing to flip to /tasks/{id}.
  // First-seen tasks on initial page load don't notify.
  // (Lives below eventsMap so the dep array can reference it without
  // a temporal-dead-zone error.)
  useEffect(() => {
    const interestingStatus = new Set([
      "completed",
      "failed",
      "rolled_back",
      "rejected",
      "awaiting_approval",
      "waiting_approval",
      "stale_failed",
    ]);
    const next: StatusUpdate[] = [];
    for (const t of visibleThreadTasks) {
      // Skip process_question chat answers — created already-completed.
      if (t.scenario === "process_question") continue;

      // ---- 1) terminal status transitions ----
      const prev = lastStatusByIdRef.current[t.id];
      const cur = t.status;
      const firstSight = prev === undefined;
      if (firstSight) lastStatusByIdRef.current[t.id] = cur;
      if (!firstSight && prev !== cur) {
        lastStatusByIdRef.current[t.id] = cur;
        if (interestingStatus.has(cur)) {
          const result = (t.latest_result_json as { result?: { reservations?: unknown; reservations_detailed?: unknown } } | null)?.result;
          const reservations = (() => {
            const r = result?.reservations;
            if (Array.isArray(r)) return r.filter((x): x is string => typeof x === "string");
            return undefined;
          })();
          const reservations_detailed = (() => {
            const r = result?.reservations_detailed;
            if (!Array.isArray(r)) return undefined;
            return r
              .filter((x): x is Record<string, unknown> => typeof x === "object" && x !== null)
              .map((x) => ({
                text: String(x.text ?? ""),
                severity: String(x.severity ?? "bug"),
                auto_fixable: Boolean(x.auto_fixable),
                blocking: Boolean(x.blocking),
              }))
              .filter((x) => x.text.length > 0);
          })();
          next.push({
            id: `${t.id}-status-${cur}-${t.updated_at}`,
            kind: "status",
            task_id: t.id,
            new_status: cur,
            prev_status: prev || null,
            scenario: t.scenario || null,
            title: t.title || null,
            timestamp: Date.now(),
            reservations,
            reservations_detailed,
          });
        }
      }

      // ---- 2) intermediate milestones from event timeline ----
      const events = eventsMap?.[t.id] ?? [];
      let seenMs = milestonesByIdRef.current[t.id];
      if (!seenMs) {
        seenMs = new Set<string>();
        milestonesByIdRef.current[t.id] = seenMs;
      }
      for (const ev of events) {
        const ms = milestoneFromEvent(ev);
        if (!ms) continue;
        if (seenMs.has(ms.key)) continue;
        seenMs.add(ms.key);
        // Same first-sight gate as status: skip on initial render.
        if (firstSight) continue;
        next.push({
          id: `${t.id}-mile-${ms.key}`,
          kind: "milestone",
          task_id: t.id,
          new_status: ms.key,
          prev_status: null,
          scenario: t.scenario || null,
          title: t.title || null,
          timestamp: Date.now(),
          label: ms.label,
          detail: ms.detail,
        });
      }
    }
    if (next.length === 0) return;
    setStatusUpdates((prev) => {
      const seen = new Set(prev.map((u) => u.id));
      const additions = next.filter((u) => !seen.has(u.id));
      if (additions.length === 0) return prev;
      return [...prev, ...additions];
    });
  }, [visibleThreadTasks, eventsMap]);

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
        {visibleThreadTasks.length > 0 ? (
          <MessageList
            tasks={visibleThreadTasks}
            eventsMap={eventsMap}
            streaming={isStreaming}
            continueFromTaskId={continueFromTaskId}
            onToggleContinueMode={(id) =>
              setContinueFromTaskId(continueFromTaskId === id ? null : id)
            }
            canCreate={can("task:create")}
          />
        ) : null}

        {/* Status feed — system bubbles for tasks that transitioned while
            the user was scrolled / chatting elsewhere. Always rendered at
            the BOTTOM so the latest event is the latest visible item. */}
        {statusUpdates.length > 0 ? (
          <ul className="status-feed" aria-live="polite">
            {statusUpdates.map((u) => (
              <StatusFeedItem key={u.id} update={u} navigate={navigate} />
            ))}
          </ul>
        ) : null}
      </section>

      {/* Failure-action UI for pipeline tasks now renders inline in
          MessageList (FailedPipelineActionBlock) — see MessageList.tsx.
          We just plumb the toggle state + active task id down. */}

      <ChatInput
        onSubmit={(message, files) =>
          submitMessage(message, files, { previousTaskId: continueFromTaskId }).finally(() =>
            setContinueFromTaskId(null),
          )
        }
        onStop={stopStreaming}
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


function statusLabel(status: string): string {
  switch (status) {
    case "completed":         return "已完成";
    case "failed":            return "失败";
    case "rolled_back":       return "已回滚";
    case "rejected":          return "已拒绝";
    case "awaiting_approval":
    case "waiting_approval":  return "待审批";
    case "stale_failed":      return "超时未推进";
    default:                  return status;
  }
}


type ReservationDetail = {
  text: string;
  severity: "bug" | "missing_test" | "security" | "policy" | "style" | string;
  auto_fixable?: boolean;
  blocking?: boolean;
};

const _SEVERITY_LABEL: Record<string, string> = {
  bug: "Bug",
  missing_test: "缺测试",
  security: "Security",
  policy: "Policy",
  style: "Style",
};

const _SEVERITY_TONE: Record<string, "fixable" | "test" | "block-sec" | "block-policy" | "neutral"> = {
  bug: "fixable",
  missing_test: "test",
  security: "block-sec",
  policy: "block-policy",
  style: "neutral",
};

/**
 * System-style bubble at chat bottom for a transitioned task. For
 * awaiting_approval with tagged reservations, renders each item with a
 * severity badge AND a one-click "auto-fix N items" button that triggers
 * /iterate with those items composed into a follow-up prompt.
 */
function StatusFeedItem({
  update,
  navigate,
}: {
  update: {
    kind: "status" | "milestone";
    task_id: string;
    new_status: string;
    scenario: string | null;
    title: string | null;
    label?: string;
    detail?: string;
    reservations?: string[];
    reservations_detailed?: ReservationDetail[];
  };
  navigate: (path: string) => void;
}) {
  const [iterating, setIterating] = useState(false);
  const [iterateError, setIterateError] = useState<string | null>(null);

  // Milestone updates render as a slim inline pill — no reservations,
  // no auto-fix button, just "task title — milestone label" with a
  // small icon. Dedicated branch for clarity.
  if (update.kind === "milestone") {
    const isFailureLike = /fail|超时|timed_out|失败/i.test(update.new_status);
    const tone = isFailureLike ? "fail" : "ok";
    const icon = isFailureLike ? "⚠" : "•";
    return (
      <li className={`status-feed-milestone tone-${tone}`}>
        <span className="status-feed-milestone-icon" aria-hidden="true">{icon}</span>
        <span className="status-feed-milestone-label">{update.label ?? update.new_status}</span>
        {update.detail ? <span className="status-feed-milestone-detail">{update.detail}</span> : null}
        <button
          type="button"
          className="status-feed-milestone-link"
          onClick={() => navigate(`/tasks/${update.task_id}`)}
        >
          #{update.task_id.slice(0, 8)}
        </button>
      </li>
    );
  }

  const tone =
    update.new_status === "completed"
      ? "ok"
      : update.new_status === "awaiting_approval" || update.new_status === "waiting_approval"
        ? "warn"
        : update.new_status === "failed" || update.new_status === "rejected" || update.new_status === "stale_failed"
          ? "fail"
          : "neutral";
  const icon = tone === "ok" ? "✓" : tone === "warn" ? "⚠" : tone === "fail" ? "✗" : "•";

  // Prefer detailed (tagged) form; fall back to plain strings (older runs).
  const detailed: ReservationDetail[] =
    update.reservations_detailed && update.reservations_detailed.length > 0
      ? update.reservations_detailed
      : (update.reservations ?? []).map((t) => ({
          text: t,
          severity: "bug",
          auto_fixable: true,
        }));
  const hasReservations = detailed.length > 0;
  const fixable = detailed.filter((r) => r.auto_fixable);
  const blocking = detailed.filter((r) => r.blocking);

  const submitAutoFix = async () => {
    if (fixable.length === 0) return;
    setIterating(true);
    setIterateError(null);
    const lines = fixable.map((r, i) => `${i + 1}. (${r.severity}) ${r.text}`).join("\n");
    const followUp =
      `请按 reviewer 标记的以下问题逐条修复(都属于自动可修类,不要碰 security/policy 类):\n\n${lines}\n\n` +
      "维持现有 plan 的 must_touch 文件范围,只补漏不扩需求。";
    try {
      const newTask = await api.iterateTask(update.task_id, followUp);
      navigate(`/tasks/${newTask.id}`);
    } catch (e) {
      setIterateError(toErrorMessage(e));
    } finally {
      setIterating(false);
    }
  };

  return (
    <li className={`status-feed-item tone-${tone}`}>
      <div className="status-feed-row">
        <span className="status-feed-icon" aria-hidden="true">{icon}</span>
        <div className="status-feed-body">
          <div className="status-feed-title">
            {update.title ?? `任务 ${update.task_id.slice(0, 8)}`} —
            <strong> {statusLabel(update.new_status)}</strong>
            {hasReservations ? (
              <span className="status-feed-flag">
                {detailed.length} 项保留意见
                {fixable.length > 0 ? ` · ${fixable.length} 可自动修` : ""}
                {blocking.length > 0 ? ` · ${blocking.length} 必须人审` : ""}
              </span>
            ) : null}
          </div>
          <div className="status-feed-actions">
            <button
              type="button"
              className="status-feed-link"
              onClick={() => navigate(`/tasks/${update.task_id}`)}
            >
              打开任务详情 #{update.task_id.slice(0, 8)} →
            </button>
            {fixable.length > 0 ? (
              <button
                type="button"
                className="status-feed-autofix"
                onClick={submitAutoFix}
                disabled={iterating}
                title="把 auto-fixable 类(bug / 缺测试 / style)合成 follow-up,/iterate 出新任务自动修。Security / policy 类不会被触发。"
              >
                {iterating ? "创建迭代中…" : `🔧 一键修复 ${fixable.length} 项`}
              </button>
            ) : null}
          </div>
          {iterateError ? <div className="status-feed-error">{iterateError}</div> : null}
        </div>
      </div>
      {hasReservations ? (
        <details className="status-feed-reservations">
          <summary>展开 {detailed.length} 项审查保留意见(审批前请看)</summary>
          <ol>
            {detailed.map((r, i) => {
              const t = _SEVERITY_TONE[r.severity] ?? "neutral";
              return (
                <li key={i} className={`reservation-row sev-${t}`}>
                  <span className={`reservation-tag tag-${t}`}>{_SEVERITY_LABEL[r.severity] ?? r.severity}</span>
                  <span>{r.text}</span>
                </li>
              );
            })}
          </ol>
        </details>
      ) : null}
    </li>
  );
}
