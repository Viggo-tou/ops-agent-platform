import { formatDateTime, titleCase } from "../../lib/format";
import type { ToolExecutionRecord } from "../../types";

function toolExecutionStatusClass(status: ToolExecutionRecord["status"]): string {
  if (status === "succeeded") {
    return "tool-status-succeeded";
  }
  if (status === "timed_out") {
    return "tool-status-timed_out";
  }
  if (status === "failed") {
    return "tool-status-failed";
  }
  return "tool-status-running";
}

interface ToolExecutionPanelProps {
  executions: ToolExecutionRecord[];
}

export function ToolExecutionPanel({ executions }: ToolExecutionPanelProps) {
  return (
    <div className="review-section">
      <div className="section-header">
        <div>
          <div className="eyebrow">Tool Runtime</div>
          <h3>Execution Logs</h3>
        </div>
        <span className="mini-pill">{executions.length}</span>
      </div>

      {executions.length > 0 ? (
        <div className="review-list">
          {executions.map((execution) => (
            <article className="review-item" key={execution.id}>
              <div className="review-item-head">
                <strong>{execution.tool_name}</strong>
                <span className={`mini-pill ${toolExecutionStatusClass(execution.status)}`}>
                  {titleCase(execution.status)}
                </span>
              </div>

              <div className="metadata-grid compact">
                <div>
                  <dt>Provider</dt>
                  <dd>{execution.provider_name}</dd>
                </div>
                <div>
                  <dt>Permission</dt>
                  <dd>{titleCase(execution.permission_category)}</dd>
                </div>
                <div>
                  <dt>Attempts</dt>
                  <dd>
                    {execution.attempt_count} / {execution.max_retries + 1}
                  </dd>
                </div>
                <div>
                  <dt>Timeout</dt>
                  <dd>{execution.timeout_seconds}s</dd>
                </div>
                <div>
                  <dt>Started</dt>
                  <dd>{formatDateTime(execution.started_at)}</dd>
                </div>
                <div>
                  <dt>Finished</dt>
                  <dd>{formatDateTime(execution.finished_at)}</dd>
                </div>
                <div>
                  <dt>Duration</dt>
                  <dd>{execution.duration_ms !== null ? `${execution.duration_ms} ms` : "N/A"}</dd>
                </div>
                <div>
                  <dt>Actor</dt>
                  <dd>{execution.actor_name ?? "N/A"}</dd>
                </div>
              </div>

              {execution.error_message ? <div className="error-banner">{execution.error_message}</div> : null}

              <details className="collapsible-panel">
                <summary className="collapsible-summary">
                  <div>
                    <div className="eyebrow">Payload</div>
                    <strong>Request and Response</strong>
                  </div>
                  <span className="mini-pill">JSON</span>
                </summary>

                <div className="collapsible-content stack-sm">
                  <div>
                    <div className="eyebrow">Request</div>
                    <pre className="json-panel">{JSON.stringify(execution.request_payload_json, null, 2)}</pre>
                  </div>
                  <div>
                    <div className="eyebrow">Response</div>
                    <pre className="json-panel">{JSON.stringify(execution.response_payload_json, null, 2)}</pre>
                  </div>
                </div>
              </details>

              {execution.attempt_log_json && execution.attempt_log_json.length > 0 ? (
                <details className="collapsible-panel">
                  <summary className="collapsible-summary">
                    <div>
                      <div className="eyebrow">Retry Detail</div>
                      <strong>Attempt Log</strong>
                    </div>
                    <span className="mini-pill">{execution.attempt_log_json.length}</span>
                  </summary>

                  <div className="collapsible-content">
                    <pre className="json-panel">{JSON.stringify(execution.attempt_log_json, null, 2)}</pre>
                  </div>
                </details>
              ) : null}
            </article>
          ))}
        </div>
      ) : (
        <p>No tool executions have been recorded for this task yet.</p>
      )}
    </div>
  );
}
