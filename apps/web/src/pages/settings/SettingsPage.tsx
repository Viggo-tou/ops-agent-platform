import { useEffect, useMemo, useState } from "react";

import { PermissionGuard } from "../../components/auth/PermissionGuard";
import { api } from "../../lib/api";

// ---- types ---------------------------------------------------------------

interface ProviderModel {
  id: string;
  display_name: string;
  sort_order: number;
}
interface ModelProvider {
  name: string;
  note?: string;
  models: ProviderModel[];
}
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
}
interface AuthStatusResp {
  cli: CliEntry[];
  api_keys: ApiKeyEntry[];
}

// Pipeline-flow ordered (Primary → Planner → Codegen → Reviewer → Knowledge)
const STAGE_ORDER = ["primary_agent", "planner", "codegen", "cc_agent", "synthesis"];

const STAGE_META: Record<string, { name: string; role: string; iconKind: string; iconClass: string }> = {
  primary_agent: { name: "主代理 (Primary agent)", role: "统筹全局规划与任务分配", iconKind: "spark",   iconClass: "stage-icon-violet" },
  planner:       { name: "Planner",                 role: "任务拆解与执行计划生成", iconKind: "sliders", iconClass: "stage-icon-orange" },
  codegen:       { name: "Codegen",                 role: "代码生成与实现",         iconKind: "code",    iconClass: "stage-icon-emerald" },
  cc_agent:      { name: "Reviewer (CC Agent)",     role: "代码审查与合规检查",     iconKind: "shield",  iconClass: "stage-icon-cyan" },
  synthesis:     { name: "Knowledge Synthesis",     role: "检索整合与知识总结",     iconKind: "doc",     iconClass: "stage-icon-purple" },
};

const CLI_PROVIDERS = new Set(["claude_code", "codex", "gemini"]);
const API_PROVIDERS = new Set(["anthropic", "openai", "deepseek", "minimax"]);
const PROVIDER_TO_API_KEY: Record<string, string> = {
  anthropic: "anthropic",
  openai: "openai",
  deepseek: "deepseek",
  minimax: "minimax",
};

const DEFAULT_RUNTIME_KEY = "ops-default-runtime";
const STAGE_RUNTIME_KEY = (s: string) => `ops-stage-runtime-${s}`;
const STAGE_MODEL_KEY = (s: string) => `ops-stage-model-${s}`;
const INHERIT_TOGGLE_KEY = "ops-inherit-main-model";

function classNames(...items: (string | false | null | undefined)[]) {
  return items.filter(Boolean).join(" ");
}

function maskKey(envVar: string): string {
  // Visual placeholder only — backend never returns the real key.
  return "sk-" + "•".repeat(20);
}

// ---- icons (inline svg) -------------------------------------------------

function Icon({ kind, className }: { kind: string; className?: string }) {
  const props = { className, viewBox: "0 0 24 24", "aria-hidden": true };
  switch (kind) {
    case "spark":
      return <svg {...props}><path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.5 5.5l2.8 2.8M15.7 15.7l2.8 2.8M5.5 18.5l2.8-2.8M15.7 8.3l2.8-2.8" /><circle cx="12" cy="12" r="3" /></svg>;
    case "sliders":
      return <svg {...props}><path d="M4 6h10M18 6h2M4 12h2M10 12h10M4 18h14M18 18h2" /><circle cx="14" cy="6" r="2" /><circle cx="8" cy="12" r="2" /><circle cx="16" cy="18" r="2" /></svg>;
    case "code":
      return <svg {...props}><path d="m8 6-6 6 6 6M16 6l6 6-6 6M14 4l-4 16" /></svg>;
    case "shield":
      return <svg {...props}><path d="M12 3 4 6v6c0 5 3.5 9 8 9s8-4 8-9V6Z" /><path d="m9 12 2 2 4-4" /></svg>;
    case "doc":
      return <svg {...props}><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8L14 2Z M14 2v6h6 M8 13h8 M8 17h6" /></svg>;
    case "terminal":
      return <svg {...props}><rect x="3" y="4" width="18" height="16" rx="2" /><path d="m7 9 3 3-3 3M13 15h4" /></svg>;
    case "key":
      return <svg {...props}><circle cx="8" cy="15" r="4" /><path d="m12 12 7-7 3 3-3 3-2-2-2 2-3-3" /></svg>;
    case "boxes":
      return <svg {...props}><path d="M21 7v10l-9 5-9-5V7l9-5 9 5ZM3 7l9 5 9-5M12 12v10" /></svg>;
    case "users":
      return <svg {...props}><circle cx="9" cy="8" r="4" /><path d="M2 21v-2a4 4 0 0 1 4-4h6a4 4 0 0 1 4 4v2" /></svg>;
    case "check":
      return <svg {...props}><path d="m5 12 5 5L20 7" /></svg>;
    case "rotate":
      return <svg {...props}><path d="M3 12a9 9 0 0 1 15-6.7L21 8M21 3v5h-5M21 12a9 9 0 0 1-15 6.7L3 16M3 21v-5h5" /></svg>;
    default:
      return null;
  }
}

// ---- root --------------------------------------------------------------

export function SettingsPage() {
  return (
    <div className="page-shell settings-page-v3">
      <PermissionGuard
        permission="settings:view"
        fallback={
          <div className="permission-note">Your role cannot view system settings.</div>
        }
      >
        <SettingsBody />
      </PermissionGuard>
    </div>
  );
}

function SettingsBody() {
  const [providers, setProviders] = useState<ModelProvider[]>([]);
  const [selectedModel, setSelectedModel] = useState<string>("");
  const [overrides, setOverrides] = useState<OverridesResp | null>(null);
  const [authStatus, setAuthStatus] = useState<AuthStatusResp | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);

  const [defaultRuntime, setDefaultRuntime] = useState<"API" | "CLI">(() => {
    const stored = window.localStorage.getItem(DEFAULT_RUNTIME_KEY);
    return stored === "CLI" ? "CLI" : "API";
  });
  const [inheritMain, setInheritMain] = useState<boolean>(() => {
    return window.localStorage.getItem(INHERIT_TOGGLE_KEY) !== "off";
  });

  useEffect(() => {
    window.localStorage.setItem(DEFAULT_RUNTIME_KEY, defaultRuntime);
  }, [defaultRuntime]);
  useEffect(() => {
    window.localStorage.setItem(INHERIT_TOGGLE_KEY, inheritMain ? "on" : "off");
  }, [inheritMain]);

  const reload = () => {
    Promise.all([
      api.getModelProviders(),
      api.getSelectedModel(),
      api.getRuntimeOverrides(),
      api.getAuthStatus(),
    ])
      .then(([p, sel, o, a]) => {
        setProviders(p);
        setSelectedModel(sel.model_id);
        setOverrides(o);
        setAuthStatus(a);
      })
      .catch((err) => setError(String(err.message ?? err)));
  };
  useEffect(reload, []);

  const onSelectModel = async (id: string) => {
    setPending("model");
    try {
      await api.setSelectedModel({ model_id: id });
      setSelectedModel(id);
    } catch (e: any) {
      setError(String(e?.message ?? e));
    } finally {
      setPending(null);
    }
  };

  const onSetOverride = async (stage: string, value: string) => {
    setPending(stage);
    setError(null);
    try {
      const updated = await api.patchRuntimeOverride({ stage, value: value || null });
      setOverrides(updated as OverridesResp);
    } catch (e: any) {
      setError(String(e?.message ?? e));
    } finally {
      setPending(null);
    }
  };

  const overrideCount = (overrides?.stages ?? []).filter((s) => s.override).length;

  const effectiveProviders = useMemo(() => {
    const set = new Set<string>();
    for (const s of overrides?.stages ?? []) {
      if (s.effective) set.add(s.effective);
    }
    return set;
  }, [overrides]);

  const cliInUse = (authStatus?.cli ?? []).filter((c) => effectiveProviders.has(c.key));
  const apiInUse = (authStatus?.api_keys ?? []).filter((k) =>
    Array.from(effectiveProviders).some((p) => PROVIDER_TO_API_KEY[p] === k.key),
  );

  const flatModels = useMemo(() => {
    const out: { provider: string; id: string; display: string }[] = [];
    for (const p of providers) {
      for (const m of p.models ?? []) {
        out.push({ provider: p.name, id: m.id, display: m.display_name });
      }
    }
    return out;
  }, [providers]);

  // Sort overrides into pipeline-flow order
  const orderedStages: StageStatus[] = useMemo(() => {
    if (!overrides) return [];
    const byKey: Record<string, StageStatus> = {};
    for (const s of overrides.stages) byKey[s.stage] = s;
    return STAGE_ORDER.filter((k) => byKey[k]).map((k) => byKey[k]);
  }, [overrides]);

  return (
    <>
      {/* HEADER SUMMARY */}
      <section className="settings-v3-header">
        <h1>模型与运行配置</h1>
        <p className="settings-v3-subtitle">
          先选择全局主模型,再按需覆盖各个子代理的模型与运行方式。
        </p>
        <div className="settings-summary-chips">
          <span className="summary-chip">
            <Icon kind="boxes" className="chip-icon" />
            当前默认模型: <strong>{selectedModel || "—"}</strong>
          </span>
          <span className="summary-chip">
            <Icon kind="terminal" className="chip-icon" />
            默认运行方式: <strong>{defaultRuntime}</strong>
          </span>
          <span className="summary-chip">
            <Icon kind="users" className="chip-icon" />
            已覆盖 <strong>{overrideCount}</strong> 个子代理
          </span>
        </div>
      </section>

      {error ? <p className="onboarding-error">{error}</p> : null}

      {/* SECTION A — Global main model */}
      <SectionCard letter="A" title="全局主模型">
        <ProviderTabsAndModels
          providers={providers}
          flatModels={flatModels}
          selectedModel={selectedModel}
          onSelect={onSelectModel}
          pending={pending}
        />

        <div className="section-a-footer">
          <div className="section-a-footer-row">
            <span className="section-a-footer-label">默认运行方式</span>
            <SegToggle
              options={["API", "CLI"]}
              value={defaultRuntime}
              onChange={(v) => setDefaultRuntime(v as "API" | "CLI")}
            />
          </div>
          <div className="section-a-footer-row">
            <div>
              <div className="section-a-footer-label">无覆盖时子代理继承主模型</div>
              <div className="section-a-footer-help">减少重复配置,只有特殊子代理单独覆盖。</div>
            </div>
            <ToggleSwitch checked={inheritMain} onChange={setInheritMain} />
          </div>
        </div>
      </SectionCard>

      {/* SECTION B — Sub-agent overrides */}
      <SectionCard
        letter="B"
        title="子代理模型覆盖"
        subtitle="按 pipeline 顺序排列(主代理→Planner→Codegen→Reviewer→Knowledge)。建议只有需要特殊能力的子代理才单独覆盖。"
      >
        {orderedStages.length === 0 ? (
          <p className="page-help">Loading…</p>
        ) : (
          <div className="agent-grid">
            <div className="agent-grid-head">
              <div>子代理</div>
              <div>职责说明</div>
              <div>运行方式</div>
              <div>Provider</div>
              <div>Model</div>
            </div>
            {orderedStages.map((s) => (
              <AgentRow
                key={s.stage}
                stage={s}
                providers={providers}
                pending={pending === s.stage}
                onChange={(v) => onSetOverride(s.stage, v)}
                inheritMain={inheritMain}
                defaultRuntime={defaultRuntime}
              />
            ))}
          </div>
        )}
      </SectionCard>

      {/* SECTION C — CLI + API runtime auth */}
      <SectionCard
        letter="C"
        title="CLI 与运行时认证"
        subtitle="只有当某个子代理选择 CLI 或对应 API Provider 时,才需要该认证。"
      >
        <div className="auth-twocol-v3">
          <div className="auth-pane">
            <div className="auth-pane-title">
              <Icon kind="terminal" className="pane-icon" />
              CLI 认证状态
            </div>
            <div className="auth-pane-body">
              {cliInUse.length === 0 ? (
                <p className="page-help">无 CLI provider 在使用。</p>
              ) : (
                cliInUse.map((c) => <CliCard key={c.key} entry={c} />)
              )}
            </div>
          </div>
          <div className="auth-pane">
            <div className="auth-pane-title">
              <Icon kind="key" className="pane-icon" />
              API Keys
            </div>
            <div className="auth-pane-body">
              {apiInUse.length === 0 ? (
                <p className="page-help">无 API provider 在使用。</p>
              ) : (
                <div className="api-keys-list">
                  {apiInUse.map((k) => (
                    <ApiKeyRow key={k.key} entry={k} />
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      </SectionCard>

      {/* sticky bottom bar */}
      <div className="settings-bottom-bar">
        <button
          className="button ghost"
          onClick={async () => {
            if (!window.confirm("清除所有 per-stage override,恢复 .env 默认?")) return;
            const stages = (overrides?.stages ?? []).filter((s) => s.override).map((s) => s.stage);
            for (const stage of stages) {
              await api.patchRuntimeOverride({ stage, value: null });
            }
            STAGE_ORDER.forEach((s) => {
              window.localStorage.removeItem(STAGE_RUNTIME_KEY(s));
              window.localStorage.removeItem(STAGE_MODEL_KEY(s));
            });
            reload();
          }}
        >
          <Icon kind="rotate" className="btn-icon" />
          恢复默认
        </button>
        <span className="settings-autosave-hint">自动保存 — 改动立即生效</span>
      </div>
    </>
  );
}

// ---- structural pieces -------------------------------------------------

function SectionCard({
  letter,
  title,
  subtitle,
  children,
}: {
  letter: string;
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="section-card-v3">
      <header className="section-card-v3-head">
        <span className="section-card-v3-letter">{letter}</span>
        <div>
          <h2>{title}</h2>
          {subtitle ? <p className="section-card-v3-subtitle">{subtitle}</p> : null}
        </div>
      </header>
      {children}
    </section>
  );
}

function ProviderTabsAndModels({
  providers,
  flatModels,
  selectedModel,
  onSelect,
  pending,
}: {
  providers: ModelProvider[];
  flatModels: { provider: string; id: string; display: string }[];
  selectedModel: string;
  onSelect: (id: string) => void;
  pending: string | null;
}) {
  const tabs = ["全部", ...providers.map((p) => p.name)];
  const [activeTab, setActiveTab] = useState<string>("全部");

  const visible = activeTab === "全部" ? flatModels : flatModels.filter((m) => m.provider === activeTab);

  return (
    <>
      <div className="provider-tabs">
        {tabs.map((t) => (
          <button
            key={t}
            type="button"
            className={classNames("provider-tab", activeTab === t && "active")}
            onClick={() => setActiveTab(t)}
          >
            {t}
          </button>
        ))}
      </div>

      <div className="model-grid-v3">
        {visible.length === 0 ? (
          <p className="page-help">No models in this provider.</p>
        ) : (
          visible.map((m) => {
            const active = selectedModel === m.id;
            return (
              <button
                key={m.id}
                type="button"
                className={classNames("model-card-v3", active && "active")}
                onClick={() => onSelect(m.id)}
                disabled={pending === "model"}
              >
                <div className="model-card-v3-icon">
                  <Icon kind="spark" />
                </div>
                <div className="model-card-v3-text">
                  <div className="model-card-v3-name">{m.display}</div>
                  <div className="model-card-v3-provider">{m.provider}</div>
                </div>
                {active ? (
                  <span className="model-card-v3-check">
                    <Icon kind="check" />
                  </span>
                ) : null}
              </button>
            );
          })
        )}
      </div>
    </>
  );
}

function SegToggle({
  options,
  value,
  onChange,
}: {
  options: string[];
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="seg-toggle">
      {options.map((opt) => (
        <button
          key={opt}
          type="button"
          className={classNames("seg-toggle-btn", value === opt && "active")}
          onClick={() => onChange(opt)}
        >
          {opt}
        </button>
      ))}
    </div>
  );
}

function ToggleSwitch({
  checked,
  onChange,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <button
      type="button"
      className={classNames("toggle-switch", checked && "active")}
      onClick={() => onChange(!checked)}
      aria-pressed={checked}
    >
      <span className="toggle-switch-thumb" />
    </button>
  );
}

function AgentRow({
  stage,
  providers,
  pending,
  onChange,
  inheritMain,
  defaultRuntime,
}: {
  stage: StageStatus;
  providers: ModelProvider[];
  pending: boolean;
  onChange: (value: string) => void;
  inheritMain: boolean;
  defaultRuntime: "API" | "CLI";
}) {
  const meta = STAGE_META[stage.stage] ?? {
    name: stage.stage,
    role: "",
    iconKind: "spark",
    iconClass: "stage-icon-emerald",
  };

  // Per-row runtime preference (UI-only, persisted in localStorage)
  const [rowRuntime, setRowRuntime] = useState<string>(() => {
    return window.localStorage.getItem(STAGE_RUNTIME_KEY(stage.stage)) ?? "inherit";
  });
  const [rowModel, setRowModel] = useState<string>(() => {
    return window.localStorage.getItem(STAGE_MODEL_KEY(stage.stage)) ?? "inherit";
  });

  useEffect(() => {
    window.localStorage.setItem(STAGE_RUNTIME_KEY(stage.stage), rowRuntime);
  }, [rowRuntime, stage.stage]);
  useEffect(() => {
    window.localStorage.setItem(STAGE_MODEL_KEY(stage.stage), rowModel);
  }, [rowModel, stage.stage]);

  // Filter Provider dropdown by chosen runtime
  const filteredProviders = (() => {
    const rt = rowRuntime === "inherit" ? defaultRuntime : rowRuntime;
    if (rt === "CLI") return stage.allowed.filter((p) => CLI_PROVIDERS.has(p));
    if (rt === "API") return stage.allowed.filter((p) => API_PROVIDERS.has(p));
    return stage.allowed;
  })();

  // Compute mode pill for "current effective"
  const mode = (() => {
    const p = stage.effective;
    if (!p) return "—";
    if (CLI_PROVIDERS.has(p)) return "CLI";
    if (API_PROVIDERS.has(p)) return "API";
    if (p === "auto") return "Auto";
    return "Other";
  })();

  // Flatten all models for the model dropdown
  const allModels = providers.flatMap((p) => p.models.map((m) => ({ id: m.id, display: m.display_name, provider: p.name })));

  return (
    <div className="agent-grid-row">
      <div className="agent-grid-name">
        <span className={classNames("stage-icon-pill", meta.iconClass)}>
          <Icon kind={meta.iconKind} />
        </span>
        <span>{meta.name}</span>
      </div>
      <div className="agent-grid-role">{meta.role}</div>
      <div>
        <select
          className="agent-grid-select"
          value={rowRuntime}
          onChange={(e) => setRowRuntime(e.target.value)}
        >
          <option value="inherit">继承主设置 ({defaultRuntime})</option>
          <option value="CLI">CLI</option>
          <option value="API">API</option>
        </select>
      </div>
      <div>
        <select
          className="agent-grid-select"
          value={stage.override ?? ""}
          onChange={(e) => onChange(e.target.value)}
          disabled={pending}
        >
          <option value="">{inheritMain ? "继承主模型 (.env)" : ".env 默认"}</option>
          {filteredProviders.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
        <div className="agent-grid-mode-line">
          <span className={classNames("pill", `mode-${mode.toLowerCase()}`)}>{mode}</span>
          <span className="cell-muted"> · 当前 <code>{stage.effective ?? "—"}</code></span>
        </div>
      </div>
      <div>
        <select
          className="agent-grid-select"
          value={rowModel}
          onChange={(e) => setRowModel(e.target.value)}
        >
          <option value="inherit">继承主模型</option>
          {allModels.map((m) => (
            <option key={m.id} value={m.id}>
              {m.display}
            </option>
          ))}
        </select>
        <div className="agent-grid-mode-line">
          <span className="cell-muted">v1.1 起 backend 真按此 routing</span>
        </div>
      </div>
    </div>
  );
}

function CliCard({ entry }: { entry: CliEntry }) {
  const ok = entry.authenticated && entry.cli_available;
  return (
    <div className={classNames("auth-card-v3", ok ? "ok" : "warn")}>
      <div className="auth-card-v3-head">
        <span className={classNames("status-dot", ok ? "green" : "red")} />
        <strong>{entry.label}</strong>
        <span className={classNames("pill", ok ? "ok-pill" : "warn-pill")}>
          {ok ? "已登录" : entry.cli_available ? "未认证" : "CLI 未安装"}
        </span>
      </div>
      {entry.auth_path ? (
        <div className="auth-card-v3-path">
          <code>{entry.auth_path}</code>
          {ok ? (
            <span className="ok-text">
              <Icon kind="check" /> 可用
            </span>
          ) : null}
        </div>
      ) : null}
      {!ok && entry.notes ? <p className="auth-card-v3-notes">{entry.notes}</p> : null}
      {!ok ? (
        <p className="auth-card-v3-cmd">
          运行: <code>{entry.login_command}</code>
        </p>
      ) : null}
    </div>
  );
}

function ApiKeyRow({ entry }: { entry: ApiKeyEntry }) {
  const set = entry.set;
  return (
    <div className={classNames("api-key-row", set ? "ok" : "warn")}>
      <span className={classNames("status-dot", set ? "green" : "red")} />
      <strong className="api-key-name">{entry.label}</strong>
      <span className={classNames("pill", set ? "ok-pill" : "warn-pill")}>
        {set ? "已配置" : "未配置"}
      </span>
      <code className="api-key-mask">{set ? maskKey(entry.env_var) : "—"}</code>
      {set ? (
        <span className="ok-text">
          <Icon kind="check" /> 可用
        </span>
      ) : (
        <span className="api-key-hint">
          配置 <code>{entry.env_var}</code> in .env
        </span>
      )}
    </div>
  );
}
