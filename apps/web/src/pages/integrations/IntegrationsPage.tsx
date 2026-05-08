import { useEffect, useState } from "react";

import { api } from "../../lib/api";

interface Integration {
  key: string;
  label: string;
  description: string;
  category: string;
  configured: boolean;
  status: "connected" | "not_configured" | "coming_soon";
  config_hint: string;
}

interface IntegrationsResp {
  integrations: Integration[];
}

const ICON: Record<string, JSX.Element> = {
  github: (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M12 .5C5.65.5.5 5.66.5 12.02c0 5.08 3.29 9.4 7.86 10.92.58.1.79-.25.79-.56v-2.13c-3.2.7-3.87-1.36-3.87-1.36-.52-1.32-1.27-1.67-1.27-1.67-1.04-.71.08-.7.08-.7 1.15.08 1.76 1.18 1.76 1.18 1.02 1.75 2.69 1.24 3.34.95.1-.74.4-1.24.73-1.53-2.55-.29-5.23-1.28-5.23-5.69 0-1.26.45-2.28 1.18-3.08-.12-.29-.51-1.46.11-3.04 0 0 .96-.31 3.15 1.18a10.92 10.92 0 0 1 5.74 0c2.19-1.49 3.15-1.18 3.15-1.18.62 1.58.23 2.75.11 3.04.74.8 1.18 1.82 1.18 3.08 0 4.42-2.69 5.39-5.25 5.68.41.36.78 1.05.78 2.13v3.16c0 .31.21.67.8.56A11.52 11.52 0 0 0 23.5 12.02C23.5 5.66 18.35.5 12 .5Z" />
    </svg>
  ),
  jira: (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M11.53 2 12 11.47l-3.53.04L4 7l3.5-3.5h3l1.03-1.5Zm.94 20-.47-9.47 3.53-.04L20 17l-3.5 3.5h-3l-1.03 1.5Z" />
    </svg>
  ),
  slack: (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M5.04 15.3a2.04 2.04 0 1 1-2.04-2.04h2.04Zm1.02 0a2.04 2.04 0 1 1 4.08 0v5.1a2.04 2.04 0 1 1-4.08 0Z" />
    </svg>
  ),
  internal_api: (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M12 2 2 7l10 5 10-5-10-5Zm0 7L2 14l10 5 10-5-10-5Z" />
    </svg>
  ),
  internal_db: (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <ellipse cx="12" cy="5" rx="8" ry="3" />
      <path d="M4 5v6c0 1.66 3.58 3 8 3s8-1.34 8-3V5M4 11v6c0 1.66 3.58 3 8 3s8-1.34 8-3v-6" />
    </svg>
  ),
  teams: (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <rect x="3" y="6" width="14" height="12" rx="2" />
      <circle cx="20" cy="9" r="3" />
    </svg>
  ),
};

const STATUS_PILL: Record<Integration["status"], string> = {
  connected: "decision-allow",
  not_configured: "risk-medium",
  coming_soon: "soon",
};

const STATUS_LABEL: Record<Integration["status"], string> = {
  connected: "Connected",
  not_configured: "Not configured",
  coming_soon: "Coming 1.1",
};

export function IntegrationsPage() {
  const [resp, setResp] = useState<IntegrationsResp | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api
      .getIntegrationStatus()
      .then((data) => {
        setResp(data);
        setLoading(false);
      })
      .catch((err) => {
        setError(String(err.message ?? err));
        setLoading(false);
      });
  }, []);

  const counts = (resp?.integrations ?? []).reduce(
    (acc, i) => {
      acc[i.status]++;
      return acc;
    },
    { connected: 0, not_configured: 0, coming_soon: 0 } as Record<
      Integration["status"],
      number
    >,
  );

  return (
    <div className="page-shell integrations-page">
      <header className="page-header">
        <div>
          <div className="page-section-label">Integrations</div>
          <h1>External tool connections</h1>
          <p className="page-subtitle">
            Optional integrations for workflow / chat / data / version-control.
            Configure via backend <code>.env</code> for 1.0; in-UI OAuth ships
            in 1.1.
          </p>
        </div>
        <div className="governance-summary">
          <div className="governance-stat">
            <span className="num">{counts.connected}</span>
            <span className="label">connected</span>
          </div>
          <div className="governance-stat">
            <span className="num">{counts.not_configured}</span>
            <span className="label">not configured</span>
          </div>
          <div className="governance-stat">
            <span className="num">{counts.coming_soon}</span>
            <span className="label">coming soon</span>
          </div>
        </div>
      </header>

      {error ? <p className="onboarding-error">Failed to load: {error}</p> : null}
      {loading ? <p className="page-help">Loading…</p> : null}

      {resp ? (
        <div className="integration-grid">
          {resp.integrations.map((i) => (
            <article
              key={i.key}
              className={`integration-card status-${i.status}`}
            >
              <header className="integration-card-head">
                <div className="integration-icon">{ICON[i.key] ?? null}</div>
                <div>
                  <h3>{i.label}</h3>
                  <span className={`pill ${STATUS_PILL[i.status]}`}>
                    {STATUS_LABEL[i.status]}
                  </span>
                </div>
              </header>
              <p className="integration-desc">{i.description}</p>
              {i.config_hint ? (
                <p className="integration-hint">
                  <strong>Setup:</strong> {i.config_hint}
                </p>
              ) : null}
            </article>
          ))}
        </div>
      ) : null}
    </div>
  );
}
