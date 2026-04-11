import type { Approval } from "../../types";
import { formatDateTime, titleCase } from "../../lib/format";

interface ApprovalPanelProps {
  approvals: Approval[];
  isSubmitting: boolean;
  actionNotes: string;
  onActionNotesChange: (value: string) => void;
  onGrant: (approvalId: string) => void;
  onReject: (approvalId: string) => void;
}

export function ApprovalPanel({
  approvals,
  isSubmitting,
  actionNotes,
  onActionNotesChange,
  onGrant,
  onReject,
}: ApprovalPanelProps) {
  if (approvals.length === 0) {
    return (
      <div className="empty-panel">
        <h3>No approvals attached</h3>
        <p>This task has no approval placeholders yet.</p>
      </div>
    );
  }

  return (
    <div className="stack">
      {approvals.map((approval) => {
        const isPending = approval.status === "pending";

        return (
          <section className="detail-card" key={approval.id}>
            <div className="detail-header">
              <div>
                <h3>{approval.action_name}</h3>
                <p>{approval.reason}</p>
              </div>
              <span className={`pill approval-${approval.status}`}>
                {titleCase(approval.status)}
              </span>
            </div>

            <dl className="metadata-grid compact">
              <div>
                <dt>Requested By</dt>
                <dd>{titleCase(approval.requested_by_role)}</dd>
              </div>
              <div>
                <dt>Approver Role</dt>
                <dd>{titleCase(approval.approver_role)}</dd>
              </div>
              <div>
                <dt>Requested At</dt>
                <dd>{formatDateTime(approval.requested_at)}</dd>
              </div>
              <div>
                <dt>Decided At</dt>
                <dd>{formatDateTime(approval.decided_at)}</dd>
              </div>
            </dl>

            {approval.request_payload_json ? (
              <pre className="json-panel">{JSON.stringify(approval.request_payload_json, null, 2)}</pre>
            ) : null}

            {isPending ? (
              <div className="approval-actions">
                <textarea
                  className="text-area compact-text-area"
                  value={actionNotes}
                  onChange={(event) => onActionNotesChange(event.target.value)}
                  placeholder="Optional approval note"
                />
                <div className="button-row">
                  <button className="button primary" onClick={() => onGrant(approval.id)} disabled={isSubmitting}>
                    {isSubmitting ? "Processing..." : "Grant Approval"}
                  </button>
                  <button className="button ghost" onClick={() => onReject(approval.id)} disabled={isSubmitting}>
                    Reject Approval
                  </button>
                </div>
              </div>
            ) : approval.decision_payload_json ? (
              <pre className="json-panel">{JSON.stringify(approval.decision_payload_json, null, 2)}</pre>
            ) : null}
          </section>
        );
      })}
    </div>
  );
}
