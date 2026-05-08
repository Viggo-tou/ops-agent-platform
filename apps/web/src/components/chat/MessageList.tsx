import { EventTimeline } from "./EventTimeline";
import { MarkdownText } from "./MarkdownText";
import { ThinkingIndicator } from "./ThinkingIndicator";
import { TypingText } from "./TypingText";
import { AwaitingApprovalBlock, readFailureDiagnosis } from "./AwaitingApprovalBlock";
import { countDiffFiles, DiffViewer } from "./DiffViewer";
import { readKnowledgeSearchResult } from "../tasks/KnowledgeResultPanel";
import { readTaskPlanDocument } from "../tasks/PlanBreakdown";
import { readTaskReviewDocument } from "../tasks/ReviewBreakdown";
import type { EventRecord, TaskDetail } from "../../types";

export const FOLLOW_UP_MARKER = "\n\nFollow-up request:\n";

/** Pipeline-backed scenarios — only these get the inline 继续修复 affordance.
 *  process_question is a light chat answer and shouldn't show iteration UI. */
const PIPELINE_SCENARIOS = new Set(["jira_issue_develop", "jira_issue_plan", "jira_issue_writeback"]);

interface MessageListProps {
  task?: TaskDetail | null;
  tasks?: TaskDetail[];
  eventsMap?: Record<string, EventRecord[]>;
  /**
   * True only while the chat endpoint is actively streaming tokens for the
   * most recent turn. The blinking caret follows this prop instead of
   * task.status so it disappears the instant the stream ends — even if the
   * optimistic task object hasn't been replaced with the completed row yet.
   */
  streaming?: boolean;
  /** When this id matches a failed pipeline task in the list, that task's
   *  inline failure block shows '✓ 继续模式已开' instead of '继续修复'. */
  continueFromTaskId?: string | null;
  /** Toggle the continue-mode flag from inside the inline failure block. */
  onToggleContinueMode?: (taskId: string) => void;
  canCreate?: boolean;
}

const TERMINAL_STATUSES = new Set(["completed", "failed", "rolled_back"]);

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function readRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function readStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && Boolean(item.trim())) : [];
}

function extractDiffFence(text: string): string | null {
  const match = /```(?:diff|patch)\s*\n([\s\S]*?)```/i.exec(text);
  if (!match?.[1]?.trim()) {
    return null;
  }

  return match[1].trim();
}

function stripDiffFences(text: string): string {
  return text.replace(/```(?:diff|patch)\s*\n[\s\S]*?```/gi, "").replace(/\n{3,}/g, "\n\n").trim();
}

function readDevelopDiff(task: TaskDetail, fallbackText: string): { diff: string; filesChanged: number } | null {
  const result = readRecord(task.latest_result_json?.result);
  const resultScenario = readString(result?.scenario);
  const structuredDiff = readString(result?.diff);
  if ((task.scenario === "jira_issue_develop" || resultScenario === "jira_issue_develop") && structuredDiff) {
    const filesChanged = readStringArray(result?.files_changed).length || countDiffFiles(structuredDiff);
    return { diff: structuredDiff, filesChanged };
  }

  const fencedDiff = extractDiffFence(fallbackText);
  if (fencedDiff) {
    return { diff: fencedDiff, filesChanged: countDiffFiles(fencedDiff) };
  }

  return null;
}

function buildNaturalFailureReply(message: string | null): string | null {
  if (!message) {
    return null;
  }

  if (/^reviewer\b/i.test(message)) {
    return "I could not produce a grounded repository answer from the current indexed knowledge yet. Add a file path, class name, error log, or sync the knowledge source and try again.";
  }

  return message;
}

const JIRA_REJECT_HEADING = "## Jira transition rejected";

function extractJiraRejectionNotice(message: string | null): string | null {
  if (!message) return null;
  const idx = message.indexOf(JIRA_REJECT_HEADING);
  if (idx === -1) return null;
  const tail = message.slice(idx);
  const nextHeadingOffset = tail.slice(JIRA_REJECT_HEADING.length).search(/\n## /);
  const section =
    nextHeadingOffset === -1
      ? tail
      : tail.slice(0, JIRA_REJECT_HEADING.length + nextHeadingOffset);
  return section.trim();
}

export function readDisplayRequestText(requestText: string): string {
  const markerIndex = requestText.lastIndexOf(FOLLOW_UP_MARKER);
  if (markerIndex === -1) {
    return requestText;
  }
  return requestText.slice(markerIndex + FOLLOW_UP_MARKER.length).trim();
}

export function buildAgentReply(task: TaskDetail): string {
  // Streaming chat path: backend stores { kind: "chat_answer", answer: "..." }
  // directly on latest_result_json. Check this before the heavier knowledge
  // path. Returns the answer even when empty (still streaming) so the chat
  // doesn't briefly flash 'I could not produce a grounded repository answer'
  // from the process_question fallback below.
  const rawKind = task.latest_result_json?.kind;
  if (typeof rawKind === "string" && rawKind === "chat_answer") {
    const ans = task.latest_result_json?.answer;
    return typeof ans === "string" ? ans : "";
  }

  const result = task.latest_result_json?.result;
  const knowledgeResult = readKnowledgeSearchResult(result);
  if (knowledgeResult?.answer) {
    return knowledgeResult.answer;
  }

  const resultMessage = readString(task.latest_result_json?.message);
  const reviewSummary = readString(task.review_summary);
  const jiraRejection = extractJiraRejectionNotice(resultMessage);

  if (task.scenario === "process_question") {
    return (
      buildNaturalFailureReply(resultMessage ?? reviewSummary) ??
      "I could not produce a grounded repository answer from the current indexed knowledge yet."
    );
  }

  if (task.status === "failed" || task.review_verdict === "needs_info" || task.review_verdict === "rejected") {
    return resultMessage ?? reviewSummary ?? "I could not complete this request yet.";
  }

  const buildDevelopDetail = (): string | null => {
    const plan = readTaskPlanDocument(task.plan_json);
    if (plan) {
      const locations =
        plan.affected_code_locations.length > 0
          ? `\n\nWhere I would look first:\n${plan.affected_code_locations
              .slice(0, 4)
              .map((location) => `- ${location.source_name}:${location.relative_path}`)
              .join("\n")}`
          : "";
      const steps =
        plan.steps.length > 0
          ? `\n\nSuggested next steps:\n${plan.steps
              .slice(0, 5)
              .map((step, index) => `${index + 1}. ${step.title}`)
              .join("\n")}`
          : "";
      return `${plan.change_explanation}${locations}${steps}`;
    }

    const review = readTaskReviewDocument(task.review_json);
    if (review?.summary) {
      return review.summary;
    }

    if (resultMessage) {
      return resultMessage;
    }

    return null;
  };

  if (jiraRejection) {
    const detail = buildDevelopDetail();
    return detail ? `${jiraRejection}\n\n${detail}` : jiraRejection;
  }

  return buildDevelopDetail() ?? "I have received the request and am preparing the response.";
}

export function MessageList({
  task,
  tasks,
  eventsMap,
  streaming = false,
  continueFromTaskId = null,
  onToggleContinueMode,
  canCreate = true,
}: MessageListProps) {
  const visibleTasks = tasks ?? (task ? [task] : []);

  if (visibleTasks.length === 0) {
    return (
      <div className="empty-chat">
        <h1>Knowledge Assistant</h1>
        <p>你的智能知识管理与学习助手</p>
      </div>
    );
  }

  return (
    <div className="message-list">
      {visibleTasks.map((messageTask, index) => {
        const events = eventsMap?.[messageTask.id] ?? [];
        const latestEventType = events.length > 0 ? events[events.length - 1].event_type : null;
        const isActive = !TERMINAL_STATUSES.has(messageTask.status);
        const isLastTask = index === visibleTasks.length - 1;
        const justCompleted =
          messageTask.status === "completed" && Date.now() - new Date(messageTask.updated_at).getTime() < 30_000;
        const shouldAnimate = isLastTask && justCompleted;
        const agentReply = buildAgentReply(messageTask);
        const failureDiagnosis = readFailureDiagnosis(messageTask.latest_result_json);
        const developDiff = readDevelopDiff(messageTask, agentReply);
        const replyText = developDiff ? stripDiffFences(agentReply) : agentReply;
        return (
          <div className="message-turn" key={messageTask.id}>
            <article className="message-row user">
              <div className="message-bubble">
                <p>{readDisplayRequestText(messageTask.request_text)}</p>
              </div>
              <div className="message-avatar user-avatar">U</div>
            </article>

            {events.length ? <EventTimeline events={events} /> : null}

            <article className="message-row assistant">
              <div className="message-avatar assistant-avatar">AI</div>
              {isLastTask && isActive && !replyText ? (
                // Heavy task in flight, no streamed text yet → show pipeline spinner.
                <ThinkingIndicator status={messageTask.status} events={events} latestEventType={latestEventType} />
              ) : (
                <div className="message-bubble">
                  <div className="message-content">
                    {failureDiagnosis ? <AwaitingApprovalBlock diagnosis={failureDiagnosis} /> : null}
                    {replyText ? (
                      <>
                        {messageTask.scenario === "process_question" ? (
                          // Chat answer: render as markdown so **bold**, ##,
                          // numbered lists, code blocks, --- all display
                          // properly. ChatPage.submitMessage drives the
                          // typewriter pace by growing replyText char-by-char.
                          <MarkdownText text={replyText} />
                        ) : (
                          <TypingText text={replyText} enabled={shouldAnimate} />
                        )}
                        {/* Blinking caret only while the chat is actively
                            streaming — drives off the explicit `streaming`
                            prop, not task.status, to avoid timing races
                            after the stream ends. */}
                        {isLastTask && streaming ? <span className="streaming-caret" aria-hidden="true">▍</span> : null}
                      </>
                    ) : null}
                    {developDiff ? (
                      <details className="diff-details" open>
                        <summary>Code Changes ({developDiff.filesChanged} files)</summary>
                        <DiffViewer diff={developDiff.diff} />
                      </details>
                    ) : null}
                    {/* Inline 继续修复 affordance — only for failed PIPELINE
                        tasks, not chat-answer failures. Renders as part of
                        the assistant bubble so it sits in the conversation
                        flow (similar to the approval block) instead of being
                        a global horizontal bar at the page bottom. */}
                    {messageTask.status === "failed" &&
                    PIPELINE_SCENARIOS.has(messageTask.scenario || "") ? (
                      <FailedPipelineActionBlock
                        task={messageTask}
                        active={continueFromTaskId === messageTask.id}
                        canCreate={canCreate}
                        onToggle={() => onToggleContinueMode?.(messageTask.id)}
                      />
                    ) : null}
                  </div>
                </div>
              )}
            </article>
          </div>
        );
      })}
    </div>
  );
}


function FailedPipelineActionBlock({
  task,
  active,
  canCreate,
  onToggle,
}: {
  task: TaskDetail;
  active: boolean;
  canCreate: boolean;
  onToggle: () => void;
}) {
  const reasonRaw =
    (task.latest_result_json as { reason?: string; message?: string } | null)?.reason ??
    (task.latest_result_json as { message?: string } | null)?.message ??
    task.review_summary ??
    task.title ??
    "上次任务失败";
  const reason = String(reasonRaw).slice(0, 200);
  return (
    <section className="inline-failed-block" aria-label="task failed">
      <header className="inline-failed-block-head">
        <span className="inline-failed-block-icon" aria-hidden="true">!</span>
        <strong>任务执行失败</strong>
        <span className="inline-failed-block-tag">{task.scenario}</span>
      </header>
      <p className="inline-failed-block-reason">{reason}</p>
      <div className="inline-failed-block-actions">
        <button
          type="button"
          className={`inline-failed-block-btn ${active ? "active" : ""}`}
          onClick={onToggle}
          disabled={!canCreate}
          title="下一条消息会带上失败上下文一起发"
        >
          {active ? "✓ 继续模式已开" : "继续修复"}
        </button>
      </div>
    </section>
  );
}
