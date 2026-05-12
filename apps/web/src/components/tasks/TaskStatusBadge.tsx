import type { RiskLevel, TaskStatus } from "../../types";

interface TaskStatusBadgeProps {
  status: TaskStatus;
}

export function TaskStatusBadge({ status }: TaskStatusBadgeProps) {
  return <span className={`pill status-${status}`}>{status.replace(/_/g, " ")}</span>;
}

interface RiskBadgeProps {
  level: RiskLevel;
}

export function RiskBadge({ level }: RiskBadgeProps) {
  return <span className={`pill risk-${level}`}>{level} risk</span>;
}
