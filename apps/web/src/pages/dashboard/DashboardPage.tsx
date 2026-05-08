import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { api } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import type { TaskSummary } from "../../types";

interface IntegrationEntry {
  key: string;
  label: string;
  status: "connected" | "not_configured" | "coming_soon";
}

function isToday(iso: string): boolean {
  const d = new Date(iso);
  const now = new Date();
  return (
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate()
  );
}

function statusLabel(status: string): string {
  switch (status) {
    case "running":
    case "executing":
      return "运行中";
    case "awaiting_approval":
    case "waiting_approval":
      return "待审批";
    case "completed":
      return "已完成";
    case "failed":
      return "失败";
    case "rolled_back":
      return "已回滚";
    case "queued":
    case "created":
      return "待启动";
    case "rejected":
      return "已拒绝";
    case "planning":
      return "规划中";
    case "reviewing":
      return "审查中";
    default:
      return status;
  }
}

function relativeTime(iso: string): string {
  const ts = new Date(iso).getTime();
  const diff = Date.now() - ts;
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "刚刚";
  if (mins < 60) return `${mins} 分钟前`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs} 小时前`;
  const days = Math.floor(hrs / 24);
  return `${days} 天前`;
}

function nowString(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function DashboardPage() {
  const { user } = useAuth();

  const tasksQ = useQuery({
    queryKey: ["dashboard", "tasks"],
    queryFn: () => api.listTasks(),
    refetchInterval: 15_000,
  });
  const integrationsQ = useQuery({
    queryKey: ["dashboard", "integrations"],
    queryFn: () => api.getIntegrationStatus(),
  });
  const knowledgeDocsQ = useQuery({
    queryKey: ["dashboard", "knowledge-docs"],
    queryFn: () => api.getKnowledgeDocuments(undefined, 200),
  });
  const knowledgeSourcesQ = useQuery({
    queryKey: ["dashboard", "knowledge-sources"],
    queryFn: () => api.getKnowledgeSources(),
  });

  const tasks: TaskSummary[] = tasksQ.data ?? [];
  const integrations: IntegrationEntry[] =
    (integrationsQ.data?.integrations ?? []) as IntegrationEntry[];

  const running = tasks.filter((t) => t.status === "running" || t.status === "executing").length;
  const awaiting = tasks.filter((t) => t.pending_approval).length;
  const totalTasks = tasks.length;
  const doneToday = tasks.filter((t) => t.status === "completed" && isToday(t.updated_at)).length;
  const failed = tasks.filter((t) => t.status === "failed").length;

  const docCount = knowledgeDocsQ.data?.length ?? 0;
  const sourceCount = knowledgeSourcesQ.data?.length ?? 0;

  const connected = integrations.filter((i) => i.status === "connected").length;
  const notConfigured = integrations.filter((i) => i.status === "not_configured").length;

  const recent = [...tasks]
    .sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime())
    .slice(0, 6);
  const focused = [...tasks]
    .filter((t) => t.pending_approval || t.risk_level === "high")
    .slice(0, 4);

  return (
    <div className="page-shell dashboard-page-v2">
      <header className="dash-header">
        <div>
          <h1>欢迎回来,{user?.name ?? "Tomonkyo"}</h1>
          <p className="dash-subtitle">
            运维代理平台运行情况一览。当前默认模型与子代理覆盖均按 .env 与 settings 中的偏好生效。
          </p>
        </div>
        <div className="dash-header-meta">
          <span className="dash-time-chip">{nowString()}</span>
        </div>
      </header>

      <section className="dash-stat-grid">
        <StatCard
          label="任务总数"
          big={totalTasks}
          subItems={[
            { label: "运行中", value: running, tone: "blue" },
            { label: "今日完成", value: doneToday, tone: "green" },
          ]}
          to="/tasks"
        />
        <StatCard
          label="待审批"
          big={awaiting}
          subItems={[
            { label: "运行中", value: running, tone: "blue" },
            { label: "失败", value: failed, tone: "red" },
          ]}
          to="/tasks"
          tone="amber"
        />
        <StatCard
          label="知识库"
          big={docCount}
          subItems={[
            { label: "来源", value: sourceCount, tone: "muted" },
          ]}
          to="/knowledge"
        />
        <StatCard
          label="集成"
          big={connected}
          subItems={[
            { label: "已连接", value: connected, tone: "green" },
            { label: "未配置", value: notConfigured, tone: "amber" },
          ]}
          to="/integrations"
        />
      </section>

      <section className="dash-bottom-grid">
        <article className="dash-panel dash-panel-recent">
          <header className="dash-panel-head">
            <h2>最近活动</h2>
            <Link className="dash-link" to="/tasks">
              查看全部 →
            </Link>
          </header>
          {tasksQ.isLoading ? (
            <p className="dash-empty">加载中…</p>
          ) : recent.length === 0 ? (
            <p className="dash-empty">
              尚未创建任何任务。前往 <Link to="/chat">对话</Link> 开始一个任务。
            </p>
          ) : (
            <ul className="dash-task-list">
              {recent.map((t) => (
                <li key={t.id}>
                  <Link className="dash-task-row" to={`/tasks/${t.id}`}>
                    <span className={`dash-task-status status-${t.status}`}>
                      {statusLabel(t.status)}
                    </span>
                    <span className="dash-task-title">{t.title || t.id.slice(0, 8)}</span>
                    <span className="dash-task-actor">
                      <span className="dash-actor-avatar">
                        {(t.actor_name || "?").slice(0, 1).toUpperCase()}
                      </span>
                      {t.actor_name}
                    </span>
                    <span className="dash-task-time">{relativeTime(t.updated_at)}</span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </article>

        <article className="dash-panel dash-panel-focus">
          <header className="dash-panel-head">
            <h2>团队关注</h2>
            <span className="dash-link-muted">{focused.length}</span>
          </header>
          {focused.length === 0 ? (
            <p className="dash-empty">暂无需要重点关注的任务。</p>
          ) : (
            <ul className="dash-focus-list">
              {focused.map((t) => (
                <li key={t.id}>
                  <Link to={`/tasks/${t.id}`} className="dash-focus-row">
                    <span
                      className={`dash-focus-dot ${
                        t.pending_approval ? "pending" : "warn"
                      }`}
                    />
                    <span className="dash-focus-title">{t.title || t.id.slice(0, 8)}</span>
                    <span className="dash-focus-meta">
                      {t.pending_approval ? "待审批" : "高风险"}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </article>
      </section>
    </div>
  );
}

function StatCard({
  label,
  big,
  subItems,
  to,
  tone,
}: {
  label: string;
  big: number;
  subItems: { label: string; value: number; tone?: string }[];
  to: string;
  tone?: "amber";
}) {
  return (
    <Link className={`dash-stat-card${tone ? ` tone-${tone}` : ""}`} to={to}>
      <div className="dash-stat-label">{label}</div>
      <div className="dash-stat-big">{big.toLocaleString()}</div>
      <div className="dash-stat-subrow">
        {subItems.map((s) => (
          <span key={s.label} className={`dash-stat-sub tone-${s.tone ?? "muted"}`}>
            <span className="dash-stat-sub-value">{s.value}</span>
            <span className="dash-stat-sub-label">{s.label}</span>
          </span>
        ))}
      </div>
    </Link>
  );
}
