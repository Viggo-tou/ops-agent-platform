import { useEffect, useState } from "react";

import { PermissionGuard } from "../../components/auth/PermissionGuard";
import { ModelSelector } from "../../components/settings/ModelSelector";
import { api } from "../../lib/api";

interface StageStatus {
  stage: string;
  default: string | null;
  override: string | null;
  effective: string | null;
  allowed: string[];
}

interface OverridesResp {
  stages: StageStatus[];
}

interface CliEntry {
  key: string;
  label: string;
  cli_available: boolean;
  authenticated: boolean;
  auth_path: string | null;
  login_command: string;
  notes: string;
}

interface ApiKeyEntry {
  key: string;
  label: string;
  env_var: string;
  set: boolean;
  notes: string;
}

interface AuthStatusResp {
  cli: CliEntry[];
  api_keys: ApiKeyEntry[];
}

const STAGE_LABEL: Record<string, string> = {
  planner: "Planner",
  codegen: "Codegen",
  synthesis: "Knowledge synthesis",
  cc_agent: "CC agent (deepseek/anthropic chain head)",
  primary_agent: "Primary agent (top-level fallback)",
};

const STAGE_HINT: Record<string, string> = {
  planner: "Generates the execution plan from a Jira issue or chat message.",
  codegen: "Writes the actual diff. Highest cost / latency.",
  synthesis: "Summarizes retrieved knowledge before plan generation.",
  cc_agent: "Internal coding-agent loop used for diagnostic / review steps.",
  primary_agent: "Top-level orchestrator fallback when other providers fail.",
};

// Providers that are CLI-based (need login). Drives the conditional auth panel.
const CLI_PROVIDERS = new Set(["claude_code", "codex", "gemini"]);
// Providers that are API-based (need API key).
const API_PROVIDERS_BY_KEY: Record<string, string> = {
  anthropic: "anthropic",
  openai: "openai",
  deepseek: "deepseek",
  minimax: "minimax",
};

export function SettingsPage() {
  return (
    <div className="page-shell settings-page">
      <header className="page-header">
        <div>
          <div className="page-section-label">Settings</div>
          <h1>Model & Agent Runtime</h1>
          <p className="page-subtitle">
            Pick the main model and which provider runs each pipeline stage.
            Auth status updates based on what you select.
          </p>
        </div>
      </header>

      <PermissionGuard
        permission="settings:view"
        fallback={
          <div className="permission-note">Your role cannot view system settings.</div>
        }
      >
        <SelectedModelSection />
        <PerStageSection />
      </PermissionGuard>
    </div>
  );
}

function SelectedModelSection() {
  return (
    <section className="settings-section">
      <h2>Selected model</h2>
      <p className="page-help">
        Tactical override applied to your next task. Persisted globally.
      </p>
      <ModelSelector />
    </section>
  );
}

function PerStageSection() {
  const [overrides, setOverrides] = useState<OverridesResp | null>(null);
  const [authStatus, setAuthStatus] = useState<AuthStatusResp | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);

  const reload = () => {
    Promise.all([api.getRuntimeOverrides(), api.getAuthStatus()])
      .then(([o, a]) => {
        setOverrides(o);
        setAuthStatus(a);
      })
      .catch((err) => setError(String(err.message ?? err)));
  };

  useEffect(reload, []);

  const onChange = async (stage: string, value: string) => {
    setPending(stage);
    setError(null);
    try {
      await api.patchRuntimeOverride({ stage, value: value || null });
      reload();
    } catch (e: any) {
      setError(String(e?.message ?? e));
    } finally {
      setPending(null);
    }
  };

  // Determine which auth blocks to show based on selected effective providers.
  const effectiveProviders = new Set(
    (overrides?.stages ?? []).map((s) => s.effective).filter(Boolean) as string[],
  );
  const showAnyCli = Array.from(effectiveProviders).some((p) => CLI_PROVIDERS.has(p));
  const apiProvidersInUse = new Set(
    Array.from(effectiveProviders)
      .map((p) => API_PROVIDERS_BY_KEY[p])
      .filter(Boolean),
  );

  return (
    <>
      <section className="settings-section">
        <h2>Per-stage providers</h2>
        <p className="page-help">
          Override the .env-configured provider per pipeline stage. Empty =
          use .env default. Changes apply to your next task (no restart).
        </p>
        {error ? <p className="onboarding-error">{error}</p> : null}
        {!overrides ? (
          <p className="page-help">Loading…</p>
        ) : (
          <table className="governance-table">
            <thead>
              <tr>
                <th>Stage</th>
                <th>Effective provider</th>
                <th>Override (UI)</th>
                <th>.env default</th>
              </tr>
            </thead>
            <tbody>
              {overrides.stages.map((s) => (
                <tr key={s.stage}>
                  <td>
                    <div className="cell-title">{STAGE_LABEL[s.stage] ?? s.stage}</div>
                    <div className="cell-key">{STAGE_HINT[s.stage] ?? ""}</div>
                  </td>
                  <td>
                    <code>{s.effective ?? "—"}</code>
                  </td>
                  <td>
                    <select
                      value={s.override ?? ""}
                      onChange={(e) => onChange(s.stage, e.target.value)}
                      disabled={pending === s.stage}
                    >
                      <option value="">— use .env default —</option>
                      {s.allowed.map((p) => (
                        <option key={p} value={p}>
                          {p}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <code className="cell-muted">{s.default ?? "—"}</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {authStatus ? (
        <section className="settings-section">
          <h2>Runtime authentication</h2>
          <p className="page-help">
            Based on the providers you've selected above, verify that each
            runtime is authenticated.
          </p>

          {showAnyCli ? (
            <div className="auth-block">
              <h3>CLI authentication</h3>
              {authStatus.cli.map((c) => {
                if (!effectiveProviders.has(c.key)) return null;
                return <CliRow key={c.key} entry={c} />;
              })}
            </div>
          ) : null}

          {apiProvidersInUse.size > 0 ? (
            <div className="auth-block">
              <h3>API keys</h3>
              {authStatus.api_keys.map((k) => {
                if (!apiProvidersInUse.has(k.key)) return null;
                return <ApiKeyRow key={k.key} entry={k} />;
              })}
            </div>
          ) : null}

          {!showAnyCli && apiProvidersInUse.size === 0 ? (
            <p className="page-help">
              No CLI / API auth required — the providers you've selected don't
              need explicit credentials (e.g. mock / auto / ollama).
            </p>
          ) : null}
        </section>
      ) : null}
    </>
  );
}

function CliRow({ entry }: { entry: CliEntry }) {
  const ok = entry.authenticated && entry.cli_available;
  return (
    <div className={`auth-row${ok ? " ok" : " warn"}`}>
      <div className="auth-row-head">
        <strong>{entry.label}</strong>
        <span className={`pill ${ok ? "decision-allow" : "decision-deny"}`}>
          {ok ? "Logged in" : entry.cli_available ? "Not authenticated" : "CLI not installed"}
        </span>
      </div>
      {entry.auth_path ? (
        <code className="auth-row-path">{entry.auth_path}</code>
      ) : null}
      {entry.notes ? <p className="auth-row-notes">{entry.notes}</p> : null}
      {!ok ? (
        <p className="auth-row-cmd">
          Run in terminal: <code>{entry.login_command}</code>
        </p>
      ) : null}
    </div>
  );
}

function ApiKeyRow({ entry }: { entry: ApiKeyEntry }) {
  return (
    <div className={`auth-row${entry.set ? " ok" : " warn"}`}>
      <div className="auth-row-head">
        <strong>{entry.label}</strong>
        <span className={`pill ${entry.set ? "decision-allow" : "decision-deny"}`}>
          {entry.set ? "Key set" : "Not set"}
        </span>
      </div>
      <p className="auth-row-cmd">
        Set <code>{entry.env_var}</code> in <code>apps/backend/.env</code> and restart.
      </p>
    </div>
  );
}
