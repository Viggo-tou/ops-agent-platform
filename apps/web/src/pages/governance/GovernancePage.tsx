import { useEffect, useMemo, useState } from "react";

import { api } from "../../lib/api";

interface RbacRole {
  role_key: string;
  display_name: string;
  description: string;
  is_human: boolean;
  is_system: boolean;
  is_active: boolean;
}

interface PolicyRule {
  id: string;
  rule_key: string;
  title: string;
  description: string;
  subject_role: string;
  resource_type: string;
  action_key: string;
  tool_name: string | null;
  decision: string;
  risk_level: string;
  risk_category: string;
  required_approver_role: string | null;
  priority: number;
  is_active: boolean;
}

const DECISION_PILL: Record<string, string> = {
  allow: "decision-allow",
  deny: "decision-deny",
  require_approval: "decision-approval",
};

const RISK_PILL: Record<string, string> = {
  low: "risk-low",
  medium: "risk-medium",
  high: "risk-high",
  critical: "risk-critical",
};

export function GovernancePage() {
  const [roles, setRoles] = useState<RbacRole[]>([]);
  const [rules, setRules] = useState<PolicyRule[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filterDecision, setFilterDecision] = useState<string>("");
  const [filterRisk, setFilterRisk] = useState<string>("");

  useEffect(() => {
    Promise.all([api.listRbacRoles(), api.listPolicyRules()])
      .then(([rolesResp, rulesResp]) => {
        setRoles(rolesResp);
        setRules(rulesResp);
        setLoading(false);
      })
      .catch((err) => {
        setError(String(err.message ?? err));
        setLoading(false);
      });
  }, []);

  const filteredRules = useMemo(() => {
    return rules.filter((r) => {
      if (filterDecision && r.decision !== filterDecision) return false;
      if (filterRisk && r.risk_level !== filterRisk) return false;
      return true;
    });
  }, [rules, filterDecision, filterRisk]);

  const decisionCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const r of rules) counts[r.decision] = (counts[r.decision] ?? 0) + 1;
    return counts;
  }, [rules]);

  return (
    <div className="page-shell governance-page">
      <header className="page-header">
        <div>
          <div className="page-section-label">Governance & Risk Control</div>
          <h1>Policy & Roles</h1>
          <p className="page-subtitle">
            Read-only view of RBAC roles and policy rules. Edits ship in 1.1.
          </p>
        </div>
        <div className="governance-summary">
          <div className="governance-stat">
            <span className="num">{roles.filter((r) => r.is_active).length}</span>
            <span className="label">active roles</span>
          </div>
          <div className="governance-stat">
            <span className="num">{rules.length}</span>
            <span className="label">policy rules</span>
          </div>
          <div className="governance-stat">
            <span className="num">{decisionCounts.require_approval ?? 0}</span>
            <span className="label">approval-gated</span>
          </div>
          <div className="governance-stat">
            <span className="num">{decisionCounts.deny ?? 0}</span>
            <span className="label">blocked</span>
          </div>
        </div>
      </header>

      {error ? <p className="onboarding-error">Failed to load: {error}</p> : null}

      {loading ? (
        <p className="page-help">Loading…</p>
      ) : (
        <>
          <section className="governance-section">
            <h2>RBAC Roles</h2>
            <p className="page-help">
              Who can do what in the platform. Configured via{" "}
              <code>governance.bootstrap_governance_data</code> on backend
              startup.
            </p>
            <table className="governance-table">
              <thead>
                <tr>
                  <th>Role</th>
                  <th>Display name</th>
                  <th>Description</th>
                  <th>Type</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {roles.map((r) => (
                  <tr key={r.role_key}>
                    <td>
                      <code>{r.role_key}</code>
                    </td>
                    <td>{r.display_name}</td>
                    <td className="cell-desc">{r.description}</td>
                    <td>
                      {r.is_system ? (
                        <span className="pill soon">system</span>
                      ) : r.is_human ? (
                        <span className="pill selected">human</span>
                      ) : (
                        <span className="pill soon">agent</span>
                      )}
                    </td>
                    <td>
                      {r.is_active ? (
                        <span className="pill active">active</span>
                      ) : (
                        <span className="pill soon">inactive</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          <section className="governance-section">
            <h2>Policy Rules</h2>
            <p className="page-help">
              Decision rules evaluated when an actor invokes a tool / action.
              Filter by decision or risk level.
            </p>
            <div className="governance-filters">
              <label>
                Decision:&nbsp;
                <select
                  value={filterDecision}
                  onChange={(e) => setFilterDecision(e.target.value)}
                >
                  <option value="">all</option>
                  <option value="allow">allow</option>
                  <option value="require_approval">require_approval</option>
                  <option value="deny">deny</option>
                </select>
              </label>
              <label>
                Risk:&nbsp;
                <select
                  value={filterRisk}
                  onChange={(e) => setFilterRisk(e.target.value)}
                >
                  <option value="">all</option>
                  <option value="low">low</option>
                  <option value="medium">medium</option>
                  <option value="high">high</option>
                  <option value="critical">critical</option>
                </select>
              </label>
              <span className="governance-filter-count">
                {filteredRules.length} of {rules.length}
              </span>
            </div>
            <table className="governance-table">
              <thead>
                <tr>
                  <th>Rule</th>
                  <th>Subject</th>
                  <th>Action / Tool</th>
                  <th>Decision</th>
                  <th>Risk</th>
                  <th>Approver</th>
                </tr>
              </thead>
              <tbody>
                {filteredRules.map((r) => (
                  <tr key={r.id}>
                    <td>
                      <div className="cell-title">{r.title}</div>
                      <div className="cell-key">{r.rule_key}</div>
                    </td>
                    <td>
                      <code>{r.subject_role}</code>
                    </td>
                    <td>
                      <code>
                        {r.tool_name || `${r.resource_type}:${r.action_key}`}
                      </code>
                    </td>
                    <td>
                      <span className={`pill ${DECISION_PILL[r.decision] ?? ""}`}>
                        {r.decision}
                      </span>
                    </td>
                    <td>
                      <span className={`pill ${RISK_PILL[r.risk_level] ?? ""}`}>
                        {r.risk_level}
                      </span>
                    </td>
                    <td>
                      {r.required_approver_role ? (
                        <code>{r.required_approver_role}</code>
                      ) : (
                        <span className="cell-muted">—</span>
                      )}
                    </td>
                  </tr>
                ))}
                {filteredRules.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="cell-muted">
                      No rules match the current filter.
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </section>
        </>
      )}
    </div>
  );
}
