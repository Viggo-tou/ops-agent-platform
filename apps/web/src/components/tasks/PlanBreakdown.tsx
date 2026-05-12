import type { TaskPlanDocument } from "../../types";
import { titleCase } from "../../lib/format";

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function readStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((item): item is string => typeof item === "string" && item.trim().length > 0);
}

export function readTaskPlanDocument(payload: Record<string, unknown> | null): TaskPlanDocument | null {
  if (!payload) {
    return null;
  }

  const schemaVersion = readString(payload.schema_version);
  const planId = readString(payload.plan_id);
  const taskId = readString(payload.task_id);
  const objective = readString(payload.objective);
  const requestSummary = readString(payload.request_summary);
  const scenario = readString(payload.scenario);
  const changeSummary = readString(payload.change_summary);
  const changeExplanation = readString(payload.change_explanation);
  const riskLevel = readString(payload.risk_level);

  if (
    !schemaVersion ||
    !planId ||
    !taskId ||
    !objective ||
    !requestSummary ||
    !scenario ||
    !changeSummary ||
    !changeExplanation ||
    !riskLevel
  ) {
    return null;
  }

  const affectedCodeLocations = Array.isArray(payload.affected_code_locations)
    ? payload.affected_code_locations.flatMap((location) => {
        if (!location || typeof location !== "object") {
          return [];
        }
        const typedLocation = location as Record<string, unknown>;
        const sourceName = readString(typedLocation.source_name);
        const relativePath = readString(typedLocation.relative_path);
        const reason = readString(typedLocation.reason);
        if (!sourceName || !relativePath || !reason) {
          return [];
        }
        return [
          {
            source_name: sourceName,
            relative_path: relativePath,
            reason,
            line_start: typeof typedLocation.line_start === "number" ? typedLocation.line_start : null,
            line_end: typeof typedLocation.line_end === "number" ? typedLocation.line_end : null,
          },
        ];
      })
    : [];

  const tools = Array.isArray(payload.tools)
    ? payload.tools.flatMap((tool) => {
        if (!tool || typeof tool !== "object") {
          return [];
        }
        const typedTool = tool as Record<string, unknown>;
        const toolName = readString(typedTool.tool_name);
        const permissionCategory = readString(typedTool.permission_category);
        const purpose = readString(typedTool.purpose);
        if (!toolName || !permissionCategory || !purpose) {
          return [];
        }
        return [
          {
            tool_name: toolName,
            permission_category: permissionCategory as TaskPlanDocument["tools"][number]["permission_category"],
            purpose,
          },
        ];
      })
    : [];

  const steps = Array.isArray(payload.steps)
    ? payload.steps.flatMap((step) => {
        if (!step || typeof step !== "object") {
          return [];
        }
        const typedStep = step as Record<string, unknown>;
        const stepId = readString(typedStep.step_id);
        const title = readString(typedStep.title);
        const kind = readString(typedStep.kind);
        const ownerRole = readString(typedStep.owner_role);
        const expectedOutput = readString(typedStep.expected_output);
        const successCriteria = readString(typedStep.success_criteria);
        if (!stepId || !title || !kind || !ownerRole || !expectedOutput || !successCriteria) {
          return [];
        }
        return [
          {
            step_id: stepId,
            title,
            kind: kind as TaskPlanDocument["steps"][number]["kind"],
            owner_role: ownerRole as TaskPlanDocument["steps"][number]["owner_role"],
            depends_on: readStringArray(typedStep.depends_on),
            tool_name: readString(typedStep.tool_name),
            expected_output: expectedOutput,
            success_criteria: successCriteria,
          },
        ];
      })
    : [];

  const finalOutputContract =
    payload.final_output_contract && typeof payload.final_output_contract === "object"
      ? (payload.final_output_contract as Record<string, unknown>)
      : null;

  return {
    schema_version: schemaVersion,
    plan_id: planId,
    task_id: taskId,
    objective,
    request_summary: requestSummary,
    scenario,
    change_summary: changeSummary,
    change_explanation: changeExplanation,
    assumptions: readStringArray(payload.assumptions),
    missing_information: readStringArray(payload.missing_information),
    risk_level: riskLevel as TaskPlanDocument["risk_level"],
    requires_approval: Boolean(payload.requires_approval),
    approval_reasons: readStringArray(payload.approval_reasons),
    affected_code_locations: affectedCodeLocations,
    tools,
    steps,
    final_output_contract: {
      type: readString(finalOutputContract?.type) ?? "unknown",
      required_fields: readStringArray(finalOutputContract?.required_fields),
    },
    provider:
      payload.provider && typeof payload.provider === "object"
        ? (payload.provider as Record<string, unknown>)
        : null,
  };
}

interface PlanBreakdownProps {
  plan: TaskPlanDocument | null;
  rawPlanJson: Record<string, unknown> | null;
}

export function PlanBreakdown({ plan, rawPlanJson }: PlanBreakdownProps) {
  if (!plan && !rawPlanJson) {
    return <p>No plan has been recorded.</p>;
  }

  if (!plan && rawPlanJson) {
    return (
      <div className="stack tight-stack">
        <p>Plan data is present but could not be parsed into the current dashboard structure.</p>
        <pre className="json-panel">{JSON.stringify(rawPlanJson, null, 2)}</pre>
      </div>
    );
  }

  if (!plan) {
    return null;
  }

  return (
    <div className="stack tight-stack">
      <div className="translation-summary-card">
        <span>What Should Change</span>
        <strong>{plan.change_summary}</strong>
      </div>

      <p className="plan-explanation">{plan.change_explanation}</p>

      {plan.affected_code_locations.length > 0 ? (
        <div className="stack tight-stack">
          <span className="detail-subheading">Likely Source Code</span>
          <div className="review-list">
            {plan.affected_code_locations.map((location) => (
              <article
                key={`${location.source_name}:${location.relative_path}:${location.line_start ?? 0}`}
                className="review-item"
              >
                <div className="review-item-head">
                  <strong>
                    {location.source_name}:{location.relative_path}
                  </strong>
                  {(location.line_start ?? location.line_end) !== null ? (
                    <span className="mini-pill">
                      lines {location.line_start ?? "?"}-{location.line_end ?? "?"}
                    </span>
                  ) : null}
                </div>
                <p>{location.reason}</p>
              </article>
            ))}
          </div>
        </div>
      ) : null}

      {plan.steps.length > 0 ? (
        <div className="stack tight-stack">
          <span className="detail-subheading">Change Plan</span>
          <ol className="step-list">
            {plan.steps.map((step) => (
              <li key={step.step_id}>
                <span className="mini-pill">{titleCase(step.owner_role)}</span>
                <strong>{step.title}</strong>
                <div className="muted-text">{step.expected_output}</div>
                <div className="muted-text">Success: {step.success_criteria}</div>
              </li>
            ))}
          </ol>
        </div>
      ) : null}

      {plan.assumptions.length > 0 ? (
        <div className="stack tight-stack">
          <span className="detail-subheading">Assumptions</span>
          <ul className="detail-list">
            {plan.assumptions.map((assumption) => (
              <li key={assumption}>{assumption}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {plan.missing_information.length > 0 ? (
        <div className="warning-banner">
          Missing information: {plan.missing_information.join(", ")}
        </div>
      ) : null}

      {rawPlanJson ? (
        <details className="details-panel">
          <summary>Raw Plan JSON</summary>
          <pre className="json-panel">{JSON.stringify(rawPlanJson, null, 2)}</pre>
        </details>
      ) : null}
    </div>
  );
}
