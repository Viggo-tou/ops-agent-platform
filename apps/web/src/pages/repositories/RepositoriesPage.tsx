import { FormEvent, useEffect, useMemo, useState } from "react";

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
const ORIGIN_LABEL: Record<string, string> = {
  env: ".env",
  upload: "上传",
  clone: "克隆",
};

function formatDate(iso: string): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function relativeTime(iso: string): string {
  if (!iso) return "—";
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

export function RepositoriesPage() {
  const [resp, setResp] = useState<SourcesResp | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [showUpload, setShowUpload] = useState(false);
  const [showClone, setShowClone] = useState(false);
  const [busy, setBusy] = useState(false);
  const [search, setSearch] = useState("");

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

  const sources = resp?.sources ?? [];
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return sources;
    return sources.filter(
      (s) =>
        s.name.toLowerCase().includes(q) ||
        s.path.toLowerCase().includes(q) ||
        (s.description ?? "").toLowerCase().includes(q),
    );
  }, [sources, search]);

  const lastAddedSrc = sources
    .slice()
    .sort((a, b) => new Date(b.added_at).getTime() - new Date(a.added_at).getTime())[0];
  const lastAddedName = lastAddedSrc?.name ?? "—";
  const lastAddedRel = lastAddedSrc?.added_at ? relativeTime(lastAddedSrc.added_at) : "—";

  // We don't have file/loc counts from the API, so keep cards honest:
  // total registered, last added name, last added age, total bytes (TBD).
  const registered = sources.length;
  const totalSizeNote = sources.filter((s) => s.origin !== "env").length;

  // Detect duplicate names / git URLs for the warning banner.
  const dupes = (() => {
    const byName: Record<string, number> = {};
    const byUrl: Record<string, number> = {};
    for (const s of sources) {
      byName[s.name] = (byName[s.name] ?? 0) + 1;
      if (s.git_url) byUrl[s.git_url] = (byUrl[s.git_url] ?? 0) + 1;
    }
    const dupNames = Object.entries(byName).filter(([, n]) => n > 1).map(([k]) => k);
    const dupUrls = Object.entries(byUrl).filter(([, n]) => n > 1).map(([k]) => k);
    return { names: dupNames, urls: dupUrls };
  })();
  const hasDupes = dupes.names.length + dupes.urls.length > 0;

  return (
    <div className="page-shell repo-page-v2">
      <header className="repo-header">
        <div>
          <div className="page-section-eyebrow">仓库管理</div>
          <h1>知识源与仓库</h1>
          <p className="repo-subtitle">
            管理用于代理检索、规划与代码生成的知识源。支持上传 ZIP、克隆 Git
            URL、以及从 .env 默认引用。
          </p>
        </div>
        <div className="repo-actions-row">
          <button className="button primary" onClick={() => setShowUpload(true)}>
            上传 ZIP
          </button>
          <button className="button ghost" onClick={() => setShowClone(true)}>
            克隆 Git URL
          </button>
        </div>
      </header>

      <section className="repo-stat-grid">
        <RepoStatCard label="已注册数" big={registered} caption="仓库与知识源" />
        <RepoStatCard label="最近添加" big={lastAddedName} caption="新增条目" />
        <RepoStatCard label="距上次同步" big={lastAddedRel} caption="基于 added_at" />
        <RepoStatCard
          label="可写源"
          big={totalSizeNote}
          caption="非 .env(可删除/重新克隆)"
        />
      </section>

      {error ? <p className="onboarding-error">{error}</p> : null}

      <section className="repo-list-card">
        <header className="repo-list-head">
          <h2>知识源列表</h2>
          <input
            className="repo-search-input"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="搜索名称 / 路径 / 描述…"
          />
        </header>

        {loading ? <p className="page-help">加载中…</p> : null}
        {!loading && filtered.length === 0 ? (
          <p className="repo-empty">
            {search
              ? "没有匹配的来源。"
              : "尚未添加来源。点击右上 上传 ZIP 或 克隆 Git URL 以开始。"}
          </p>
        ) : null}

        <ul className="repo-row-list">
          {filtered.map((src) => (
            <li key={src.name} className="repo-row-v2">
              <div className="repo-row-icon">
                <FolderIcon />
              </div>
              <div className="repo-row-main">
                <div className="repo-row-titleline">
                  <strong>{src.name}</strong>
                  <span className={`pill ${ORIGIN_PILL[src.origin] ?? ""}`}>
                    {ORIGIN_LABEL[src.origin] ?? src.origin}
                  </span>
                  {src.added_at ? (
                    <span className="repo-row-rel">{relativeTime(src.added_at)}</span>
                  ) : null}
                </div>
                <code className="repo-row-path">{src.path}</code>
                <div className="repo-row-meta">
                  {src.git_url ? (
                    <span>
                      Git: <code>{src.git_url}</code>
                    </span>
                  ) : null}
                  {src.added_at ? (
                    <span>添加于 {formatDate(src.added_at)}</span>
                  ) : null}
                  {src.description ? <span>{src.description}</span> : null}
                </div>
              </div>
              <div className="repo-row-actions">
                {src.origin !== "env" ? (
                  <button
                    className="button ghost button-sm"
                    onClick={() => onDelete(src.name)}
                    disabled={busy}
                  >
                    删除
                  </button>
                ) : (
                  <span className="repo-row-locked">.env(只读)</span>
                )}
              </div>
            </li>
          ))}
        </ul>
      </section>

      {hasDupes ? (
        <section className="repo-warn-banner">
          <div className="repo-warn-icon">!</div>
          <div>
            <strong>检测到重复源</strong>
            <p>
              {dupes.names.length > 0
                ? `重复名称: ${dupes.names.join(", ")}`
                : null}
              {dupes.names.length > 0 && dupes.urls.length > 0 ? " · " : null}
              {dupes.urls.length > 0
                ? `重复 Git URL: ${dupes.urls.join(", ")}`
                : null}
              。请删除其一,以避免代理在多个相同源中重复检索。
            </p>
          </div>
        </section>
      ) : null}

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

function RepoStatCard({
  label,
  big,
  caption,
}: {
  label: string;
  big: string | number;
  caption?: string;
}) {
  return (
    <div className="repo-stat-card">
      <div className="repo-stat-label">{label}</div>
      <div className="repo-stat-big">{big}</div>
      {caption ? <div className="repo-stat-caption">{caption}</div> : null}
    </div>
  );
}

function FolderIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M3 7.5A2.5 2.5 0 0 1 5.5 5h3.6c.55 0 1.07.22 1.45.6l1.4 1.4h7.55A2.5 2.5 0 0 1 22 9.5v8A2.5 2.5 0 0 1 19.5 20h-15A2.5 2.5 0 0 1 2 17.5v-10Z" />
    </svg>
  );
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
        <h2>上传仓库 (.zip)</h2>
        <p className="page-help">
          上传项目压缩包,后端会解压到 <code>data/repositories/&lt;name&gt;/</code>。
          解压后体积上限 200 MB / 5,000 entries。
        </p>
        <form onSubmit={submit} className="modal-form">
          <label>
            名称
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
            描述(可选)
            <input
              className="onboarding-input"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Brief blurb shown in the source list"
            />
          </label>
          <label>
            Zip 文件
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
              取消
            </button>
            <button
              type="submit"
              className="button primary"
              disabled={busy || !file || !name.trim()}
            >
              {busy ? "上传中…" : "上传"}
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
        <h2>克隆 Git URL</h2>
        <p className="page-help">
          1.0 仅支持公开 HTTPS URL(不支持 SSH / 私有仓库)。私有仓库 OAuth 见 1.1。
          克隆参数: <code>--depth=1 --single-branch</code>(60s 超时)。
        </p>
        <form onSubmit={submit} className="modal-form">
          <label>
            名称
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
            描述(可选)
            <input
              className="onboarding-input"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </label>
          {err ? <p className="onboarding-error">{err}</p> : null}
          <div className="modal-actions">
            <button type="button" className="button ghost" onClick={onClose} disabled={busy}>
              取消
            </button>
            <button
              type="submit"
              className="button primary"
              disabled={busy || !name.trim() || !gitUrl.trim()}
            >
              {busy ? "克隆中…" : "克隆"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
