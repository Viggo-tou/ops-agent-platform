import { useEffect, useState } from "react";

import { api } from "../../lib/api";

type McpServer = {
  name: string;
  connected: boolean;
  error: string | null;
  tool_count: number;
  tools: { name: string; description: string; input_schema: Record<string, unknown> }[];
};

type ToolEntry = {
  name: string;
  display_name: string;
  description: string;
  provider_name: string;
  permission_category: string;
  enabled: boolean;
  status_message: string;
  missing_configuration: string[];
  requires_network: boolean;
  timeout_seconds: number;
  retry_count: number;
  tags: string[];
};

type UsageEntry = {
  tool_name: string;
  total: number;
  succeeded: number;
  failed: number;
  success_rate: number;
};

type UsageStats = {
  total_invocations: number;
  succeeded: number;
  failed: number;
  success_rate: number;
  window_days: number;
  by_tool: UsageEntry[];
};

export function SkillsPage() {
  const [servers, setServers] = useState<McpServer[] | null>(null);
  const [tools, setTools] = useState<ToolEntry[] | null>(null);
  const [usage, setUsage] = useState<UsageStats | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      api.getMcpServers(),
      api.getToolRegistry(),
      api.getToolUsageStats(7, 12),
    ])
      .then(([s, t, u]) => {
        if (cancelled) return;
        setServers(s);
        setTools(t as ToolEntry[]);
        setUsage(u as UsageStats);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(String(err?.message ?? err));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const connectedServers = servers?.filter((s) => s.connected) ?? [];
  const failedServers = servers?.filter((s) => !s.connected) ?? [];
  const mcpTools = tools?.filter((t) => t.name.startsWith("mcp.")) ?? [];
  const builtinTools = tools?.filter((t) => !t.name.startsWith("mcp.")) ?? [];

  return (
    <div className="page-shell skills-page">
      <header className="page-header">
        <div>
          <div className="page-section-label">Skills</div>
          <h1>工具与 MCP 服务器</h1>
          <p className="page-subtitle">
            chat / pipeline 可调用的所有工具,以及连接的 MCP 服务器状态。
            通过 <code>OPS_AGENT_MCP_SERVERS_JSON</code> 环境变量配置 MCP server
            (格式与 Claude Desktop 一致)。
          </p>
        </div>
        <div className="governance-summary">
          <div className="governance-stat">
            <span className="num">{connectedServers.length}</span>
            <span className="label">MCP 已连接</span>
          </div>
          <div className="governance-stat">
            <span className="num">{tools?.length ?? 0}</span>
            <span className="label">工具总数</span>
          </div>
          <div className="governance-stat">
            <span className="num">{usage?.total_invocations ?? 0}</span>
            <span className="label">近 7 天调用</span>
          </div>
        </div>
      </header>

      {error ? <p className="onboarding-error">加载失败: {error}</p> : null}

      <section className="page-section">
        <header className="page-section-header">
          <h2>MCP 服务器</h2>
          <span className="page-section-meta">
            {servers === null
              ? "loading…"
              : servers.length === 0
                ? "未配置任何 MCP server"
                : `${connectedServers.length} 个已连接 · ${failedServers.length} 个失败`}
          </span>
        </header>
        {servers && servers.length === 0 ? (
          <p className="page-help">
            在后端 <code>.env</code> 设置 <code>OPS_AGENT_MCP_SERVERS_JSON</code>。例:
            <br />
            <code>{`{"filesystem":{"command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","D:/path"]}}`}</code>
          </p>
        ) : null}
        {servers && servers.length > 0 ? (
          <div className="skills-mcp-grid">
            {servers.map((server) => (
              <article
                key={server.name}
                className={`skills-mcp-card ${server.connected ? "connected" : "failed"}`}
              >
                <header>
                  <h3>{server.name}</h3>
                  <span className={`pill ${server.connected ? "decision-allow" : "risk-high"}`}>
                    {server.connected ? "Connected" : "Failed"}
                  </span>
                </header>
                {server.error ? (
                  <p className="skills-mcp-error">{server.error}</p>
                ) : null}
                <p className="skills-mcp-meta">
                  {server.tool_count} 个工具
                </p>
                {server.tools.length > 0 ? (
                  <ul className="skills-mcp-tool-list">
                    {server.tools.map((t) => (
                      <li key={t.name}>
                        <code>{t.name}</code>
                        {t.description ? (
                          <span className="skills-mcp-tool-desc">{t.description}</span>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </article>
            ))}
          </div>
        ) : null}
      </section>

      <section className="page-section">
        <header className="page-section-header">
          <h2>工具注册表</h2>
          <span className="page-section-meta">
            {builtinTools.length} 内置 · {mcpTools.length} MCP
          </span>
        </header>
        {tools === null ? (
          <p className="page-help">loading…</p>
        ) : (
          <table className="skills-tool-table">
            <thead>
              <tr>
                <th>名称</th>
                <th>provider</th>
                <th>权限</th>
                <th>状态</th>
                <th>超时</th>
              </tr>
            </thead>
            <tbody>
              {[...builtinTools, ...mcpTools].map((t) => (
                <tr key={t.name} className={t.enabled ? "" : "disabled"}>
                  <td>
                    <code>{t.name}</code>
                    {t.description ? (
                      <div className="skills-tool-desc">{t.description}</div>
                    ) : null}
                  </td>
                  <td><code>{t.provider_name}</code></td>
                  <td>
                    <span className={`pill perm-${t.permission_category.toLowerCase()}`}>
                      {t.permission_category}
                    </span>
                  </td>
                  <td>
                    {t.enabled ? (
                      <span className="pill decision-allow">就绪</span>
                    ) : (
                      <span className="pill risk-medium">缺配置</span>
                    )}
                  </td>
                  <td>{t.timeout_seconds}s</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="page-section">
        <header className="page-section-header">
          <h2>近 7 天调用</h2>
          <span className="page-section-meta">
            {usage === null
              ? "loading…"
              : `成功率 ${(usage.success_rate * 100).toFixed(1)}% (${usage.succeeded}/${usage.total_invocations})`}
          </span>
        </header>
        {usage && usage.by_tool.length > 0 ? (
          <table className="skills-tool-table">
            <thead>
              <tr>
                <th>工具</th>
                <th>调用</th>
                <th>成功</th>
                <th>失败</th>
                <th>成功率</th>
              </tr>
            </thead>
            <tbody>
              {usage.by_tool.map((row) => (
                <tr key={row.tool_name}>
                  <td><code>{row.tool_name}</code></td>
                  <td>{row.total}</td>
                  <td>{row.succeeded}</td>
                  <td>{row.failed}</td>
                  <td>{(row.success_rate * 100).toFixed(1)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : usage ? (
          <p className="page-help">最近没有工具调用记录。</p>
        ) : null}
      </section>
    </div>
  );
}
