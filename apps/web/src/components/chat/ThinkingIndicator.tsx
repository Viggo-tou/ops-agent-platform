import type { EventRecord } from "../../types";

interface ThinkingIndicatorProps {
  status: string;
  events: EventRecord[];
  latestEventType: string | null;
}

function readToolName(event: EventRecord | null): string {
  if (!event) {
    return "";
  }

  const payloadToolName = event.payload_json?.tool_name ?? event.payload_json?.toolName ?? event.payload_json?.name;
  if (typeof payloadToolName === "string") {
    return payloadToolName.toLowerCase();
  }

  return (event.tool_name ?? "").toLowerCase();
}

function deriveThinkingMessage(status: string, events: EventRecord[], latestEventType: string | null): string {
  const latestEvent = events.length > 0 ? events[events.length - 1] : null;

  if (latestEventType === "tool_call_requested") {
    const toolName = readToolName(latestEvent);
    if (toolName.includes("codegen")) {
      return "正在生成代码…";
    }
    if (toolName.includes("jira")) {
      return "正在读取 Jira…";
    }
    if (toolName.includes("sandbox")) {
      return "正在应用补丁…";
    }
    if (toolName.includes("test")) {
      return "正在运行测试…";
    }
    if (toolName.includes("diff_reviewer")) {
      return "正在审查代码…";
    }
  }

  switch (latestEventType) {
    case "task_created":
    case "semantic_translation_started":
      return "正在思考…";
    case "semantic_translation_completed":
    case "planning_started":
      return "正在分析需求…";
    case "plan_generated":
    case "review_started":
      return "正在审查计划…";
    case "review_passed":
    case "execution_started":
      return "正在执行…";
    case "approval_requested":
      return "等待审批中…";
    default:
      return status ? "处理中…" : "正在思考…";
  }
}

export function ThinkingIndicator({ status, events, latestEventType }: ThinkingIndicatorProps) {
  const message = deriveThinkingMessage(status, events, latestEventType);

  return (
    <div className="thinking-indicator">
      <span className="thinking-dot" />
      <span className="thinking-text">{message}</span>
    </div>
  );
}
