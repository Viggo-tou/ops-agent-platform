import { useEffect, useState } from "react";

import { api } from "../../lib/api";

interface RepoSource {
  name: string;
  path: string;
  description: string;
  is_active: boolean;
}

interface SourcesResp {
  sources: RepoSource[];
  active: string;
  multi_source_enabled: boolean;
}

export function RepositoriesPage() {
  const [resp, setResp] = useState<SourcesResp | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api
      .listRepositorySources()
      .then((data) => {
        setResp(data);
        setLoading(false);
      })
      .catch((err) => {
        setError(String(err.message ?? err));
        setLoading(false);
      });
  }, []);

  return (
    <div className="page-shell repositories-page">
      <header className="page-header">
        <div>
          <div className="page-section-label">Repository Setup</div>
          <h1>Knowledge sources</h1>
          <p className="page-subtitle">
            Sources the agent can index, search, and edit. Configured via{" "}
            <code>OPS_AGENT_KNOWLEDGE_SOURCE_SPECS</code> in the backend{" "}
            <code>.env</code>.
          </p>
        </div>
        {resp ? (
          <div className="repo-summary">
            <div className="governance-stat">
              <span className="num">{resp.sources.length}</span>
              <span className="label">sources</span>
            </div>
            <div className="governance-stat">
              <span className="num">{resp.multi_source_enabled ? "yes" : "no"}</span>
              <span className="label">multi-source</span>
            </div>
          </div>
        ) : null}
      </header>

      {error ? <p className="onboarding-error">Failed to load: {error}</p> : null}
      {loading ? <p className="page-help">Loading…</p> : null}

      {resp && resp.sources.length === 0 ? (
        <div className="repo-empty">
          <h2>No sources configured</h2>
          <p className="page-help">
            Add a source in your backend <code>.env</code>:
          </p>
          <pre className="repo-env-example">
{`# Single source:
OPS_AGENT_KNOWLEDGE_SOURCE_NAME=myrepo
OPS_AGENT_KNOWLEDGE_SOURCE_PATH=D:\\projects\\myrepo

# Multi-source (semicolon-separated, name=path|description):
OPS_AGENT_KNOWLEDGE_SOURCE_SPECS=myrepo=D:\\projects\\myrepo|description text;other=D:\\projects\\other|...`}
          </pre>
          <p className="page-help">
            Restart the backend after editing <code>.env</code>.
          </p>
        </div>
      ) : null}

      {resp && resp.sources.length > 0 ? (
        <ul className="repo-list">
          {resp.sources.map((src) => (
            <li
              key={src.name}
              className={`repo-row${src.is_active ? " active" : ""}`}
            >
              <div className="repo-row-head">
                <strong>{src.name}</strong>
                {src.is_active ? (
                  <span className="pill active">active</span>
                ) : (
                  <span className="pill soon">inactive</span>
                )}
              </div>
              <code className="repo-row-path">{src.path}</code>
              {src.description ? (
                <p className="repo-row-desc">{src.description}</p>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}

      {resp && resp.multi_source_enabled ? (
        <section className="repo-switch-help">
          <h2>How to switch the active source</h2>
          <p className="page-help">
            1.0 limitation: switching the active source requires editing{" "}
            <code>OPS_AGENT_KNOWLEDGE_SOURCE_NAME</code> in{" "}
            <code>apps/backend/.env</code> and restarting the backend.
          </p>
          <p className="page-help">
            Inline-switch + per-task source override ships in 1.1 along with
            the GitHub OAuth integration.
          </p>
        </section>
      ) : null}
    </div>
  );
}
