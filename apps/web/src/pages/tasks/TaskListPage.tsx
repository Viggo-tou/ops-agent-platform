import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useDeferredValue, useMemo, useState } from "react";

import { api } from "../../lib/api";
import type { TaskStatus, TaskSummary } from "../../types";

const STATUS_OPTIONS: Array<{ label: string; value: "all" | TaskStatus }> = [
  { label: "全部状态", value: "all" },
  { label: "待启动", value: "created" },
  { label: "规划中", value: "planning" },
  { label: "审查中", value: "reviewing" },
  { label: "待审批", value: "awaiting_approval" },
  { label: "运行中", value: "executing" },
  { label: "已完成", value: "completed" },
  { label: "失败", value: "failed" },
  { label: "已回滚", value: "rolled_back" },
];

const PROVIDER_OPTIONS = [
  { label: "全部模型", value: "all" },
  { label: "MiniMax", value: "minimax" },
  { label: "OpenAI", value: "openai" },
  { label: "Anthropic", value: "anthropic" },
  { label: "Mock", value: "mock" },
] as const;

const RISK_OPTIONS = [
  { label: "全部风险类别", value: "all" },
  { label: "开发", value: "development" },
  { label: "数据访问", value: "data_access" },
  { label: "外部消息", value: "external_messaging" },
  { label: "运维", value: "operations" },
] as const;

const SORT_OPTIONS = [
  { label: "最近更新", value: "updated_desc" },
  { label: "最早更新", value: "updated_asc" },
  { label: "创建时间(新→旧)", value: "created_desc" },
  { label: "创建时间(旧→新)", value: "created_asc" },
] as const;

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

function riskLabel(risk: string): string {
  return (
    {
      low: "低",
      medium: "中",
      high: "高",
      critical: "严重",
    } as Record<string, string>
  )[risk] ?? risk;
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

export function TaskListPage() {
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] =
    useState<"all" | TaskStatus>("all");
  const [providerFilter, setProviderFilter] =
    useState<(typeof PROVIDER_OPTIONS)[number]["value"]>("all");
  const [riskFilter, setRiskFilter] =
    useState<(typeof RISK_OPTIONS)[number]["value"]>("all");
  const [sortBy, setSortBy] =
    useState<(typeof SORT_OPTIONS)[number]["value"]>("updated_desc");

  const deferredSearch = useDeferredValue(search.trim());
  const filters = {
    search: deferredSearch || undefined,
    status: statusFilter === "all" ? undefined : statusFilter,
    provider: providerFilter === "all" ? undefined : providerFilter,
    riskCategory: riskFilter === "all" ? undefined : (riskFilter as any),
  };

  const tasksQ = useQuery({
    queryKey: ["tasks", filters],
    queryFn: () => api.listTasks(filters),
    refetchInterval: 5_000,
  });
  const usageQ = useQuery({
    queryKey: ["tools", "usage-stats", 7],
    queryFn: () => api.getToolUsageStats(7, 8),
    refetchInterval: 30_000,
  });

  const tasks: TaskSummary[] = tasksQ.data ?? [];

  // Stat metrics — over the *current filtered view*.
  const m_total = tasks.length;
  const m_awaiting = tasks.filter((t) => t.pending_approval).length;
  const m_running = tasks.filter((t) => t.status === "running" || t.status === "executing").length;
  const m_completed = tasks.filter((t) => t.status === "completed").length;
  const m_high = tasks.filter((t) => t.risk_level === "high" || t.risk_level === "critical").length;

  const sortedTasks = useMemo(() => {
    const arr = [...tasks];
    arr.sort((a, b) => {
      const ua = new Date(a.updated_at).getTime();
      const ub = new Date(b.updated_at).getTime();
      const ca = new Date(a.created_at).getTime();
      const cb = new Date(b.created_at).getTime();
      switch (sortBy) {
        case "updated_asc":
          return ua - ub;
        case "created_desc":
          return cb - ca;
        case "created_asc":
          return ca - cb;
        case "updated_desc":
        default:
          return ub - ua;
      }
    });
    return arr;
  }, [tasks, sortBy]);

  return (
    <div className="page-shell tasklist-page-v2">
      <header className="tl-header">
        <div>
          <div className="page-section-eyebrow">任务管理</div>
          <h1>任务列表</h1>
          <p className="tl-subtitle">
            浏览代理已持久化的全部任务,跟踪执行阶段、审批状态以及最近更新。
          </p>
        </div>
        <div className="tl-actions">
          <Link to="/chat" className="button primary">
            + 新建任务
          </Link>
        </div>
      </header>

      <section className="tl-stat-grid">
        <TlStatCard label="总任务" value={m_total} hint="当前筛选下" />
        <TlStatCard label="待审批" value={m_awaiting} tone="amber" />
        <TlStatCard label="运行中" value={m_running} tone="blue" />
        <TlStatCard label="已完成" value={m_completed} tone="green" />
        <TlStatCard label="高风险" value={m_high} tone="red" hint="high / critical" />
      </section>

      <section className="tl-filter-card">
        <div className="tl-filter-row">
          <input
            className="tl-search-input"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="搜索任务标题、ID、actor…"
          />
          <select
            className="tl-select"
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value as any)}
          >
            {STATUS_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
          <select
            className="tl-select"
            value={providerFilter}
            onChange={(e) => setProviderFilter(e.target.value as any)}
          >
            {PROVIDER_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
          <select
            className="tl-select"
            value={riskFilter}
            onChange={(e) => setRiskFilter(e.target.value as any)}
          >
            {RISK_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
          <select
            className="tl-select"
            value={sortBy}
            onChange={(e) => setSortBy(e.target.value as any)}
          >
            {SORT_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                排序: {o.label}
              </option>
            ))}
          </select>
        </div>
      </section>

      <section className="tl-body-grid">
        <article className="tl-table-card">
          <header className="tl-table-head">
            <h2>任务列表 ({sortedTasks.length})</h2>
          </header>
          <div className="tl-table-wrap">
            <div className="tl-table-row tl-table-row-head">
              <div>任务</div>
              <div>风险类别</div>
              <div>角色</div>
              <div>状态</div>
              <div>Actor</div>
              <div>更新时间</div>
            </div>
            {tasksQ.isLoading ? (
              <p className="tl-empty">加载中…</p>
            ) : sortedTasks.length === 0 ? (
              <p className="tl-empty">没有匹配的任务。</p>
            ) : (
              sortedTasks.map((t) => (
                <Link
                  key={t.id}
                  to={`/tasks/${t.id}`}
                  className="tl-table-row tl-table-row-data"
                >
                  <div className="tl-cell-title">
                    <strong>{t.title || t.id.slice(0, 8)}</strong>
                    <span className="tl-cell-id">#{t.id.slice(0, 8)}</span>
                  </div>
                  <div>
                    <span className="tl-pill risk">{t.risk_category}</span>
                  </div>
                  <div>
                    <span className="tl-pill role">{t.actor_role}</span>
                  </div>
                  <div>
                    <span className={`tl-pill status status-${t.status}`}>
                      {statusLabel(t.status)}
                    </span>
                    {t.risk_level === "high" || t.risk_level === "critical" ? (
                      <span className="tl-pill risk-level high">
                        风险 {riskLabel(t.risk_level)}
                      </span>
                    ) : null}
                  </div>
                  <div className="tl-cell-actor">
                    <span className="tl-actor-avatar">
                      {(t.actor_name || "?").slice(0, 1).toUpperCase()}
                    </span>
                    <span>{t.actor_name}</span>
                  </div>
                  <div className="tl-cell-time">{relativeTime(t.updated_at)}</div>
                </Link>
              ))
            )}
          </div>
        </article>

        <article className="tl-tools-card">
          <header className="tl-table-head">
            <h2>工具调用量</h2>
            <span className="tl-link-muted">近 7 天</span>
          </header>
          <ToolDonut
            successRate={usageQ.data?.success_rate ?? 0}
            total={usageQ.data?.total_invocations ?? 0}
            succeeded={usageQ.data?.succeeded ?? 0}
            failed={usageQ.data?.failed ?? 0}
            loading={usageQ.isLoading}
          />
          <ul className="tl-tool-list">
            {(usageQ.data?.by_tool ?? []).map((t) => (
              <li key={t.tool_name}>
                <div className="tl-tool-line">
                  <span className="tl-tool-name">{t.tool_name}</span>
                  <span className="tl-tool-count">{t.total}</span>
                </div>
                <div className="tl-tool-bar">
                  <div
                    className="tl-tool-bar-fill"
                    style={{ width: `${Math.round(t.success_rate * 100)}%` }}
                  />
                </div>
                <div className="tl-tool-meta">
                  成功 {t.succeeded} · 失败 {t.failed} ·{" "}
                  {Math.round(t.success_rate * 100)}%
                </div>
              </li>
            ))}
            {(usageQ.data?.by_tool ?? []).length === 0 && !usageQ.isLoading ? (
              <li className="tl-empty">无统计数据。</li>
            ) : null}
          </ul>
        </article>
      </section>
    </div>
  );
}

function TlStatCard({
  label,
  value,
  tone,
  hint,
}: {
  label: string;
  value: number;
  tone?: "amber" | "blue" | "green" | "red";
  hint?: string;
}) {
  return (
    <div className={`tl-stat-card${tone ? ` tone-${tone}` : ""}`}>
      <div className="tl-stat-label">{label}</div>
      <div className="tl-stat-value">{value.toLocaleString()}</div>
      {hint ? <div className="tl-stat-hint">{hint}</div> : null}
    </div>
  );
}

function ToolDonut({
  successRate,
  total,
  succeeded,
  failed,
  loading,
}: {
  successRate: number;
  total: number;
  succeeded: number;
  failed: number;
  loading: boolean;
}) {
  const pct = Math.round(successRate * 100);
  // svg donut: r=42, circumference 2*pi*42 ~= 263.89
  const C = 2 * Math.PI * 42;
  const dash = (pct / 100) * C;
  return (
    <div className="tl-donut">
      <svg width="140" height="140" viewBox="0 0 100 100" aria-hidden="true">
        <circle
          cx="50"
          cy="50"
          r="42"
          fill="none"
          stroke="#f1f1f1"
          strokeWidth="10"
        />
        <circle
          cx="50"
          cy="50"
          r="42"
          fill="none"
          stroke="#047857"
          strokeWidth="10"
          strokeDasharray={`${dash} ${C - dash}`}
          strokeDashoffset={C / 4}
          strokeLinecap="round"
          transform="rotate(-90 50 50)"
        />
        <text x="50" y="48" textAnchor="middle" className="tl-donut-pct">
          {loading ? "…" : `${pct}%`}
        </text>
        <text x="50" y="62" textAnchor="middle" className="tl-donut-sub">
          成功率
        </text>
      </svg>
      <div className="tl-donut-legend">
        <div>
          <span className="tl-dot tl-dot-green" /> 成功 <strong>{succeeded}</strong>
        </div>
        <div>
          <span className="tl-dot tl-dot-red" /> 失败 <strong>{failed}</strong>
        </div>
        <div className="tl-donut-total">总调用 {total}</div>
      </div>
    </div>
  );
}
