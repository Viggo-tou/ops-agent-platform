import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../../lib/api";

const WINDOWS = [
  { label: "近 7 天", value: 7 },
  { label: "近 14 天", value: 14 },
  { label: "近 30 天", value: 30 },
  { label: "近 90 天", value: 90 },
] as const;

function formatTokens(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)} B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)} M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)} k`;
  return String(n);
}

function formatUsd(n: number): string {
  if (n === 0) return "$0.00";
  if (n < 0.01) return `$${n.toFixed(4)}`;
  if (n < 1) return `$${n.toFixed(3)}`;
  return `$${n.toFixed(2)}`;
}

function Icon({ kind, className }: { kind: string; className?: string }) {
  const props = { className, viewBox: "0 0 24 24", "aria-hidden": true } as const;
  switch (kind) {
    case "pulse":
      return <svg {...props}><path d="M3 12h4l3-9 4 18 3-9h4" /></svg>;
    case "stack":
      return <svg {...props}><path d="m12 2 9 5-9 5-9-5 9-5ZM3 12l9 5 9-5M3 17l9 5 9-5" /></svg>;
    case "dollar":
      return <svg {...props}><path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" /></svg>;
    case "spark":
      return <svg {...props}><path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.5 5.5l2.8 2.8M15.7 15.7l2.8 2.8M5.5 18.5l2.8-2.8M15.7 8.3l2.8-2.8" /><circle cx="12" cy="12" r="3" /></svg>;
    case "chev-down":
      return <svg {...props}><path d="m6 9 6 6 6-6" /></svg>;
    default:
      return null;
  }
}

export function UsagePage() {
  const [windowDays, setWindowDays] =
    useState<(typeof WINDOWS)[number]["value"]>(30);

  const statsQ = useQuery({
    queryKey: ["llm-usage", "stats", windowDays],
    queryFn: () => api.getLlmUsageStats(windowDays, 20),
    refetchInterval: 60_000,
  });
  const tsQ = useQuery({
    queryKey: ["llm-usage", "timeseries", windowDays],
    queryFn: () => api.getLlmUsageTimeseries(Math.min(windowDays, 60)),
  });

  const stats = statsQ.data;
  const points = tsQ.data?.points ?? [];

  const maxTokens = points.length > 0 ? Math.max(...points.map((p) => p.total_tokens), 1) : 1;

  return (
    <div className="page-shell usage3-page">
      <section className="dash3-hero">
        <div className="dash3-hero-top">
          <div>
            <div className="page-section-eyebrow">用量与成本</div>
            <h1>模型 Token 用量</h1>
            <p className="dash3-hero-subtitle">
              统计指定窗口内各模型的 token 消耗与估算成本。数据来源:
              <code className="usage3-inline-code">llm_usage</code>{" "}
              表,在每次模型调用后由后端持久化。
            </p>
          </div>
          <div className="usage3-window-wrap">
            <span className="usage3-window-label">窗口</span>
            <select
              className="usage3-window-select"
              value={windowDays}
              onChange={(e) => setWindowDays(Number(e.target.value) as any)}
            >
              {WINDOWS.map((w) => (
                <option key={w.value} value={w.value}>
                  {w.label}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div className="usage3-kpi-grid">
          <KpiCard
            iconKind="pulse"
            tone="blue"
            title="调用次数"
            value={stats ? stats.total_invocations.toLocaleString() : "—"}
            subtitle={`窗口 ${windowDays} 天`}
          />
          <KpiCard
            iconKind="stack"
            tone="purple"
            title="总 Tokens"
            value={stats ? formatTokens(stats.total_tokens) : "—"}
            subtitle={
              stats
                ? `输入 ${formatTokens(stats.total_input_tokens)} · 输出 ${formatTokens(stats.total_output_tokens)}`
                : "—"
            }
          />
          <KpiCard
            iconKind="dollar"
            tone="green"
            title="估算成本"
            value={stats ? formatUsd(stats.total_cost_usd) : "—"}
            subtitle="按 provider 单价估算"
          />
          <KpiCard
            iconKind="spark"
            tone="orange"
            title="活跃模型"
            value={stats ? String(stats.by_model.length) : "—"}
            subtitle="本窗口内有调用"
          />
        </div>
      </section>

      <section className="usage3-chart-card">
        <header className="tl3-table-head">
          <h2>每日 Token 用量</h2>
          <span className="tl3-muted">{points.length} 个数据点</span>
        </header>
        {tsQ.isLoading ? (
          <p className="tl3-empty">加载中…</p>
        ) : points.length === 0 ? (
          <p className="tl3-empty">该窗口内暂无数据。</p>
        ) : (
          <div className="usage3-chart">
            <div className="usage3-chart-bars">
              {points.map((p) => {
                const h = Math.max(2, Math.round((p.total_tokens / maxTokens) * 100));
                return (
                  <div
                    key={p.date}
                    className="usage3-chart-bar"
                    title={`${p.date} · ${formatTokens(p.total_tokens)} tokens · ${formatUsd(p.total_cost_usd)}`}
                  >
                    <div
                      className="usage3-chart-fill"
                      style={{ height: `${h}%` }}
                    />
                  </div>
                );
              })}
            </div>
            <div className="usage3-chart-axis">
              <span>{points[0]?.date}</span>
              <span>{points[points.length - 1]?.date}</span>
            </div>
          </div>
        )}
      </section>

      <section className="usage3-tables-grid">
        <article className="usage3-table-card">
          <header className="tl3-table-head">
            <h2>按模型</h2>
            <span className="tl3-muted">前 20 名</span>
          </header>
          <div className="tl3-table-wrap">
            <table className="tl3-table">
              <thead>
                <tr>
                  <th>模型</th>
                  <th>调用</th>
                  <th>输入</th>
                  <th>输出</th>
                  <th>合计</th>
                  <th>成本</th>
                </tr>
              </thead>
              <tbody>
                {(stats?.by_model ?? []).length === 0 && !statsQ.isLoading ? (
                  <tr>
                    <td colSpan={6} className="tl3-empty">无模型用量数据。</td>
                  </tr>
                ) : (
                  (stats?.by_model ?? []).map((m) => (
                    <tr key={`${m.provider_name}/${m.model_name}`}>
                      <td>
                        <div className="tl3-task-name">{m.model_name}</div>
                        <div className="tl3-task-id">{m.provider_name}</div>
                      </td>
                      <td className="tl3-cell-muted">{m.invocations.toLocaleString()}</td>
                      <td className="tl3-cell-muted">{formatTokens(m.input_tokens)}</td>
                      <td className="tl3-cell-muted">{formatTokens(m.output_tokens)}</td>
                      <td>
                        <strong>{formatTokens(m.total_tokens)}</strong>
                      </td>
                      <td className="tl3-cell-muted">{formatUsd(m.estimated_cost_usd)}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </article>

        <article className="usage3-table-card usage3-table-card-narrow">
          <header className="tl3-table-head">
            <h2>按用途</h2>
          </header>
          <div className="tl3-table-wrap">
            <table className="tl3-table">
              <thead>
                <tr>
                  <th>Purpose</th>
                  <th>调用</th>
                  <th>合计</th>
                  <th>成本</th>
                </tr>
              </thead>
              <tbody>
                {(stats?.by_purpose ?? []).length === 0 && !statsQ.isLoading ? (
                  <tr>
                    <td colSpan={4} className="tl3-empty">无 purpose 数据。</td>
                  </tr>
                ) : (
                  (stats?.by_purpose ?? []).map((p) => (
                    <tr key={p.purpose}>
                      <td>{p.purpose}</td>
                      <td className="tl3-cell-muted">{p.invocations.toLocaleString()}</td>
                      <td>
                        <strong>{formatTokens(p.total_tokens)}</strong>
                      </td>
                      <td className="tl3-cell-muted">{formatUsd(p.estimated_cost_usd)}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </article>
      </section>
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
  value: string;
  subtitle: string;
}) {
  return (
    <div className="tl3-kpi-card">
      <div className={`tl3-kpi-iconwrap tone-${tone}`}>
        <Icon kind={iconKind} />
      </div>
      <div className="tl3-kpi-title">{title}</div>
      <div className="tl3-kpi-value">{value}</div>
      <div className="tl3-kpi-subtitle">{subtitle}</div>
    </div>
  );
}
