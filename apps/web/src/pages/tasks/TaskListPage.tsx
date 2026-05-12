import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useDeferredValue, useEffect, useMemo, useState } from "react";

import { api } from "../../lib/api";
import type { TaskStatus, TaskSummary, ToolRegistryEntry } from "../../types";

const PAGE_SIZE_DEFAULT = 10;
const PAGE_SIZES = [10, 20, 50];

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
  { label: "超时未推进", value: "stale_failed" },
];

const SORT_OPTIONS = [
  { label: "更新时间(最新)", value: "updated_desc" },
  { label: "更新时间(最早)", value: "updated_asc" },
  { label: "创建时间(最新)", value: "created_desc" },
  { label: "创建时间(最早)", value: "created_asc" },
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
      return "异常";
    case "rolled_back":
      return "已回滚";
    case "stale_failed":
      return "超时未推进";
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

function readiness(entry: ToolRegistryEntry): "ready" | "partial" | "unavailable" {
  if (!entry.enabled) return "unavailable";
  if ((entry.missing_configuration ?? []).length > 0) return "partial";
  return "ready";
}

// ---- Icons (inline SVG) ------------------------------------------------

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
    case "search":
      return <svg {...props}><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>;
    case "filter":
      return <svg {...props}><path d="M22 3H2l8 9.46V19l4 2v-8.54Z" /></svg>;
    case "refresh":
      return <svg {...props}><path d="M3 12a9 9 0 0 1 15-6.7L21 8M21 3v5h-5M21 12a9 9 0 0 1-15 6.7L3 16M3 21v-5h5" /></svg>;
    case "chev-down":
      return <svg {...props}><path d="m6 9 6 6 6-6" /></svg>;
    case "chev-left":
      return <svg {...props}><path d="m15 18-6-6 6-6" /></svg>;
    case "chev-right":
      return <svg {...props}><path d="m9 18 6-6-6-6" /></svg>;
    case "more":
      return <svg {...props}><circle cx="5" cy="12" r="1.5" /><circle cx="12" cy="12" r="1.5" /><circle cx="19" cy="12" r="1.5" /></svg>;
    case "plus":
      return <svg {...props}><circle cx="12" cy="12" r="9" /><path d="M12 8v8M8 12h8" /></svg>;
    case "arrow-right":
      return <svg {...props}><path d="M5 12h14M13 5l7 7-7 7" /></svg>;
    case "check-pill":
      return <svg {...props}><path d="m5 12 5 5L20 7" /></svg>;
    case "x-pill":
      return <svg {...props}><path d="M18 6 6 18M6 6l12 12" /></svg>;
    case "clock-pill":
      return <svg {...props}><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></svg>;
    default:
      return null;
  }
}

// ---- Page --------------------------------------------------------------

export function TaskListPage() {
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] =
    useState<"all" | TaskStatus>("all");
  const [repoFilter, setRepoFilter] = useState<string>("all");
  const [sortBy, setSortBy] =
    useState<(typeof SORT_OPTIONS)[number]["value"]>("updated_desc");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(PAGE_SIZE_DEFAULT);

  const deferredSearch = useDeferredValue(search.trim());

  const filters = {
    search: deferredSearch || undefined,
    status: statusFilter === "all" ? undefined : statusFilter,
  };

  const tasksQ = useQuery({
    queryKey: ["tasks", filters],
    queryFn: () => api.listTasks(filters),
    refetchInterval: 5_000,
  });
  const registryQ = useQuery({
    queryKey: ["tool-registry"],
    queryFn: () => api.getToolRegistry(),
    refetchInterval: 30_000,
  });

  const tasks: TaskSummary[] = tasksQ.data ?? [];
  const registry: ToolRegistryEntry[] = registryQ.data ?? [];

  // Reset page on filter / search / size change.
  useEffect(() => {
    setPage(1);
  }, [statusFilter, repoFilter, deferredSearch, sortBy, pageSize]);

  const repoOptions = useMemo(() => {
    const set = new Set<string>();
    for (const t of tasks) {
      if (t.source_name) set.add(t.source_name);
    }
    return ["all", ...Array.from(set).sort()];
  }, [tasks]);

  const filteredAndSorted = useMemo(() => {
    let arr = tasks;
    if (repoFilter !== "all") {
      arr = arr.filter((t) => t.source_name === repoFilter);
    }
    arr = [...arr].sort((a, b) => {
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
  }, [tasks, repoFilter, sortBy]);

  // Stats — over the un-paginated filtered set.
  const m_total = tasks.length;
  const m_awaiting = tasks.filter((t) => t.pending_approval).length;
  const m_running = tasks.filter((t) => t.status === "running" || t.status === "executing").length;
  const m_completed = tasks.filter((t) => t.status === "completed").length;
  const m_failed = tasks.filter(
    (t) =>
      t.status === "failed" ||
      t.status === "rejected" ||
      t.status === "rolled_back" ||
      t.status === "stale_failed",
  ).length;

  // Pagination.
  const totalPages = Math.max(1, Math.ceil(filteredAndSorted.length / pageSize));
  const safePage = Math.min(page, totalPages);
  const pageStart = (safePage - 1) * pageSize;
  const pageItems = filteredAndSorted.slice(pageStart, pageStart + pageSize);

  // Tool readiness aggregates.
  const readyCount = registry.filter((e) => readiness(e) === "ready").length;
  const partialCount = registry.filter((e) => readiness(e) === "partial").length;
  const unavailableCount = registry.filter((e) => readiness(e) === "unavailable").length;
  const totalTools = registry.length;
  const readinessPct = totalTools ? Math.round((readyCount / totalTools) * 100) : 0;
  const keyTools = registry
    .slice()
    .sort((a, b) => a.name.localeCompare(b.name))
    .slice(0, 4);

  return (
    <div className="page-shell tasklist-page-v3">
      <section className="tl3-hero">
        <div className="tl3-hero-top">
          <div>
            <div className="page-section-eyebrow">任务管理</div>
            <h1>任务列表</h1>
            <p className="tl3-hero-subtitle">
              浏览持久化任务、执行阶段、审批状态和最新更新,全面掌控任务执行情况。
            </p>
          </div>
          <Link to="/chat" className="tl3-primary-btn">
            <Icon kind="plus" />
            新建任务
          </Link>
        </div>

        <div className="tl3-kpi-grid">
          <KpiCard
            iconKind="inbox"
            tone="blue"
            title="总任务"
            value={m_total}
            subtitle="全部时间"
          />
          <KpiCard
            iconKind="clock"
            tone="orange"
            title="待审批"
            value={m_awaiting}
            subtitle="需要人工审核"
          />
          <KpiCard
            iconKind="play"
            tone="purple"
            title="运行中"
            value={m_running}
            subtitle="正在执行"
          />
          <KpiCard
            iconKind="check"
            tone="green"
            title="已完成"
            value={m_completed}
            subtitle="成功完成"
          />
          <KpiCard
            iconKind="alert"
            tone="red"
            title="异常 / 失败"
            value={m_failed}
            subtitle="需要处理"
          />
        </div>

        <div className="tl3-filter-bar">
          <div className="tl3-search">
            <Icon kind="search" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="搜索任务名称、ID 或关键词..."
            />
          </div>
          <SelectButton
            label="状态"
            value={STATUS_OPTIONS.find((o) => o.value === statusFilter)?.label ?? "全部状态"}
            options={STATUS_OPTIONS.map((o) => ({ label: o.label, value: o.value }))}
            onSelect={(v) => setStatusFilter(v as any)}
          />
          <SelectButton
            label="仓库"
            value={repoFilter === "all" ? "全部仓库" : repoFilter}
            options={repoOptions.map((r) => ({
              label: r === "all" ? "全部仓库" : r,
              value: r,
            }))}
            onSelect={setRepoFilter}
          />
          <SelectButton
            label="排序"
            value={SORT_OPTIONS.find((o) => o.value === sortBy)?.label ?? ""}
            options={SORT_OPTIONS.map((o) => ({ label: o.label, value: o.value }))}
            onSelect={(v) => setSortBy(v as any)}
          />
          <button
            className="tl3-icon-btn"
            type="button"
            title="刷新"
            onClick={() => {
              tasksQ.refetch();
              registryQ.refetch();
            }}
          >
            <Icon kind="refresh" />
          </button>
        </div>
      </section>

      <div className="tl3-body-grid">
        <article className="tl3-table-card">
          <header className="tl3-table-head">
            <h2>
              任务列表 <span className="tl3-muted">({filteredAndSorted.length})</span>
            </h2>
          </header>

          <div className="tl3-table-wrap">
            <table className="tl3-table">
              <thead>
                <tr>
                  <th>任务名称</th>
                  <th>状态</th>
                  <th>阶段</th>
                  <th>仓库</th>
                  <th>所有者</th>
                  <th>更新时间</th>
                  <th className="tl3-th-right">操作</th>
                </tr>
              </thead>
              <tbody>
                {tasksQ.isLoading ? (
                  <tr>
                    <td colSpan={7} className="tl3-empty">加载中…</td>
                  </tr>
                ) : pageItems.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="tl3-empty">没有匹配的任务。</td>
                  </tr>
                ) : (
                  pageItems.map((t) => (
                    <tr
                      key={t.id}
                      className="tl3-row"
                      onClick={() => {
                        window.location.href = `/tasks/${t.id}`;
                      }}
                    >
                      <td>
                        <div className="tl3-task-name">{t.title || t.id.slice(0, 8)}</div>
                        <div className="tl3-task-id">#{t.id.slice(0, 8)}</div>
                      </td>
                      <td>
                        <StatusPill bucket={statusBucket(t.status)} label={statusLabel(t.status)} />
                      </td>
                      <td className="tl3-cell-muted">{t.workflow_stage ?? "—"}</td>
                      <td className="tl3-cell-muted">{t.source_name ?? "—"}</td>
                      <td>
                        <div className="tl3-owner">
                          <span className="tl3-avatar">
                            {(t.actor_name || "?").slice(0, 1).toUpperCase()}
                          </span>
                          <span>{t.actor_name}</span>
                        </div>
                      </td>
                      <td className="tl3-cell-muted">{relativeTime(t.updated_at)}</td>
                      <td className="tl3-th-right">
                        <Link to={`/tasks/${t.id}`} className="tl3-row-action" onClick={(e) => e.stopPropagation()}>
                          <Icon kind="more" />
                        </Link>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>

          <Pagination
            total={filteredAndSorted.length}
            page={safePage}
            pageSize={pageSize}
            onPage={setPage}
            onPageSize={setPageSize}
          />
        </article>

        <aside className="tl3-readiness-card">
          <header className="tl3-table-head">
            <h2>工具就绪度</h2>
            <Link to="/integrations" className="tl3-link-blue">
              查看全部 <Icon kind="arrow-right" />
            </Link>
          </header>

          <div className="tl3-readiness-pill">
            {readyCount}/{totalTools} 就绪
          </div>

          <div className="tl3-readiness-row">
            <ReadinessDonut
              percent={readinessPct}
              ready={readyCount}
              partial={partialCount}
              unavailable={unavailableCount}
              total={totalTools}
            />
            <ul className="tl3-readiness-legend">
              <li>
                <span className="tl3-dot tl3-dot-green" />
                就绪
                <strong>{readyCount}</strong>
              </li>
              <li>
                <span className="tl3-dot tl3-dot-amber" />
                部分可用
                <strong>{partialCount}</strong>
              </li>
              <li>
                <span className="tl3-dot tl3-dot-red" />
                不可用
                <strong>{unavailableCount}</strong>
              </li>
            </ul>
          </div>

          <div className="tl3-readiness-section-head">
            <span>关键工具</span>
            <span>状态</span>
          </div>
          <div className="tl3-readiness-list">
            {keyTools.map((tool) => {
              const r = readiness(tool);
              return (
                <div key={tool.name} className="tl3-readiness-item">
                  <div>
                    <div className="tl3-readiness-name">{tool.display_name || tool.name}</div>
                    <div className="tl3-readiness-key">{tool.name}</div>
                  </div>
                  <ReadinessPill state={r} />
                </div>
              );
            })}
            {keyTools.length === 0 ? (
              <p className="tl3-empty">无工具数据。</p>
            ) : null}
          </div>

          <div className="tl3-readiness-foot">
            <span>
              最近检查:
              {registryQ.dataUpdatedAt
                ? new Date(registryQ.dataUpdatedAt).toLocaleString()
                : "—"}
            </span>
            <button
              type="button"
              className="tl3-foot-refresh"
              onClick={() => registryQ.refetch()}
            >
              <Icon kind="refresh" />
              刷新
            </button>
          </div>
        </aside>
      </div>
    </div>
  );
}

// ---- bits --------------------------------------------------------------

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

function StatusPill({
  bucket,
  label,
}: {
  bucket: ReturnType<typeof statusBucket>;
  label: string;
}) {
  return <span className={`tl3-status-pill bucket-${bucket}`}>{label}</span>;
}

function SelectButton({
  label,
  value,
  options,
  onSelect,
}: {
  label: string;
  value: string;
  options: { label: string; value: string }[];
  onSelect: (v: string) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="tl3-selectbtn-wrap">
      <button
        type="button"
        className="tl3-selectbtn"
        onClick={() => setOpen((v) => !v)}
        onBlur={() => setTimeout(() => setOpen(false), 120)}
      >
        <div>
          <div className="tl3-selectbtn-label">{label}</div>
          <div className="tl3-selectbtn-value">{value}</div>
        </div>
        <Icon kind="chev-down" />
      </button>
      {open ? (
        <ul className="tl3-selectbtn-menu">
          {options.map((o) => (
            <li
              key={o.value}
              onMouseDown={(e) => {
                e.preventDefault();
                onSelect(o.value);
                setOpen(false);
              }}
            >
              {o.label}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function Pagination({
  total,
  page,
  pageSize,
  onPage,
  onPageSize,
}: {
  total: number;
  page: number;
  pageSize: number;
  onPage: (p: number) => void;
  onPageSize: (s: number) => void;
}) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  const pages: (number | "…")[] = useMemo(() => {
    const out: (number | "…")[] = [];
    const maxAround = 1;
    const add = (n: number) => {
      if (!out.includes(n) && n >= 1 && n <= totalPages) out.push(n);
    };
    add(1);
    for (let i = page - maxAround; i <= page + maxAround; i++) add(i);
    add(totalPages);
    // Insert ellipses
    const withGaps: (number | "…")[] = [];
    for (let i = 0; i < out.length; i++) {
      withGaps.push(out[i]);
      const next = out[i + 1];
      if (typeof next === "number" && typeof out[i] === "number" && next - (out[i] as number) > 1) {
        withGaps.push("…");
      }
    }
    return withGaps;
  }, [page, totalPages]);

  return (
    <div className="tl3-pagination">
      <div className="tl3-muted">共 {total.toLocaleString()} 条</div>
      <div className="tl3-pagination-controls">
        <button
          type="button"
          className="tl3-page-arrow"
          disabled={page <= 1}
          onClick={() => onPage(page - 1)}
        >
          <Icon kind="chev-left" />
        </button>
        {pages.map((p, idx) =>
          p === "…" ? (
            <span key={`gap-${idx}`} className="tl3-page-gap">…</span>
          ) : (
            <button
              key={p}
              type="button"
              className={`tl3-page-num${p === page ? " active" : ""}`}
              onClick={() => onPage(p)}
            >
              {p}
            </button>
          ),
        )}
        <button
          type="button"
          className="tl3-page-arrow"
          disabled={page >= totalPages}
          onClick={() => onPage(page + 1)}
        >
          <Icon kind="chev-right" />
        </button>
        <select
          className="tl3-pagesize"
          value={pageSize}
          onChange={(e) => onPageSize(Number(e.target.value))}
        >
          {PAGE_SIZES.map((s) => (
            <option key={s} value={s}>
              {s} 条/页
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}

function ReadinessDonut({
  percent,
  ready,
  partial,
  unavailable,
  total,
}: {
  percent: number;
  ready: number;
  partial: number;
  unavailable: number;
  total: number;
}) {
  const C = 2 * Math.PI * 42; // circumference for r=42
  const totalSafe = total || 1;
  const readyDash = (ready / totalSafe) * C;
  const partialDash = (partial / totalSafe) * C;
  const unavailableDash = (unavailable / totalSafe) * C;

  return (
    <div className="tl3-donut" aria-label={`整体就绪率 ${percent}%`}>
      <svg viewBox="0 0 100 100">
        <circle cx="50" cy="50" r="42" fill="none" stroke="#f1f1f1" strokeWidth="12" />
        <circle
          cx="50"
          cy="50"
          r="42"
          fill="none"
          stroke="#22c55e"
          strokeWidth="12"
          strokeDasharray={`${readyDash} ${C - readyDash}`}
          strokeDashoffset={C / 4}
          transform="rotate(-90 50 50)"
        />
        <circle
          cx="50"
          cy="50"
          r="42"
          fill="none"
          stroke="#f59e0b"
          strokeWidth="12"
          strokeDasharray={`${partialDash} ${C - partialDash}`}
          strokeDashoffset={C / 4 - readyDash}
          transform="rotate(-90 50 50)"
        />
        <circle
          cx="50"
          cy="50"
          r="42"
          fill="none"
          stroke="#ef4444"
          strokeWidth="12"
          strokeDasharray={`${unavailableDash} ${C - unavailableDash}`}
          strokeDashoffset={C / 4 - readyDash - partialDash}
          transform="rotate(-90 50 50)"
        />
        <text x="50" y="49" textAnchor="middle" className="tl3-donut-pct">{percent}%</text>
        <text x="50" y="62" textAnchor="middle" className="tl3-donut-sub">整体就绪率</text>
      </svg>
    </div>
  );
}

function ReadinessPill({ state }: { state: "ready" | "partial" | "unavailable" }) {
  if (state === "ready") {
    return (
      <span className="tl3-rpill ready">
        <Icon kind="check-pill" />
        就绪
      </span>
    );
  }
  if (state === "partial") {
    return (
      <span className="tl3-rpill partial">
        <Icon kind="clock-pill" />
        部分可用
      </span>
    );
  }
  return (
    <span className="tl3-rpill unavailable">
      <Icon kind="x-pill" />
      不可用
    </span>
  );
}
