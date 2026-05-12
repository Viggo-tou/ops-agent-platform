import { titleCase } from "../../lib/format";
import type {
  ReviewApprovalRequirement,
  ReviewFinding,
  ReviewFindingSeverity,
  ReviewPolicyCheck,
  ReviewPolicyStatus,
  TaskReviewDocument,
} from "../../types";

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : null;
}

function readString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function readBoolean(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function readStringList(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }

  return value.flatMap((item) => (typeof item === "string" ? [item] : []));
}

function readFinding(value: unknown): ReviewFinding | null {
  const record = asRecord(value);
  if (!record) {
    return null;
  }

  const code = readString(record.code);
  const severity = readString(record.severity) as ReviewFindingSeverity | null;
  const message = readString(record.message);
  if (!code || !severity || !message) {
    return null;
  }

  return {
    code,
    severity,
    message,
    step_id: readString(record.step_id),
    field: readString(record.field),
  };
}

function readPolicyCheck(value: unknown): ReviewPolicyCheck | null {
  const record = asRecord(value);
  if (!record) {
    return null;
  }

  const name = readString(record.name);
  const status = readString(record.status) as ReviewPolicyStatus | null;
  const detail = readString(record.detail);
  if (!name || !status || !detail) {
    return null;
  }

  return { name, status, detail };
}

function readApprovalRequirement(value: unknown): ReviewApprovalRequirement | null {
  const record = asRecord(value);
  if (!record) {
    return null;
  }

  const actionName = readString(record.action_name);
  const reason = readString(record.reason);
  const approverRole = readString(record.approver_role);
  if (!actionName || !reason || !approverRole) {
    return null;
  }

  return {
    action_name: actionName,
    reason,
    approver_role: approverRole,
  };
}

export function readTaskReviewDocument(reviewJson: Record<string, unknown> | null): TaskReviewDocument | null {
  if (!reviewJson) {
    return null;
  }

  const schemaVersion = readString(reviewJson.schema_version);
  const reviewId = readString(reviewJson.review_id);
  const taskId = readString(reviewJson.task_id);
  const planId = readString(reviewJson.plan_id);
  const reviewStage = readString(reviewJson.review_stage);
  const verdict = readString(reviewJson.verdict);
  const summary = readString(reviewJson.summary);
  const recommendedStatus = readString(reviewJson.recommended_status);
  const readyForExecution = readBoolean(reviewJson.ready_for_execution);

  if (
    !schemaVersion ||
    !reviewId ||
    !taskId ||
    !planId ||
    !reviewStage ||
    !verdict ||
    !summary ||
    !recommendedStatus ||
    readyForExecution === null
  ) {
    return null;
  }

  const findings = Array.isArray(reviewJson.findings)
    ? reviewJson.findings.flatMap((finding) => {
        const parsed = readFinding(finding);
        return parsed ? [parsed] : [];
      })
    : [];

  const policyChecks = Array.isArray(reviewJson.policy_checks)
    ? reviewJson.policy_checks.flatMap((check) => {
        const parsed = readPolicyCheck(check);
        return parsed ? [parsed] : [];
      })
    : [];

  const approvalRequirements = Array.isArray(reviewJson.approval_requirements)
    ? reviewJson.approval_requirements.flatMap((requirement) => {
        const parsed = readApprovalRequirement(requirement);
        return parsed ? [parsed] : [];
      })
    : [];

  return {
    schema_version: schemaVersion,
    review_id: reviewId,
    task_id: taskId,
    plan_id: planId,
    review_stage: reviewStage,
    verdict,
    ready_for_execution: readyForExecution,
    summary,
    findings,
    missing_information: readStringList(reviewJson.missing_information),
    policy_checks: policyChecks,
    approval_requirements: approvalRequirements,
    recommended_status: recommendedStatus,
    provider: asRecord(reviewJson.provider),
  };
}

interface ReviewBreakdownProps {
  review: TaskReviewDocument | null;
  rawReviewJson: Record<string, unknown> | null;
}

export function ReviewBreakdown({ review, rawReviewJson }: ReviewBreakdownProps) {
  if (!review && !rawReviewJson) {
    return <p>No review has been recorded yet.</p>;
  }

  if (!review && rawReviewJson) {
    return (
      <div className="stack-sm">
        <p>Review data is present but could not be parsed into the current dashboard structure.</p>
        <pre className="json-panel">{JSON.stringify(rawReviewJson, null, 2)}</pre>
      </div>
    );
  }

  if (!review) {
    return null;
  }

  return (
    <div className="stack-sm">
      <div className="review-metadata-grid">
        <div className="review-metric">
          <span>Ready For Execution</span>
          <strong>{review.ready_for_execution ? "Yes" : "No"}</strong>
        </div>
        <div className="review-metric">
          <span>Recommended Status</span>
          <strong>{titleCase(review.recommended_status)}</strong>
        </div>
        <div className="review-metric">
          <span>Schema</span>
          <strong>{review.schema_version}</strong>
        </div>
        <div className="review-metric">
          <span>Review ID</span>
          <strong>{review.review_id}</strong>
        </div>
      </div>

      {review.missing_information.length > 0 ? (
        <div className="warning-banner">
          <strong>Missing Information</strong>
          <ul className="plain-list">
            {review.missing_information.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <section className="review-section">
        <div className="section-header">
          <div>
            <div className="eyebrow">Review Findings</div>
            <h3>Findings</h3>
          </div>
          <span className="mini-pill">{review.findings.length}</span>
        </div>

        {review.findings.length > 0 ? (
          <div className="review-list">
            {review.findings.map((finding) => (
              <article className="review-item" key={`${finding.code}-${finding.step_id ?? "global"}`}>
                <div className="review-item-head">
                  <span className={`mini-pill finding-${finding.severity}`}>{finding.severity}</span>
                  <strong>{finding.code}</strong>
                </div>
                <p>{finding.message}</p>
                {finding.step_id ? <div className="muted-text">Step: {finding.step_id}</div> : null}
                {finding.field ? <div className="muted-text">Field: {finding.field}</div> : null}
              </article>
            ))}
          </div>
        ) : (
          <p>No reviewer findings were recorded.</p>
        )}
      </section>

      <section className="review-section">
        <div className="section-header">
          <div>
            <div className="eyebrow">Policy Checks</div>
            <h3>Checks</h3>
          </div>
          <span className="mini-pill">{review.policy_checks.length}</span>
        </div>

        {review.policy_checks.length > 0 ? (
          <div className="review-list">
            {review.policy_checks.map((check) => (
              <article className="review-item" key={check.name}>
                <div className="review-item-head">
                  <span className={`mini-pill policy-${check.status}`}>{check.status}</span>
                  <strong>{check.name}</strong>
                </div>
                <p>{check.detail}</p>
              </article>
            ))}
          </div>
        ) : (
          <p>No explicit policy checks were recorded.</p>
        )}
      </section>

      <section className="review-section">
        <div className="section-header">
          <div>
            <div className="eyebrow">Approval Requirements</div>
            <h3>Requirements</h3>
          </div>
          <span className="mini-pill">{review.approval_requirements.length}</span>
        </div>

        {review.approval_requirements.length > 0 ? (
          <div className="review-list">
            {review.approval_requirements.map((requirement) => (
              <article className="review-item" key={`${requirement.action_name}-${requirement.approver_role}`}>
                <div className="review-item-head">
                  <strong>{requirement.action_name}</strong>
                  <span className="mini-pill">{titleCase(requirement.approver_role)}</span>
                </div>
                <p>{requirement.reason}</p>
              </article>
            ))}
          </div>
        ) : (
          <p>No approval requirements were recorded for this review.</p>
        )}
      </section>

      <details className="review-debug">
        <summary>Raw Review JSON</summary>
        <pre className="json-panel">{JSON.stringify(rawReviewJson, null, 2)}</pre>
      </details>
    </div>
  );
}
