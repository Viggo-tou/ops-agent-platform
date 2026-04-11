import { Link } from "react-router-dom";

import { formatDateTime, titleCase } from "../../lib/format";
import type { TaskSummary } from "../../types";
import { PlanProviderBadge } from "./PlanProviderBadge";
import { ReviewVerdictBadge } from "./ReviewVerdictBadge";
import { RiskBadge, TaskStatusBadge } from "./TaskStatusBadge";

interface TaskTableProps {
  tasks: TaskSummary[];
  emptyTitle?: string;
  emptyMessage?: string;
}

export function TaskTable({
  tasks,
  emptyTitle = "No tasks yet",
  emptyMessage = "Submit a request to create the first task in this local environment.",
}: TaskTableProps) {
  if (tasks.length === 0) {
    return (
      <div className="empty-panel">
        <h3>{emptyTitle}</h3>
        <p>{emptyMessage}</p>
      </div>
    );
  }

  return (
    <div className="table-card">
      <table className="task-table">
        <thead>
          <tr>
            <th>Task</th>
            <th>Status</th>
            <th>Scenario</th>
            <th>Stage</th>
            <th>Plan Source</th>
            <th>Review</th>
            <th>Risk</th>
            <th>Updated</th>
          </tr>
        </thead>
        <tbody>
          {tasks.map((task) => (
            <tr key={task.id}>
              <td>
                <Link to={`/tasks/${task.id}`} className="task-link">
                  {task.title}
                </Link>
                <div className="muted-text">{task.id}</div>
                <div className="muted-text">session {task.session_id ?? "n/a"}</div>
              </td>
              <td>
                <TaskStatusBadge status={task.status} />
              </td>
              <td>{titleCase(task.scenario)}</td>
              <td>{titleCase(task.workflow_stage)}</td>
              <td>
                <PlanProviderBadge
                  providerName={task.plan_provider_name}
                  providerMode={task.plan_provider_mode}
                  usedFallback={task.plan_used_fallback}
                />
              </td>
              <td>
                <ReviewVerdictBadge verdict={task.review_verdict} />
                <div className="muted-text">{task.review_stage ? titleCase(task.review_stage) : "Not reviewed"}</div>
                <div className="muted-text">{task.review_summary ?? "No review summary yet"}</div>
              </td>
              <td>
                <RiskBadge level={task.risk_level} />
              </td>
              <td>{formatDateTime(task.updated_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
