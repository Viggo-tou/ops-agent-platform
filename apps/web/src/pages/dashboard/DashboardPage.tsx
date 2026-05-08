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
      return "异常";
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

function statusBucket(status: string): "waiting" | "done" | "failed" | "running" | "other" {
  if (status === "awaiting_approval" || status === "waiting_approval") return "waiting";
  if (status === "completed") return "done";
  if (status === "failed" || status === "rejected") return "failed";
  if (status === "running" || status === "executing") return "running";
  return "other";
}

function relativeTime(iso: string): string {
  const ts = new Date(iso).getTime();
  if (Number.isNaN(ts)) return "—";
  const diff = Date.now() - ts;
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "刚刚";
  if (mins < 60) return `${mins} 分钟前`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs} 小时前`;
  const days = Math.floor(hrs / 24);
  return `${days} 天前`;
}

function Icon({ kind, className }: { kind: string; className?: string }) {
  const props = { className, viewBox: "0 0 24 24", "aria-hidden": true } as const;
  switch (kind) {
    case "inbox":
      return <svg {...props}><path d="M22 13h-6l-2 3h-4l-2-3H2M5.45 5.11 2 13v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-7.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11Z" /></svg>;
    case "clock":
      return <svg {...props}><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></svg>;
    case "play":
      return <svg {...props}><circle cx="12" cy="12" r="9" /><path d="m10 8 6 4-6 4Z" /></svg>;
    case "check":
      return <svg {...props}><circle cx="12" cy="12" r="9" /><path d="m8 12 3 3 5-6" /></svg>;
    case "alert":
      return <svg {...props}><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z" /><path d="M12 9v4M12 17h.01" /></svg>;
    case "book":
      return <svg {...props}><path d="M4 4.5A1.5 1.5 0 0 1 5.5 3H10c1.1 0 2 .9 2 2v15c0-1.1-.9-2-2-2H5.5A1.5 1.5 0 0 1 4 16.5v-12Z" /><path d="M20 4.5A1.5 1.5 0 0 0 18.5 3H14c-1.1 0-2 .9-2 2v15c0-1.1.9-2 2-2h4.5a1.5 1.5 0 0 0 1.5-1.5v-12Z" /></svg>;
    case "wrench":
      return <svg {...props}><path d="M14.7 6.3a4 4 0 0 0-5.4 5.4l-6 6a2 2 0 0 0 2.8 2.8l6-6a4 4 0 0 0 5.4-5.4l-2.4 2.4-2-2 2.4-2.4Z" /></svg>;
    case "spark":
      return <svg {...props}><path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.5 5.5l2.8 2.8M15.7 15.7l2.8 2.8M5.5 18.5l2.8-2.8M15.7 8.3l2.8-2.8" /><circle cx="12" cy="12" r="3" /></svg>;
    case "arrow-right":
      return <svg {...props}><path d="M5 12h14M13 5l7 7-7 7" /></svg>;
    case "more":
      return <svg {...props}><circle cx="5" cy="12" r="1.5" /><circle cx="12" cy="12" r="1.5" /><circle cx="19" cy="12" r="1.5" /></svg>;
    default:
      return null;
  }
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
  const failed = tasks.filter(
    (t) => t.status === "failed" || t.status === "rejected" || t.status === "rolled_back",
  ).length;

  const docCount = knowledgeDocsQ.data?.length ?? 0;
  const sourceCount = knowledgeSourcesQ.data?.length ?? 0;

  const connected = integrations.filter((i) => i.status === "connected").length;
  const notConfigured = integrations.filter((i) => i.status === "not_configured").length;

  const recent = [...tasks]
    .sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime())
    .slice(0, 6);
  const focused = [...tasks]
    .filter((t) => t.pending_approval || t.risk_level === "high" || t.risk_level === "critical")
    .slice(0, 5);

  return (
    <div className="page-shell dash3-page">
      <section className="dash3-hero">
        <div className="dash3-hero-top">
          <div>
            <div className="page-section-eyebrow">总览</div>
            <h1>欢迎回来,{user?.name ?? "Tomonkyo"}</h1>
            <p className="dash3-hero-subtitle">
              运维代理平台运行情况一览。当前默认模型与子代理覆盖均按 .env
              与 settings 中的偏好生效。
            </p>
          </div>
          <Link to="/chat" className="tl3-primary-btn">
            <Icon kind="spark" />
            新建对话
          </Link>
        </div>

        <div className="dash3-kpi-grid">
          <KpiCard
            iconKind="inbox"
            tone="blue"
            title="任务总数"
            value={totalTasks}
            subtitle={`运行中 ${running} · 今日完成 ${doneToday}`}
          />
          <KpiCard
            iconKind="clock"
            tone="orange"
            title="待审批"
            value={awaiting}
            subtitle={
              awaiting === 0 ? "暂无待审批任务" : "需要人工审核"
            }
          />
          <KpiCard
            iconKind="alert"
            tone="red"
            title="异常 / 失败"
            value={failed}
            subtitle="需要处理"
          />
          <KpiCard
            iconKind="book"
            tone="purple"
            title="知识库"
            value={docCount}
            subtitle={`${sourceCount} 个来源`}
          />
          <KpiCard
            iconKind="wrench"
            tone="green"
            title="集成"
            value={connected}
            subtitle={`已连接 ${connected} · 未配置 ${notConfigured}`}
          />
        </div>
      </section>

      <div className="dash3-body-grid">
        <article className="dash3-panel">
          <header className="tl3-table-head">
            <h2>最近活动</h2>
            <Link to="/tasks" className="tl3-link-blue">
              查看全部 <Icon kind="arrow-right" />
            </Link>
          </header>
          {tasksQ.isLoading ? (
            <p className="tl3-empty">加载中…</p>
          ) : recent.length === 0 ? (
            <p className="tl3-empty">
              尚未创建任何任务。前往{" "}
              <Link to="/chat" className="tl3-link-blue">对话</Link> 开始一个任务。
            </p>
          ) : (
            <ul className="dash3-task-list">
              {recent.map((t) => (
                <li key={t.id}>
                  <Link className="dash3-task-row" to={`/tasks/${t.id}`}>
                    <div>
                      <div className="dash3-task-title">{t.title || t.id.slice(0, 8)}</div>
                      <div className="dash3-task-id">#{t.id.slice(0, 8)}</div>
                    </div>
                    <span
                      className={`tl3-status-pill bucket-${statusBucket(t.status)}`}
                    >
                      {statusLabel(t.status)}
                    </span>
                    <div className="dash3-task-owner">
                      <span className="tl3-avatar">
                        {(t.actor_name || "?").slice(0, 1).toUpperCase()}
                      </span>
                      <span>{t.actor_name}</span>
                    </div>
                    <div className="dash3-task-time">{relativeTime(t.updated_at)}</div>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </article>

        <aside className="dash3-panel dash3-panel-focus">
          <header className="tl3-table-head">
            <h2>团队关注</h2>
            <span className="tl3-readiness-pill">{focused.length}</span>
          </header>
          {focused.length === 0 ? (
            <p className="tl3-empty">暂无需要重点关注的任务。</p>
          ) : (
            <ul className="dash3-focus-list">
              {focused.map((t) => (
                <li key={t.id}>
                  <Link to={`/tasks/${t.id}`} className="dash3-focus-row">
                    <span
                      className={`dash3-focus-dot ${
                        t.pending_approval ? "pending" : "warn"
                      }`}
                    />
                    <div className="dash3-focus-main">
                      <div className="dash3-focus-title">{t.title || t.id.slice(0, 8)}</div>
                      <div className="dash3-focus-meta">
                        {t.actor_name} · {relativeTime(t.updated_at)}
                      </div>
                    </div>
                    <span
                      className={`tl3-status-pill bucket-${statusBucket(t.status)}`}
                    >
                      {t.pending_approval ? "待审批" : "高风险"}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </aside>
      </div>
    </div>
  );
}

function KpiCard({
  iconKind,
  tone,
  title,
  value,
  subtitle,
}: {
  iconKind: string;
  tone: "blue" | "orange" | "purple" | "green" | "red";
  title: string;
  value: number;
  subtitle: string;
}) {
  return (
    <div className="tl3-kpi-card">
      <div className={`tl3-kpi-iconwrap tone-${tone}`}>
        <Icon kind={iconKind} />
      </div>
      <div className="tl3-kpi-title">{title}</div>
      <div className="tl3-kpi-value">{value.toLocaleString()}</div>
      <div className="tl3-kpi-subtitle">{subtitle}</div>
    </div>
  );
}
