import { formatDateTime } from "../../lib/format";
import type { EventRecord as EventRead } from "../../types";

export type GateVerdict = "pass" | "fail" | "skipped" | "running";

export interface GateStatus {
  id:
    | "compile_gate"
    | "runtime_validation"
    | "semantic_review"
    | "diff_reviewer"
    | "spec_conformance"
    | "goal_attestation";
  label: string;
  verdict: GateVerdict;
  attempts: number;
  latestMessage: string | null;
  latestAt: string | null;
}

interface GateDefinition {
  id: GateStatus["id"];
  label: string;
  prefix: string;
}

interface GateStatusPanelProps {
  events: EventRead[];
}

const GATES: GateDefinition[] = [
  { id: "compile_gate", label: "Compile Gate", prefix: "compile_gate." },
  { id: "runtime_validation", label: "Runtime Validation", prefix: "runtime_validation." },
  { id: "semantic_review", label: "Semantic Review", prefix: "semantic_review." },
  { id: "diff_reviewer", label: "Diff Reviewer", prefix: "diff_reviewer." },
  { id: "spec_conformance", label: "Spec Conformance", prefix: "spec_conformance." },
  { id: "goal_attestation", label: "Goal Attestation", prefix: "goal_decomposition." },
];

const TERMINAL_EVENT_TYPES = new Set(["tool_succeeded", "tool_failed"]);

function readToolName(event: EventRead): string | null {
  const payloadToolName = event.payload_json?.tool_name;
  if (typeof payloadToolName === "string") {
    return payloadToolName;
  }
  return event.tool_name;
}

function sortChronologically(events: EventRead[]): EventRead[] {
  return events
    .map((event, index) => ({ event, index }))
    .sort((left, right) => {
      const leftTime = Date.parse(left.event.created_at);
      const rightTime = Date.parse(right.event.created_at);
      const normalizedLeft = Number.isNaN(leftTime) ? 0 : leftTime;
      const normalizedRight = Number.isNaN(rightTime) ? 0 : rightTime;
      return normalizedLeft - normalizedRight || left.index - right.index;
    })
    .map(({ event }) => event);
}

function truncateDetail(value: string): string {
  return value.length > 200 ? `${value.slice(0, 197)}...` : value;
}

export function resolveGateStatuses(events: EventRead[]): GateStatus[] {
  const chronologicalEvents = sortChronologically(events);

  return GATES.map((gate) => {
    let attempts = 0;
    let matchedEvents = 0;
    let latestMessage: string | null = null;
    let latestAt: string | null = null;
    let latestTerminalType: string | null = null;

    for (const event of chronologicalEvents) {
      const toolName = readToolName(event);
      if (!toolName?.startsWith(gate.prefix)) {
        continue;
      }

      matchedEvents += 1;
      latestMessage = event.message || null;
      latestAt = event.created_at;

      const eventType = event.event_type.toLowerCase();
      if (TERMINAL_EVENT_TYPES.has(eventType)) {
        attempts += 1;
        latestTerminalType = eventType;
      }
    }

    let verdict: GateVerdict = "skipped";
    if (latestTerminalType === "tool_succeeded") {
      verdict = "pass";
    } else if (latestTerminalType === "tool_failed") {
      verdict = "fail";
    } else if (matchedEvents > 0) {
      verdict = "running";
    }

    return {
      id: gate.id,
      label: gate.label,
      verdict,
      attempts,
      latestMessage,
      latestAt,
    };
  });
}

export function GateStatusPanel({ events }: GateStatusPanelProps) {
  const statuses = resolveGateStatuses(events);
  const hasTerminalResult = statuses.some((status) => status.verdict === "pass" || status.verdict === "fail");
  const failingStatuses = statuses.filter((status) => status.verdict === "fail");

  return (
    <section className="detail-card gate-panel">
      <div className="section-header">
        <div>
          <div className="eyebrow">Develop Pipeline</div>
          <h3>Pipeline Gates</h3>
          <p>Latest develop-pipeline gate results from the task event stream.</p>
        </div>
      </div>

      {!hasTerminalResult ? (
        <p className="muted-text">Develop pipeline has not produced gate results yet.</p>
      ) : (
        <>
          <div className="gate-strip" aria-label="Pipeline gate statuses">
            {statuses.map((status, index) => (
              <article className={`gate-card gate-card--${status.verdict}`} key={status.id}>
                <div className="gate-card-head">
                  <span className="gate-number">{index + 1}</span>
                  <strong>{status.label}</strong>
                </div>
                <div className="gate-card-meta">
                  <span className="gate-verdict">{status.verdict}</span>
                  {status.attempts > 1 ? <span className="gate-attempts">x{status.attempts}</span> : null}
                </div>
              </article>
            ))}
          </div>

          {failingStatuses.length > 0 ? (
            <div className="gate-failures">
              {failingStatuses.map((status) => (
                <article className="gate-failure-row" key={status.id}>
                  <div>
                    <strong>{status.label}</strong>
                    <span className="muted-text">{formatDateTime(status.latestAt)}</span>
                  </div>
                  {status.latestMessage ? (
                    <p title={status.latestMessage}>{truncateDetail(status.latestMessage)}</p>
                  ) : (
                    <p>No failure detail was recorded.</p>
                  )}
                </article>
              ))}
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}
