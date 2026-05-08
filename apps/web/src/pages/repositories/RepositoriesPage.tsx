import { FormEvent, useEffect, useState } from "react";

import { api } from "../../lib/api";

interface RepoSource {
  name: string;
  path: string;
  description: string;
  origin: string; // env | upload | clone
  git_url: string;
  added_at: string;
}

interface SourcesResp {
  sources: RepoSource[];
  multi_source_enabled: boolean;
}

const ORIGIN_PILL: Record<string, string> = {
  env: "origin-env",
  upload: "origin-upload",
  clone: "origin-clone",
};

export function RepositoriesPage() {
  const [resp, setResp] = useState<SourcesResp | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [showUpload, setShowUpload] = useState(false);
  const [showClone, setShowClone] = useState(false);
  const [busy, setBusy] = useState(false);

  const reload = () => {
    setLoading(true);
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
  };

  useEffect(reload, []);

  const onDelete = (name: string) => {
    if (!window.confirm(`Delete source "${name}"? Local files will be removed.`)) return;
    setBusy(true);
    api
      .deleteRepositorySource(name)
      .then(reload)
      .catch((err) => setError(String(err.message ?? err)))
      .finally(() => setBusy(false));
  };

  return (
    <div className="page-shell repositories-page">
      <header className="page-header">
        <div>
          <div className="page-section-label">Repository Setup</div>
          <h1>Knowledge sources</h1>
          <p className="page-subtitle">
            Sources the agent can index, search, and edit. Mix env-configured
            (read-only here), uploaded zips, and git-cloned repos.
          </p>
        </div>
        <div className="repo-actions-row">
          <button className="button primary" onClick={() => setShowUpload(true)}>
            Upload .zip
          </button>
          <button className="button ghost" onClick={() => setShowClone(true)}>
            Clone Git URL
          </button>
        </div>
      </header>

      {error ? <p className="onboarding-error">{error}</p> : null}
      {loading ? <p className="page-help">Loading…</p> : null}

      {resp && resp.sources.length === 0 ? (
        <div className="repo-empty">
          <h2>No sources yet</h2>
          <p className="page-help">
            Add one via <strong>Upload .zip</strong> or <strong>Clone Git URL</strong>{" "}
            above, or set <code>OPS_AGENT_KNOWLEDGE_SOURCE_PATH</code> in the
            backend <code>.env</code>.
          </p>
        </div>
      ) : null}

      {resp && resp.sources.length > 0 ? (
        <ul className="repo-list">
          {resp.sources.map((src) => (
            <li key={src.name} className="repo-row">
              <div className="repo-row-head">
                <strong>{src.name}</strong>
                <span className={`pill ${ORIGIN_PILL[src.origin] ?? ""}`}>
                  {src.origin}
                </span>
                {src.origin !== "env" ? (
                  <button
                    className="button ghost button-sm"
                    onClick={() => onDelete(src.name)}
                    disabled={busy}
                  >
                    Delete
                  </button>
                ) : null}
              </div>
              <code className="repo-row-path">{src.path}</code>
              {src.git_url ? (
                <p className="repo-row-meta">
                  Cloned from <code>{src.git_url}</code>
                  {src.added_at ? ` · ${formatDate(src.added_at)}` : null}
                </p>
              ) : null}
              {src.description ? (
                <p className="repo-row-desc">{src.description}</p>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}

      <section className="repo-switch-help">
        <h2>Per-task source override (1.0)</h2>
        <p className="page-help">
          On the <strong>New Task</strong> page, pick which source to use per
          task. When unset, the orchestrator uses the env-configured default
          (legacy behavior, fully preserved).
        </p>
      </section>

      {showUpload ? (
        <UploadModal
          onClose={() => setShowUpload(false)}
          onDone={() => {
            setShowUpload(false);
            reload();
          }}
        />
      ) : null}
      {showClone ? (
        <CloneModal
          onClose={() => setShowClone(false)}
          onDone={() => {
            setShowClone(false);
            reload();
          }}
        />
      ) : null}
    </div>
  );
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function UploadModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!file || !name.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      await api.uploadRepositoryZip({ name: name.trim(), description, file });
      onDone();
    } catch (e: any) {
      setErr(String(e?.message ?? e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card" onClick={(e) => e.stopPropagation()}>
        <h2>Upload repository (.zip)</h2>
        <p className="page-help">
          Upload a project as a zip. Will extract to{" "}
          <code>data/repositories/&lt;name&gt;/</code> on the backend. Max 200 MB
          uncompressed, 5,000 entries.
        </p>
        <form onSubmit={submit} className="modal-form">
          <label>
            Name
            <input
              className="onboarding-input"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="my-project"
              required
              autoFocus
            />
          </label>
          <label>
            Description (optional)
            <input
              className="onboarding-input"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Brief blurb shown in the source list"
            />
          </label>
          <label>
            Zip file
            <input
              type="file"
              accept=".zip,application/zip"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              required
            />
          </label>
          {err ? <p className="onboarding-error">{err}</p> : null}
          <div className="modal-actions">
            <button type="button" className="button ghost" onClick={onClose} disabled={busy}>
              Cancel
            </button>
            <button type="submit" className="button primary" disabled={busy || !file || !name.trim()}>
              {busy ? "Uploading…" : "Upload"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function CloneModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState("");
  const [gitUrl, setGitUrl] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!name.trim() || !gitUrl.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      await api.cloneRepositoryGit({
        name: name.trim(),
        git_url: gitUrl.trim(),
        description,
      });
      onDone();
    } catch (e: any) {
      setErr(String(e?.message ?? e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card" onClick={(e) => e.stopPropagation()}>
        <h2>Clone Git URL</h2>
        <p className="page-help">
          Public HTTPS URLs only in 1.0 (no SSH). Private repos via GitHub
          OAuth ship in 1.1. Clones with <code>--depth=1 --single-branch</code>{" "}
          (60s timeout).
        </p>
        <form onSubmit={submit} className="modal-form">
          <label>
            Name
            <input
              className="onboarding-input"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="my-project"
              required
              autoFocus
            />
          </label>
          <label>
            Git URL
            <input
              className="onboarding-input"
              value={gitUrl}
              onChange={(e) => setGitUrl(e.target.value)}
              placeholder="https://github.com/user/repo.git"
              required
            />
          </label>
          <label>
            Description (optional)
            <input
              className="onboarding-input"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </label>
          {err ? <p className="onboarding-error">{err}</p> : null}
          <div className="modal-actions">
            <button type="button" className="button ghost" onClick={onClose} disabled={busy}>
              Cancel
            </button>
            <button type="submit" className="button primary" disabled={busy || !name.trim() || !gitUrl.trim()}>
              {busy ? "Cloning…" : "Clone"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
