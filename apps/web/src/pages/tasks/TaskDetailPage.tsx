import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { useState } from "react";

import { ApprovalPanel } from "../../components/tasks/ApprovalPanel";
import { GateStatusPanel } from "../../components/tasks/GateStatusPanel";
import { KnowledgeResultPanel, readKnowledgeSearchResult } from "../../components/tasks/KnowledgeResultPanel";
import { PlanBreakdown, readTaskPlanDocument } from "../../components/tasks/PlanBreakdown";
import { PlanProviderBadge } from "../../components/tasks/PlanProviderBadge";
import { ReviewBreakdown, readTaskReviewDocument } from "../../components/tasks/ReviewBreakdown";
import { ReviewVerdictBadge } from "../../components/tasks/ReviewVerdictBadge";
import {
  SemanticTranslationPanel,
  readSemanticTranslationDocument,
} from "../../components/tasks/SemanticTranslationPanel";
import { ToolExecutionPanel } from "../../components/tasks/ToolExecutionPanel";
import { RiskBadge, TaskStatusBadge } from "../../components/tasks/TaskStatusBadge";
import { TaskTimeline } from "../../components/tasks/TaskTimeline";
import { api } from "../../lib/api";
import { formatDateTime, formatSyncTime, titleCase, toErrorMessage } from "../../lib/format";
import { useTaskStream } from "../../lib/useTaskStream";

function readPlanSummary(plan: Record<string, unknown> | null): string | null {
  if (!plan) {
    return null;
  }

  const changeSummary = typeof plan.change_summary === "string" ? plan.change_summary : null;
  const objective = typeof plan.objective === "string" ? plan.objective : null;
  const requestSummary = typeof plan.request_summary === "string" ? plan.request_summary : null;
  return changeSummary ?? objective ?? requestSummary;
}

export function TaskDetailPage() {
  const { taskId } = useParams();
  const queryClient = useQueryClient();
  const [actionNotes, setActionNotes] = useState("");
  const [rollbackReason, setRollbackReason] = useState("Reset task state for demo follow-up.");

  // SSE: drives invalidations for the queries below; falls back to interval
  // polling when the stream isn't live yet (or has finished).
  const stream = useTaskStream(taskId);
  const pollInterval = stream.connected && !stream.done ? false : 5_000;

  const taskQuery = useQuery({
    queryKey: ["task", taskId],
    queryFn: () => api.getTask(taskId!),
    enabled: Boolean(taskId),
    refetchInterval: pollInterval,
  });

  const eventsQuery = useQuery({
    queryKey: ["task-events", taskId],
    queryFn: () => api.getTaskEvents(taskId!),
    enabled: Boolean(taskId),
    refetchInterval: pollInterval,
  });

  const toolExecutionsQuery = useQuery({
    queryKey: ["task-tool-executions", taskId],
    queryFn: () => api.getTaskToolExecutions(taskId!),
    enabled: Boolean(taskId),
    refetchInterval: pollInterval,
  });

  const refreshTaskViews = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["tasks"] }),
      queryClient.invalidateQueries({ queryKey: ["task", taskId] }),
      queryClient.invalidateQueries({ queryKey: ["task-events", taskId] }),
      queryClient.invalidateQueries({ queryKey: ["task-tool-executions", taskId] }),
    ]);
  };

  const grantMutation = useMutation({
    mutationFn: (approvalId: string) => api.grantApproval(approvalId, "team_lead", actionNotes || undefined),
    onSuccess: refreshTaskViews,
  });

  const rejectMutation = useMutation({
    mutationFn: (approvalId: string) => api.rejectApproval(approvalId, "team_lead", actionNotes || undefined),
    onSuccess: refreshTaskViews,
  });

  const rollbackMutation = useMutation({
    mutationFn: () => api.rollbackTask(taskId!, "operator", rollbackReason),
    onSuccess: refreshTaskViews,
  });

  if (!taskId) {
    return <div className="error-banner">Task id is missing from the route.</div>;
  }

  if (taskQuery.isLoading || eventsQuery.isLoading || toolExecutionsQuery.isLoading) {
    return <div className="loading-panel">Loading task console...</div>;
  }

  if (taskQuery.isError) {
    return <div className="error-banner">{toErrorMessage(taskQuery.error)}</div>;
  }

  if (eventsQuery.isError) {
    return <div className="error-banner">{toErrorMessage(eventsQuery.error)}</div>;
  }

  if (toolExecutionsQuery.isError) {
    return <div className="error-banner">{toErrorMessage(toolExecutionsQuery.error)}</div>;
  }

  const task = taskQuery.data;
  const events = eventsQuery.data ?? [];
  const toolExecutions = toolExecutionsQuery.data ?? [];

  if (!task) {
    return <div className="error-banner">Task not found.</div>;
  }

  const planSummary = readPlanSummary(task.plan_json);
  const planDocument = readTaskPlanDocument(task.plan_json);
  const pendingApproval = task.approvals.some((approval) => approval.status === "pending");
  const reviewDocument = readTaskReviewDocument(task.review_json);
  const translationDocument = readSemanticTranslationDocument(task.translation_json);
  const structuredResult = task.latest_result_json?.result;
  const knowledgeResult = readKnowledgeSearchResult(structuredResult);

  return (
    <div className="stack">
      <section className="page-header-card">
        <div>
          <div className="eyebrow">Task Detail</div>
          <h2>{task.title}</h2>
          <p>{task.request_text}</p>
        </div>
        <div className="button-row">
          <LiveBadge stream={stream} />
          <div className="live-hint">{formatSyncTime(Math.max(taskQuery.dataUpdatedAt, eventsQuery.dataUpdatedAt))}</div>
          <Link to="/tasks" className="button ghost link-button">
            Back to Tasks
          </Link>
          <button className="button ghost" onClick={() => refreshTaskViews()}>
            Refresh
          </button>
        </div>
      </section>

      <section className="metadata-band">
        <div className="metric-card">
          <span>Status</span>
          <TaskStatusBadge status={task.status} />
        </div>
        <div className="metric-card">
          <span>Stage</span>
          <strong>{titleCase(task.workflow_stage)}</strong>
        </div>
        <div className="metric-card">
          <span>Current Role</span>
          <strong>{task.current_role ? titleCase(task.current_role) : "Unassigned"}</strong>
        </div>
        <div className="metric-card">
          <span>Risk</span>
          <RiskBadge level={task.risk_level} />
        </div>
      </section>

      <GateStatusPanel events={events} />

      <section className="detail-grid">
        <article className="detail-card">
          <div className="section-header">
            <div>
              <div className="eyebrow">Primary Understanding</div>
              <h3>Semantic Translation</h3>
            </div>
          </div>

          <SemanticTranslationPanel
            translation={translationDocument}
            rawTranslationJson={task.translation_json}
          />
        </article>

        <article className="detail-card">
          <div className="section-header">
            <div>
              <div className="eyebrow">Execution Snapshot</div>
              <h3>Task State</h3>
            </div>
          </div>

          <dl className="metadata-grid">
            <div>
              <dt>Task ID</dt>
              <dd>{task.id}</dd>
            </div>
            <div>
              <dt>Session ID</dt>
              <dd>{task.session_id ?? "N/A"}</dd>
            </div>
            <div>
              <dt>Scenario</dt>
              <dd>{titleCase(task.scenario)}</dd>
            </div>
            <div>
              <dt>Created</dt>
              <dd>{formatDateTime(task.created_at)}</dd>
            </div>
            <div>
              <dt>Updated</dt>
              <dd>{formatDateTime(task.updated_at)}</dd>
            </div>
            <div>
              <dt>Pending Approval</dt>
              <dd>{task.pending_approval ? "Yes" : "No"}</dd>
            </div>
            <div>
              <dt>Retry Count</dt>
              <dd>{task.retry_count}</dd>
            </div>
            <div>
              <dt>Review Verdict</dt>
              <dd>{task.review_verdict ? titleCase(task.review_verdict) : "N/A"}</dd>
            </div>
            <div>
              <dt>Review Stage</dt>
              <dd>{task.review_stage ? titleCase(task.review_stage) : "N/A"}</dd>
            </div>
          </dl>
        </article>

        <article className="detail-card">
          <div className="section-header">
            <div>
              <div className="eyebrow">Planner Output</div>
              <h3>Execution Plan</h3>
            </div>
            <PlanProviderBadge
              providerName={task.plan_provider_name}
              providerMode={task.plan_provider_mode}
              usedFallback={task.plan_used_fallback}
            />
          </div>

          {planSummary ? <p className="lead-copy">{planSummary}</p> : <p>No plan has been recorded.</p>}

          <dl className="metadata-grid compact">
            <div>
              <dt>Provider</dt>
              <dd>{task.plan_provider_name ?? "Unknown"}</dd>
            </div>
            <div>
              <dt>Mode</dt>
              <dd>{task.plan_provider_mode ? titleCase(task.plan_provider_mode) : "N/A"}</dd>
            </div>
            <div>
              <dt>Model</dt>
              <dd>{task.plan_model_name ?? "N/A"}</dd>
            </div>
            <div>
              <dt>Fallback</dt>
              <dd>{task.plan_used_fallback ? "Yes" : "No"}</dd>
            </div>
          </dl>

          {task.plan_fallback_reason ? (
            <div className="warning-banner">{task.plan_fallback_reason}</div>
          ) : null}

          <PlanBreakdown plan={planDocument} rawPlanJson={task.plan_json} />
        </article>
      </section>

      <section className="detail-grid">
        <article className="detail-card">
          <div className="section-header">
            <div>
              <div className="eyebrow">Reviewer Output</div>
              <h3>Latest Review</h3>
            </div>
            <ReviewVerdictBadge verdict={task.review_verdict} />
          </div>

          {task.review_summary ? <p className="lead-copy">{task.review_summary}</p> : null}

          <dl className="metadata-grid compact">
            <div>
              <dt>Review Stage</dt>
              <dd>{task.review_stage ? titleCase(task.review_stage) : "N/A"}</dd>
            </div>
            <div>
              <dt>Verdict</dt>
              <dd>{task.review_verdict ? titleCase(task.review_verdict) : "N/A"}</dd>
            </div>
          </dl>

          <ReviewBreakdown review={reviewDocument} rawReviewJson={task.review_json} />
        </article>

        <article className="detail-card">
          <div className="section-header">
            <div>
              <div className="eyebrow">Execution Output</div>
              <h3>Latest Result</h3>
            </div>
          </div>

          <KnowledgeResultPanel result={knowledgeResult} />

          {task.latest_result_json ? (
            <pre className="json-panel">{JSON.stringify(task.latest_result_json, null, 2)}</pre>
          ) : (
            <p>No result emitted yet.</p>
          )}
        </article>

        <article className="detail-card">
          <div className="section-header">
            <div>
              <div className="eyebrow">Operator Actions</div>
              <h3>Rollback</h3>
            </div>
          </div>

          <p>
            Rollback only reverts platform state and records audit events. It does not perform external
            compensation.
          </p>

          <textarea
            className="text-area compact-text-area"
            value={rollbackReason}
            onChange={(event) => setRollbackReason(event.target.value)}
          />

          <div className="button-row">
            <button
              className="button ghost"
              onClick={() => rollbackMutation.mutate()}
              disabled={rollbackMutation.isPending || task.status === "rolled_back"}
            >
              {rollbackMutation.isPending ? "Rolling Back..." : "Rollback Task"}
            </button>
          </div>

          {rollbackMutation.isError ? (
            <div className="error-banner">{toErrorMessage(rollbackMutation.error)}</div>
          ) : null}
        </article>
      </section>

      <section className="detail-card">
        <div className="section-header">
          <div>
            <div className="eyebrow">Approval Layer</div>
            <h3>Approvals</h3>
          </div>
          {pendingApproval ? <span className="mini-pill">Action Required</span> : null}
        </div>

        <ApprovalPanel
          approvals={task.approvals}
          actionNotes={actionNotes}
          onActionNotesChange={setActionNotes}
          onGrant={(approvalId) => grantMutation.mutate(approvalId)}
          onReject={(approvalId) => rejectMutation.mutate(approvalId)}
          isSubmitting={grantMutation.isPending || rejectMutation.isPending}
        />

        {grantMutation.isError ? <div className="error-banner">{toErrorMessage(grantMutation.error)}</div> : null}
        {rejectMutation.isError ? <div className="error-banner">{toErrorMessage(rejectMutation.error)}</div> : null}
      </section>

      <section className="detail-card">
        <ToolExecutionPanel executions={toolExecutions} />
      </section>

      <section className="detail-card">
        <div className="section-header">
          <div>
            <div className="eyebrow">Session Event Store</div>
            <h3>Event Timeline</h3>
          </div>
        </div>

        <TaskTimeline events={events} />
      </section>
    </div>
  );
}

function LiveBadge({ stream }: { stream: ReturnType<typeof useTaskStream> }) {
  if (stream.done) {
    return (
      <span className="live-badge live-badge-done" title="Stream closed at terminal status.">
        <span className="live-dot live-dot-done" />
        终止
      </span>
    );
  }
  if (stream.paused) {
    return (
      <span className="live-badge live-badge-paused" title="Task is awaiting human approval.">
        <span className="live-dot live-dot-paused" />
        待审批
      </span>
    );
  }
  if (!stream.connected) {
    return (
      <span className="live-badge live-badge-off" title={stream.error ?? "Connecting…"}>
        <span className="live-dot live-dot-off" />
        连接中…
      </span>
    );
  }
  const ago = stream.lastEventAgoMs ?? 0;
  const fresh = ago < 30_000;
  return (
    <span
      className={`live-badge ${fresh ? "live-badge-live" : "live-badge-stale"}`}
      title={`Last server message ${Math.round(ago / 1000)}s ago.`}
    >
      <span className={`live-dot ${fresh ? "live-dot-live" : "live-dot-stale"}`} />
      {fresh ? "实时" : `${Math.round(ago / 1000)}s 前`}
    </span>
  );
}
