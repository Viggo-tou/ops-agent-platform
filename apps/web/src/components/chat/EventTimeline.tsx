import type { EventRecord } from "../../types";
import { ApprovalActions } from "./ApprovalActions";
import { AttemptHistoryChips } from "./AttemptHistoryChips";
import { DiffBlock } from "./DiffBlock";
import { ReservationsBlock } from "./ReservationsBlock";

interface EventTimelineProps {
  events: EventRecord[];
}

const hiddenEventTypes = new Set([
  "task_status_changed",
  "policy_evaluation_started",
  "policy_evaluation_completed",
  "final_response_emitted",
  "semantic_translation_failed",
  "approval_assigned",
  "approval_expired",
  "approval_cancelled",
  "guardrail_triggered",
]);

function readPayloadString(event: EventRecord, keys: string[]): string | null {
  const payload = event.payload_json;
  if (!payload) {
    return null;
  }

  for (const key of keys) {
    const value = payload[key];
    if (typeof value === "string" && value.trim()) {
      return value.trim();
    }
  }

  return null;
}

function readToolName(event: EventRecord): string {
  return readPayloadString(event, ["tool_name", "toolName", "name"]) ?? event.tool_name ?? "未知工具";
}

function readActionName(event: EventRecord): string {
  return (
    readPayloadString(event, ["action_name", "actionName", "action", "tool_name", "toolName"]) ??
    event.tool_name ??
    "待审批操作"
  );
}

function readDiff(event: EventRecord): string | null {
  return readPayloadString(event, ["diff", "patch", "patch_stats"]);
}

function readDiffSummary(event: EventRecord): string {
  const explicitSummary = readPayloadString(event, ["summary", "change_summary", "message"]);
  if (explicitSummary) {
    return explicitSummary;
  }
  if (event.tool_name === "sandbox.apply_patch") {
    return "查看已应用补丁";
  }
  if (event.tool_name === "codegen.generate_patch") {
    return "查看生成的代码 diff";
  }
  if (event.event_type === "execution_completed") {
    return "查看执行 diff";
  }
  return "查看 diff";
}

function readApprovalId(event: EventRecord): string | null {
  return readPayloadString(event, ["approval_id", "approvalId", "id"]);
}

function readReservations(event: EventRecord): string[] {
  const payload = event.payload_json;
  if (!payload) {
    return [];
  }
  const raw = (payload as Record<string, unknown>).reservations;
  if (!Array.isArray(raw)) {
    return [];
  }
  const cleaned: string[] = [];
  for (const item of raw) {
    if (typeof item === "string") {
      const trimmed = item.trim();
      if (trimmed) {
        cleaned.push(trimmed);
      }
    }
  }
  return cleaned;
}

interface AttemptHistoryEntry {
  provider: string;
  status: string;
  error?: string;
}

function readAttemptHistory(event: EventRecord): AttemptHistoryEntry[] {
  const payload = event.payload_json;
  if (!payload) {
    return [];
  }
  const raw = (payload as Record<string, unknown>).attempt_history;
  if (!Array.isArray(raw)) {
    return [];
  }
  const out: AttemptHistoryEntry[] = [];
  for (const item of raw) {
    if (item && typeof item === "object") {
      const rec = item as Record<string, unknown>;
      const provider = typeof rec.provider === "string" ? rec.provider : "";
      const status = typeof rec.status === "string" ? rec.status : "";
      if (!provider || !status) continue;
      const entry: AttemptHistoryEntry = { provider, status };
      if (typeof rec.error === "string" && rec.error.trim()) {
        entry.error = rec.error.trim();
      }
      out.push(entry);
    }
  }
  return out;
}

export function formatEventMessage(event: EventRecord): string | null {
  if (hiddenEventTypes.has(event.event_type)) {
    return null;
  }

  switch (event.event_type) {
    case "task_created":
      return "任务已创建";
    case "semantic_translation_started":
      return "正在理解请求…";
    case "semantic_translation_completed":
      return "请求解析完成";
    case "planning_started":
      return "正在生成执行计划…";
    case "plan_generated":
      return "执行计划已生成";
    case "review_started":
      return "正在审查计划…";
    case "review_passed":
      return "审查通过 ✓";
    case "review_failed":
      return "审查未通过 ✗";
    case "tool_call_requested":
      return `正在调用工具：${readToolName(event)}…`;
    case "tool_succeeded":
      return `工具调用成功：${readToolName(event)}`;
    case "tool_failed":
      return `工具调用失败：${readToolName(event)}`;
    case "tool_timed_out":
      return `工具调用超时：${readToolName(event)}`;
    case "approval_requested":
      return `等待审批：${readActionName(event)}`;
    case "approval_granted":
      return "审批已通过 ✓";
    case "approval_rejected":
      return "审批已拒绝 ✗";
    case "patch.applied":
      return "补丁已应用 ✓";
    case "execution_started":
      return "正在执行…";
    case "execution_completed":
      return "执行完成 ✓";
    case "execution_failed":
      return "执行失败 ✗";
    case "rollback_requested":
      return "正在回滚…";
    case "rollback_completed":
      return "回滚完成";
    case "knowledge_retrieved":
      return "知识检索完成";
    default:
      return null;
  }
}

export function EventTimeline({ events }: EventTimelineProps) {
  const visibleEvents = events
    .map((event) => ({ event, text: formatEventMessage(event) ?? (readDiff(event) ? "代码变更已生成" : null) }))
    .filter((item): item is { event: EventRecord; text: string } => item.text !== null);

  if (visibleEvents.length === 0) {
    return null;
  }

  return (
    <div className="event-timeline">
      {visibleEvents.map(({ event, text }) => (
        <div className="event-timeline-item" key={event.id}>
          <div className="event-timeline-main">
            <span className="event-timeline-text">{text}</span>
            {readDiff(event) ? <DiffBlock diff={readDiff(event)!} summary={readDiffSummary(event)} /> : null}
            {event.tool_name === "codegen.generate_patch" &&
            event.event_type === "tool_succeeded" &&
            readAttemptHistory(event).length > 0 ? (
              <AttemptHistoryChips attempts={readAttemptHistory(event)} />
            ) : null}
            {event.event_type === "approval_requested" && readReservations(event).length > 0 ? (
              <ReservationsBlock reservations={readReservations(event)} />
            ) : null}
            {event.event_type === "approval_requested" && readApprovalId(event) ? (
              <ApprovalActions
                approvalId={readApprovalId(event)!}
                actionName={readActionName(event)}
                taskId={event.task_id}
              />
            ) : null}
          </div>
          <span className="event-timeline-time">
            {new Date(event.created_at).toLocaleTimeString("zh-CN", {
              hour: "2-digit",
              minute: "2-digit",
              second: "2-digit",
            })}
          </span>
        </div>
      ))}
    </div>
  );
}
