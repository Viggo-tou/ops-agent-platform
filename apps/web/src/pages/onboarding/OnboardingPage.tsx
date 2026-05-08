import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { api } from "../../lib/api";

type ModelPreset = "recommended" | "cli" | "api" | "advanced";

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

const STORAGE_KEY = "ops-agent-onboarding";

interface OnboardingState {
  workspaceName: string;
  selectedSource: string;
  modelPreset: ModelPreset;
  completed: boolean;
}

export function OnboardingPage() {
  const nav = useNavigate();
  const [step, setStep] = useState(0);
  const [state, setState] = useState<OnboardingState>(() => {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw) {
      try {
        return JSON.parse(raw) as OnboardingState;
      } catch {
        // fall through
      }
    }
    return {
      workspaceName: "",
      selectedSource: "",
      modelPreset: "recommended",
      completed: false,
    };
  });

  useEffect(() => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  }, [state]);

  const totalSteps = 4;
  const canAdvance =
    (step === 0 && state.workspaceName.trim().length > 0) ||
    (step === 1 && state.selectedSource.length > 0) ||
    (step === 2 && state.modelPreset.length > 0) ||
    step === 3;

  return (
    <main className="onboarding-page">
      <div className="onboarding-card">
        <div className="onboarding-progress">
          {Array.from({ length: totalSteps }).map((_, i) => (
            <div
              key={i}
              className={`onboarding-progress-step${i <= step ? " active" : ""}${
                i < step ? " done" : ""
              }`}
            >
              <span className="onboarding-progress-num">{i + 1}</span>
            </div>
          ))}
        </div>

        {step === 0 ? (
          <Step1Workspace
            value={state.workspaceName}
            onChange={(v) => setState({ ...state, workspaceName: v })}
          />
        ) : null}
        {step === 1 ? (
          <Step2Repository
            selected={state.selectedSource}
            onSelect={(v) => setState({ ...state, selectedSource: v })}
          />
        ) : null}
        {step === 2 ? (
          <Step3Model
            value={state.modelPreset}
            onChange={(v) => setState({ ...state, modelPreset: v })}
          />
        ) : null}
        {step === 3 ? <Step4Done state={state} /> : null}

        <div className="onboarding-actions">
          {step > 0 ? (
            <button className="button ghost" onClick={() => setStep(step - 1)}>
              Back
            </button>
          ) : (
            <span />
          )}
          {step < totalSteps - 1 ? (
            <button
              className="button primary"
              disabled={!canAdvance}
              onClick={() => setStep(step + 1)}
            >
              Next
            </button>
          ) : (
            <button
              className="button primary"
              onClick={() => {
                setState({ ...state, completed: true });
                nav("/chat");
              }}
            >
              Enter Workspace
            </button>
          )}
        </div>
      </div>
    </main>
  );
}

function Step1Workspace({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="onboarding-step">
      <div className="onboarding-step-label">Step 1 of 4</div>
      <h2 className="onboarding-step-title">Name your workspace</h2>
      <p className="onboarding-step-help">
        This is just a label that shows in the sidebar — you can change it later.
      </p>
      <input
        className="onboarding-input"
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="e.g. ICT Capstone"
        autoFocus
      />
    </div>
  );
}

function Step2Repository({
  selected,
  onSelect,
}: {
  selected: string;
  onSelect: (name: string) => void;
}) {
  const [resp, setResp] = useState<SourcesResp | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .listRepositorySources()
      .then((data) => {
        setResp(data);
        // Pre-select first env-origin source as default if user hasn't picked.
        if (!selected) {
          const def = data.sources.find((s) => s.origin === "env") ?? data.sources[0];
          if (def) onSelect(def.name);
        }
      })
      .catch((err) => setError(String(err.message ?? err)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="onboarding-step">
      <div className="onboarding-step-label">Step 2 of 4</div>
      <h2 className="onboarding-step-title">Pick a repository to work on</h2>
      <p className="onboarding-step-help">
        Sources come from <code>OPS_AGENT_KNOWLEDGE_SOURCE_SPECS</code> in your
        backend <code>.env</code>. To add or remove sources, edit <code>.env</code>{" "}
        and restart the backend (1.0 limitation — full UI in 1.1).
      </p>

      {error ? <p className="onboarding-error">Failed to load: {error}</p> : null}

      {!resp ? (
        <p className="onboarding-step-help">Loading sources…</p>
      ) : resp.sources.length === 0 ? (
        <p className="onboarding-error">
          No sources configured. Set <code>OPS_AGENT_KNOWLEDGE_SOURCE_PATH</code>{" "}
          or <code>OPS_AGENT_KNOWLEDGE_SOURCE_SPECS</code> in <code>.env</code>.
        </p>
      ) : (
        <ul className="onboarding-source-list">
          {resp.sources.map((src) => {
            const isSelected = selected === src.name;
            return (
              <li
                key={src.name}
                className={`onboarding-source-row${
                  isSelected ? " selected" : ""
                }`}
                onClick={() => onSelect(src.name)}
              >
                <div className="onboarding-source-head">
                  <strong>{src.name}</strong>
                  <span className={`pill origin-${src.origin}`}>{src.origin}</span>
                  {isSelected ? <span className="pill selected">selected</span> : null}
                </div>
                <code className="onboarding-source-path">{src.path}</code>
                {src.description ? (
                  <p className="onboarding-source-desc">{src.description}</p>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

const PRESETS: { key: ModelPreset; title: string; desc: string; status: "ready" | "soon" }[] = [
  {
    key: "recommended",
    title: "Recommended",
    desc: "System chooses the best model per stage. Codegen via Claude Code CLI. Reviewer via Anthropic / DeepSeek. No setup.",
    status: "ready",
  },
  {
    key: "cli",
    title: "CLI Runtime",
    desc: "Use your local Claude Code / Codex / Gemini CLI. Reads VS Code OAuth or ChatGPT auth.",
    status: "ready",
  },
  {
    key: "api",
    title: "API Mode",
    desc: "Hosted: OpenAI, Anthropic, Gemini, or custom OpenAI-compatible endpoint. Bring API key.",
    status: "soon",
  },
  {
    key: "advanced",
    title: "Advanced Multi-Agent",
    desc: "Pin different models per stage (planner / knowledge / code / reviewer / repair).",
    status: "soon",
  },
];

function Step3Model({
  value,
  onChange,
}: {
  value: ModelPreset;
  onChange: (v: ModelPreset) => void;
}) {
  return (
    <div className="onboarding-step">
      <div className="onboarding-step-label">Step 3 of 4</div>
      <h2 className="onboarding-step-title">Pick a model setup</h2>
      <p className="onboarding-step-help">
        Recommended works for most users. The other modes are placeholders for
        1.0 — they read .env but the UI editor ships in 1.1.
      </p>
      <ul className="onboarding-preset-list">
        {PRESETS.map((p) => {
          const isSelected = value === p.key;
          const disabled = p.status === "soon";
          return (
            <li
              key={p.key}
              className={`onboarding-preset-row${isSelected ? " selected" : ""}${
                disabled ? " disabled" : ""
              }`}
              onClick={() => {
                if (!disabled) onChange(p.key);
              }}
            >
              <div className="onboarding-preset-head">
                <strong>{p.title}</strong>
                {p.status === "soon" ? (
                  <span className="pill soon">v1.1</span>
                ) : isSelected ? (
                  <span className="pill selected">selected</span>
                ) : null}
              </div>
              <p className="onboarding-preset-desc">{p.desc}</p>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function Step4Done({ state }: { state: OnboardingState }) {
  return (
    <div className="onboarding-step">
      <div className="onboarding-step-label">Step 4 of 4</div>
      <h2 className="onboarding-step-title">Ready</h2>
      <p className="onboarding-step-help">
        Your workspace is configured. You can change any of these later from
        Settings.
      </p>
      <ul className="onboarding-summary">
        <li>
          <span className="label">Workspace</span>
          <span className="value">{state.workspaceName || "—"}</span>
        </li>
        <li>
          <span className="label">Repository</span>
          <span className="value">{state.selectedSource || "—"}</span>
        </li>
        <li>
          <span className="label">Model preset</span>
          <span className="value">{state.modelPreset}</span>
        </li>
      </ul>
      <p className="onboarding-step-help">
        Click <strong>Enter Workspace</strong> below to dispatch your first task.
      </p>
    </div>
  );
}
