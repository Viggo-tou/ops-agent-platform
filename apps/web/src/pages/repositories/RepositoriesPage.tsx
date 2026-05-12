import { FormEvent, useEffect, useMemo, useState } from "react";

import { api } from "../../lib/api";

interface RepoSource {
  name: string;
  path: string;
  description: string;
  origin: string;
  git_url: string;
  added_at: string;
}

interface SourcesResp {
  sources: RepoSource[];
  multi_source_enabled: boolean;
}

const ORIGIN_LABEL: Record<string, string> = {
  env: ".env",
  upload: "上传",
  clone: "克隆",
};
const ORIGIN_TONE: Record<string, "blue" | "orange" | "purple" | "green" | "red"> = {
  env: "blue",
  upload: "purple",
  clone: "green",
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

function Icon({ kind, className }: { kind: string; className?: string }) {
  const props = { className, viewBox: "0 0 24 24", "aria-hidden": true } as const;
  switch (kind) {
    case "folder":
      return <svg {...props}><path d="M3 7.5A2.5 2.5 0 0 1 5.5 5h3.6c.55 0 1.07.22 1.45.6l1.4 1.4h7.55A2.5 2.5 0 0 1 22 9.5v8A2.5 2.5 0 0 1 19.5 20h-15A2.5 2.5 0 0 1 2 17.5v-10Z" /></svg>;
    case "boxes":
      return <svg {...props}><path d="M21 7v10l-9 5-9-5V7l9-5 9 5ZM3 7l9 5 9-5M12 12v10" /></svg>;
    case "clock":
      return <svg {...props}><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></svg>;
    case "spark":
      return <svg {...props}><path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.5 5.5l2.8 2.8M15.7 15.7l2.8 2.8M5.5 18.5l2.8-2.8M15.7 8.3l2.8-2.8" /><circle cx="12" cy="12" r="3" /></svg>;
    case "search":
      return <svg {...props}><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>;
    case "upload":
      return <svg {...props}><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12" /></svg>;
    case "git":
      return <svg {...props}><path d="m20 12-8 8-8-8 8-8 8 8Z" /><circle cx="12" cy="12" r="2" /><path d="M12 8v4M12 14v2M16 12h-4M10 12H8" /></svg>;
    case "trash":
      return <svg {...props}><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" /></svg>;
    case "alert":
      return <svg {...props}><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z" /><path d="M12 9v4M12 17h.01" /></svg>;
    case "lock":
      return <svg {...props}><rect x="4" y="11" width="16" height="10" rx="2" /><path d="M8 11V7a4 4 0 1 1 8 0v4" /></svg>;
    case "more":
      return <svg {...props}><circle cx="5" cy="12" r="1.5" /><circle cx="12" cy="12" r="1.5" /><circle cx="19" cy="12" r="1.5" /></svg>;
    default:
      return null;
  }
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
    if (!window.confirm(`删除来源 "${name}"? 本地文件也会被移除。`)) return;
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

  const registered = sources.length;
  const writable = sources.filter((s) => s.origin !== "env").length;
  const env = sources.filter((s) => s.origin === "env").length;

  const dupes = (() => {
    const byName: Record<string, number> = {};
    const byUrl: Record<string, number> = {};
    for (const s of sources) {
      byName[s.name] = (byName[s.name] ?? 0) + 1;
      if (s.git_url) byUrl[s.git_url] = (byUrl[s.git_url] ?? 0) + 1;
    }
    return {
      names: Object.entries(byName).filter(([, n]) => n > 1).map(([k]) => k),
      urls: Object.entries(byUrl).filter(([, n]) => n > 1).map(([k]) => k),
    };
  })();
  const hasDupes = dupes.names.length + dupes.urls.length > 0;

  return (
    <div className="page-shell repo3-page">
      <section className="dash3-hero">
        <div className="dash3-hero-top">
          <div>
            <div className="page-section-eyebrow">仓库管理</div>
            <h1>知识源与仓库</h1>
            <p className="dash3-hero-subtitle">
              管理用于代理检索、规划与代码生成的知识源。支持上传 ZIP、克隆 Git
              URL、以及从 .env 默认引用。
            </p>
          </div>
          <div className="repo3-actions">
            <button
              className="tl3-primary-btn"
              type="button"
              onClick={() => setShowUpload(true)}
            >
              <Icon kind="upload" />
              上传 ZIP
            </button>
            <button
              className="repo3-ghost-btn"
              type="button"
              onClick={() => setShowClone(true)}
            >
              <Icon kind="git" />
              克隆 Git URL
            </button>
          </div>
        </div>

        <div className="repo3-kpi-grid">
          <KpiCard
            iconKind="boxes"
            tone="blue"
            title="已注册数"
            value={registered}
            subtitle="仓库与知识源"
          />
          <KpiCard
            iconKind="folder"
            tone="purple"
            title="最近添加"
            value={lastAddedName}
            subtitle="新增条目"
          />
          <KpiCard
            iconKind="clock"
            tone="orange"
            title="距上次同步"
            value={lastAddedRel}
            subtitle="基于 added_at"
          />
          <KpiCard
            iconKind="spark"
            tone="green"
            title="可写源"
            value={writable}
            subtitle={`非 .env(${env} 个 .env 只读)`}
          />
        </div>
      </section>

      {error ? <p className="onboarding-error">{error}</p> : null}

      <section className="repo3-list-card">
        <header className="repo3-list-head">
          <h2>
            知识源列表 <span className="tl3-muted">({filtered.length})</span>
          </h2>
          <div className="repo3-search">
            <Icon kind="search" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="搜索名称 / 路径 / 描述…"
            />
          </div>
        </header>

        {loading ? <p className="tl3-empty">加载中…</p> : null}
        {!loading && filtered.length === 0 ? (
          <p className="tl3-empty">
            {search
              ? "没有匹配的来源。"
              : "尚未添加来源。点击右上 上传 ZIP 或 克隆 Git URL 以开始。"}
          </p>
        ) : null}

        <ul className="repo3-row-list">
          {filtered.map((src) => (
            <li key={src.name} className="repo3-row">
              <div className={`repo3-row-icon tone-${ORIGIN_TONE[src.origin] ?? "blue"}`}>
                <Icon kind="folder" />
              </div>
              <div className="repo3-row-main">
                <div className="repo3-row-titleline">
                  <strong>{src.name}</strong>
                  <span className={`repo3-origin-pill tone-${ORIGIN_TONE[src.origin] ?? "blue"}`}>
                    {ORIGIN_LABEL[src.origin] ?? src.origin}
                  </span>
                  {src.added_at ? (
                    <span className="repo3-row-rel">{relativeTime(src.added_at)}</span>
                  ) : null}
                </div>
                <code className="repo3-row-path">{src.path}</code>
                <div className="repo3-row-meta">
                  {src.git_url ? (
                    <span>
                      <Icon kind="git" /> <code>{src.git_url}</code>
                    </span>
                  ) : null}
                  {src.added_at ? (
                    <span>添加于 {formatDate(src.added_at)}</span>
                  ) : null}
                  {src.description ? <span>{src.description}</span> : null}
                </div>
              </div>
              <div className="repo3-row-actions">
                {src.origin !== "env" ? (
                  <button
                    className="repo3-ghost-btn-sm"
                    onClick={() => onDelete(src.name)}
                    disabled={busy}
                    title="删除来源"
                  >
                    <Icon kind="trash" />
                    删除
                  </button>
                ) : (
                  <span className="repo3-locked">
                    <Icon kind="lock" />
                    .env 只读
                  </span>
                )}
              </div>
            </li>
          ))}
        </ul>
      </section>

      {hasDupes ? (
        <section className="repo3-warn-banner">
          <div className="repo3-warn-icon">
            <Icon kind="alert" />
          </div>
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
  value: string | number;
  subtitle: string;
}) {
  return (
    <div className="tl3-kpi-card">
      <div className={`tl3-kpi-iconwrap tone-${tone}`}>
        <Icon kind={iconKind} />
      </div>
      <div className="tl3-kpi-title">{title}</div>
      <div className="tl3-kpi-value">
        {typeof value === "number" ? value.toLocaleString() : value}
      </div>
      <div className="tl3-kpi-subtitle">{subtitle}</div>
    </div>
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
            <button type="button" className="repo3-ghost-btn" onClick={onClose} disabled={busy}>
              取消
            </button>
            <button
              type="submit"
              className="tl3-primary-btn"
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
            <button type="button" className="repo3-ghost-btn" onClick={onClose} disabled={busy}>
              取消
            </button>
            <button
              type="submit"
              className="tl3-primary-btn"
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
