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
    <div className="page-shell usage-page">
      <header className="usage-header">
        <div>
          <div className="page-section-eyebrow">用量与成本</div>
          <h1>模型 Token 用量</h1>
          <p className="usage-subtitle">
            统计指定窗口内各模型的 token 消耗与估算成本。数据来源:
            <code>llm_usage</code> 表,在每次模型调用后由后端持久化。
          </p>
        </div>
        <div className="usage-actions">
          <select
            className="usage-window-select"
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
      </header>

      <section className="usage-stat-grid">
        <UsageStat
          label="调用次数"
          value={stats ? stats.total_invocations.toLocaleString() : "—"}
        />
        <UsageStat
          label="总 Tokens"
          value={stats ? formatTokens(stats.total_tokens) : "—"}
          hint={
            stats
              ? `输入 ${formatTokens(stats.total_input_tokens)} / 输出 ${formatTokens(stats.total_output_tokens)}`
              : undefined
          }
        />
        <UsageStat
          label="估算成本"
          value={stats ? formatUsd(stats.total_cost_usd) : "—"}
          hint="按 provider 单价估算,仅供参考"
        />
        <UsageStat
          label="活跃模型"
          value={stats ? String(stats.by_model.length) : "—"}
          hint={`窗口 ${windowDays} 天`}
        />
      </section>

      <section className="usage-chart-card">
        <header className="usage-card-head">
          <h2>每日 Token 用量</h2>
          <span className="tl-link-muted">{points.length} 个数据点</span>
        </header>
        {tsQ.isLoading ? (
          <p className="tl-empty">加载中…</p>
        ) : points.length === 0 ? (
          <p className="tl-empty">该窗口内暂无数据。</p>
        ) : (
          <div className="usage-chart">
            <div className="usage-chart-bars">
              {points.map((p) => {
                const h = Math.max(2, Math.round((p.total_tokens / maxTokens) * 100));
                return (
                  <div
                    key={p.date}
                    className="usage-chart-bar"
                    title={`${p.date} · ${formatTokens(p.total_tokens)} tokens · ${formatUsd(p.total_cost_usd)}`}
                  >
                    <div
                      className="usage-chart-fill"
                      style={{ height: `${h}%` }}
                    />
                  </div>
                );
              })}
            </div>
            <div className="usage-chart-axis">
              <span>{points[0]?.date}</span>
              <span>{points[points.length - 1]?.date}</span>
            </div>
          </div>
        )}
      </section>

      <section className="usage-tables-grid">
        <article className="usage-table-card">
          <header className="usage-card-head">
            <h2>按模型</h2>
            <span className="tl-link-muted">前 20 名</span>
          </header>
          <div className="usage-table">
            <div className="usage-table-row usage-table-head">
              <div>模型</div>
              <div>调用</div>
              <div>输入</div>
              <div>输出</div>
              <div>合计</div>
              <div>成本</div>
            </div>
            {(stats?.by_model ?? []).map((m) => (
              <div key={`${m.provider_name}/${m.model_name}`} className="usage-table-row">
                <div>
                  <strong>{m.model_name}</strong>
                  <span className="usage-row-sub">{m.provider_name}</span>
                </div>
                <div>{m.invocations.toLocaleString()}</div>
                <div>{formatTokens(m.input_tokens)}</div>
                <div>{formatTokens(m.output_tokens)}</div>
                <div>
                  <strong>{formatTokens(m.total_tokens)}</strong>
                </div>
                <div>{formatUsd(m.estimated_cost_usd)}</div>
              </div>
            ))}
            {(stats?.by_model ?? []).length === 0 && !statsQ.isLoading ? (
              <p className="tl-empty">无模型用量数据。</p>
            ) : null}
          </div>
        </article>

        <article className="usage-table-card usage-table-card-narrow">
          <header className="usage-card-head">
            <h2>按用途</h2>
          </header>
          <div className="usage-table">
            <div className="usage-table-row usage-table-head usage-table-row-narrow">
              <div>Purpose</div>
              <div>调用</div>
              <div>合计</div>
              <div>成本</div>
            </div>
            {(stats?.by_purpose ?? []).map((p) => (
              <div key={p.purpose} className="usage-table-row usage-table-row-narrow">
                <div>{p.purpose}</div>
                <div>{p.invocations.toLocaleString()}</div>
                <div>{formatTokens(p.total_tokens)}</div>
                <div>{formatUsd(p.estimated_cost_usd)}</div>
              </div>
            ))}
            {(stats?.by_purpose ?? []).length === 0 && !statsQ.isLoading ? (
              <p className="tl-empty">无 purpose 数据。</p>
            ) : null}
          </div>
        </article>
      </section>
    </div>
  );
}

function UsageStat({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div className="usage-stat-card">
      <div className="usage-stat-label">{label}</div>
      <div className="usage-stat-value">{value}</div>
      {hint ? <div className="usage-stat-hint">{hint}</div> : null}
    </div>
  );
}
