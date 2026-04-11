import { useMemo, useState } from "react";

import { useAuth } from "../../lib/auth";

interface ModelOption {
  provider: string;
  models: string[];
  note: string;
}

const modelGroups: ModelOption[] = [
  { provider: "OpenAI", models: ["GPT-5.4", "GPT-5.4 Mini", "GPT-4.1"], note: "General reasoning and tool use" },
  { provider: "Anthropic", models: ["Claude Opus 4.6", "Claude Sonnet 4.6", "Claude Haiku 4.5"], note: "Long-context writing and coding" },
  { provider: "Google", models: ["Gemini 2.5 Pro", "Gemini 2.5 Flash"], note: "Multimodal and fast assistant work" },
  { provider: "DeepSeek", models: ["DeepSeek V3", "DeepSeek R1"], note: "Reasoning and code-oriented tasks" },
  { provider: "Moonshot", models: ["Kimi K2", "Kimi Turbo"], note: "Long-context Chinese and mixed-language tasks" },
  { provider: "Mistral", models: ["Mistral Large", "Codestral"], note: "Enterprise and coding workflows" },
  { provider: "Cohere", models: ["Command R+", "Command A"], note: "RAG and enterprise retrieval" },
  { provider: "Aliyun / Zhipu / Domestic", models: ["MiniMax-M2.7", "Qwen Max", "GLM-5"], note: "Domestic provider compatibility" },
];

export function ModelSelector() {
  const { can } = useAuth();
  const [activeTab, setActiveTab] = useState<"models" | "api">("models");
  const [providerFilter, setProviderFilter] = useState("All");
  const [selectedModel, setSelectedModel] = useState(
    () => window.localStorage.getItem("ops-agent-selected-model") ?? "GLM-5",
  );
  const [providerKeys, setProviderKeys] = useState<Record<string, string>>({});

  const selectedProvider = modelGroups.find((group) => group.models.includes(selectedModel))?.provider ?? "Not selected";
  const visibleGroups = useMemo(
    () => (providerFilter === "All" ? modelGroups : modelGroups.filter((group) => group.provider === providerFilter)),
    [providerFilter],
  );

  function selectModel(model: string) {
    if (!can("settings:model_config")) {
      return;
    }
    setSelectedModel(model);
    window.localStorage.setItem("ops-agent-selected-model", model);
  }

  return (
    <div className="settings-panel">
      <div className="settings-tab-row" role="tablist" aria-label="Settings sections">
        <button className={activeTab === "models" ? "tab-button active" : "tab-button"} type="button" onClick={() => setActiveTab("models")}>
          Model selection
        </button>
        <button className={activeTab === "api" ? "tab-button active" : "tab-button"} type="button" onClick={() => setActiveTab("api")}>
          API configuration
        </button>
      </div>

      {activeTab === "models" ? (
        <section className="settings-section">
          <div className="settings-section-head">
            <div className="settings-icon">M</div>
            <div>
              <h2>Select model</h2>
              <p>Choose the assistant model used for this workspace.</p>
            </div>
          </div>

          {!can("settings:model_config") ? (
            <div className="permission-note">Your role can view settings but cannot change model configuration.</div>
          ) : null}

          <div className="provider-chip-row" aria-label="Provider filter">
            {["All", ...modelGroups.map((group) => group.provider)].map((provider) => (
              <button
                key={provider}
                type="button"
                className={provider === providerFilter ? "provider-chip active" : "provider-chip"}
                onClick={() => setProviderFilter(provider)}
              >
                {provider}
              </button>
            ))}
          </div>

          <div className="model-row-list">
            {visibleGroups.flatMap((group) =>
              group.models.map((model) => (
                <button
                  key={`${group.provider}-${model}`}
                  type="button"
                  className={model === selectedModel ? "model-row-card selected" : "model-row-card"}
                  onClick={() => selectModel(model)}
                  disabled={!can("settings:model_config")}
                >
                  <span>
                    <strong>{model}</strong>
                    <small>{group.provider}</small>
                  </span>
                  <span>{group.note}</span>
                  <i>{model === selectedModel ? "Selected" : "Select"}</i>
                </button>
              )),
            )}
          </div>
        </section>
      ) : (
        <section className="settings-section">
          <div className="settings-section-head">
            <div className="settings-icon">K</div>
            <div>
              <h2>API configuration</h2>
              <p>Keys stay masked here and should be managed by the backend or a vault in production.</p>
            </div>
          </div>

          <div className="model-status">
            <span className="pill">Provider: {selectedProvider}</span>
            <span className="pill">Model: {selectedModel}</span>
          </div>

          <div className="provider-key-list">
            {modelGroups.map((group) => (
              <label className="field" key={group.provider}>
                <span>{group.provider}</span>
                <input
                  type="password"
                  value={providerKeys[group.provider] ?? ""}
                  onChange={(event) =>
                    setProviderKeys((current) => ({ ...current, [group.provider]: event.target.value }))
                  }
                  placeholder="Managed by backend"
                  disabled={!can("settings:model_config")}
                />
              </label>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
