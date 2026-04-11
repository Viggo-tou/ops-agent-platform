import type { TaskSummary } from "../../types";

interface TaskSummaryMetricsProps {
  tasks: TaskSummary[];
}

export function TaskSummaryMetrics({ tasks }: TaskSummaryMetricsProps) {
  const total = tasks.length;
  const waitingApproval = tasks.filter(
    (task) => task.status === "awaiting_approval" || task.status === "waiting_approval",
  ).length;
  const running = tasks.filter((task) =>
    ["created", "planning", "reviewing", "executing", "running", "queued"].includes(task.status),
  ).length;
  const completed = tasks.filter((task) => task.status === "completed").length;
  const exceptions = tasks.filter((task) => task.status === "failed" || task.status === "rolled_back").length;

  const metrics = [
    { label: "Total Tasks", value: total, tone: "neutral" },
    { label: "Waiting Approval", value: waitingApproval, tone: "warning" },
    { label: "Active Runtime", value: running, tone: "info" },
    { label: "Completed", value: completed, tone: "success" },
    { label: "Exceptions", value: exceptions, tone: "danger" },
  ];

  return (
    <section className="metrics-grid">
      {metrics.map((metric) => (
        <article className={`metric-card metric-${metric.tone}`} key={metric.label}>
          <span>{metric.label}</span>
          <strong>{metric.value}</strong>
        </article>
      ))}
    </section>
  );
}
