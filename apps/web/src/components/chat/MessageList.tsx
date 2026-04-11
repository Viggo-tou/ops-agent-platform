import { readKnowledgeSearchResult } from "../tasks/KnowledgeResultPanel";
import { readTaskPlanDocument } from "../tasks/PlanBreakdown";
import { readTaskReviewDocument } from "../tasks/ReviewBreakdown";
import type { TaskDetail } from "../../types";

export const FOLLOW_UP_MARKER = "\n\nFollow-up request:\n";

interface MessageListProps {
  task?: TaskDetail | null;
  tasks?: TaskDetail[];
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
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

export function readDisplayRequestText(requestText: string): string {
  const markerIndex = requestText.lastIndexOf(FOLLOW_UP_MARKER);
  if (markerIndex === -1) {
    return requestText;
  }
  return requestText.slice(markerIndex + FOLLOW_UP_MARKER.length).trim();
}

export function buildAgentReply(task: TaskDetail): string {
  const result = task.latest_result_json?.result;
  const knowledgeResult = readKnowledgeSearchResult(result);
  if (knowledgeResult?.answer) {
    return knowledgeResult.answer;
  }

  const resultMessage = readString(task.latest_result_json?.message);
  const reviewSummary = readString(task.review_summary);

  if (task.scenario === "process_question") {
    return (
      buildNaturalFailureReply(resultMessage ?? reviewSummary) ??
      "I could not produce a grounded repository answer from the current indexed knowledge yet."
    );
  }

  if (task.status === "failed" || task.review_verdict === "needs_info" || task.review_verdict === "rejected") {
    return resultMessage ?? reviewSummary ?? "I could not complete this request yet.";
  }

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

  return "I have received the request and am preparing the response.";
}

function renderMarkdownLite(text: string) {
  return text.split(/\n\s*\n/).map((block, index) => {
    const lines = block
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    if (lines.length > 0 && lines.every((line) => /^(-|\d+\.)\s+/.test(line))) {
      return (
        <ul className="message-list-block" key={`list-${index}`}>
          {lines.map((line) => (
            <li key={line}>{line.replace(/^(-|\d+\.)\s+/, "")}</li>
          ))}
        </ul>
      );
    }
    return <p key={`paragraph-${index}`}>{lines.join(" ")}</p>;
  });
}

export function MessageList({ task, tasks }: MessageListProps) {
  const visibleTasks = tasks ?? (task ? [task] : []);

  if (visibleTasks.length === 0) {
    return (
      <div className="empty-chat">
        <h1>Knowledge Assistant</h1>
        <p>
          Ask a question, inspect repository evidence, or plan the next task.
        </p>
      </div>
    );
  }

  return (
    <div className="message-list">
      {visibleTasks.map((messageTask) => {
        const agentReply = buildAgentReply(messageTask);
        return (
          <div className="message-turn" key={messageTask.id}>
            <article className="message-row user">
              <div className="message-bubble">
                <div className="message-author">You</div>
                <p>{readDisplayRequestText(messageTask.request_text)}</p>
              </div>
            </article>

            <article className="message-row assistant">
              <div className="message-bubble">
                <div className="message-author">Assistant</div>
                <div className="message-content">{renderMarkdownLite(agentReply)}</div>
              </div>
            </article>
          </div>
        );
      })}
    </div>
  );
}
