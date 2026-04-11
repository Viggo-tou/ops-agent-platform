import { titleCase } from "../../lib/format";
import type { ToolRegistryEntry } from "../../types";

function permissionClass(permission: ToolRegistryEntry["permission_category"]): string {
  if (permission === "approval_required") {
    return "registry-permission-approval_required";
  }
  if (permission === "write") {
    return "registry-permission-write";
  }
  return "registry-permission-read_only";
}

interface ToolRegistryPanelProps {
  tools: ToolRegistryEntry[];
  compact?: boolean;
}

export function ToolRegistryPanel({ tools, compact = false }: ToolRegistryPanelProps) {
  const enabledCount = tools.filter((tool) => tool.enabled).length;

  return (
    <section className="detail-card">
      <div className="section-header">
        <div>
          <div className="eyebrow">Tool Registry</div>
          <h3>{compact ? "Connector Readiness" : "Tool Runtime Readiness"}</h3>
        </div>
        <span className="mini-pill">
          {enabledCount}/{tools.length} ready
        </span>
      </div>

      <div className={compact ? "registry-grid compact" : "registry-grid"}>
        {tools.map((tool) => (
          <article className="registry-card" key={tool.name}>
            <div className="registry-card-head">
              <div>
                <strong>{tool.display_name}</strong>
                <div className="muted-text">{tool.name}</div>
              </div>
              <span className={`mini-pill ${tool.enabled ? "registry-ready" : "registry-disabled"}`}>
                {tool.enabled ? "Ready" : "Blocked"}
              </span>
            </div>

            <p className="compact-line">{tool.description}</p>

            <div className="button-row">
              <span className={`mini-pill ${permissionClass(tool.permission_category)}`}>
                {titleCase(tool.permission_category)}
              </span>
              <span className="mini-pill">{tool.provider_name}</span>
              <span className="mini-pill">{tool.retry_count + 1} attempt max</span>
            </div>

            <p className="muted-text compact-line">{tool.status_message}</p>

            {tool.missing_configuration.length > 0 ? (
              <div className="registry-missing">
                Missing: {tool.missing_configuration.join(", ")}
              </div>
            ) : null}
          </article>
        ))}
      </div>
    </section>
  );
}
