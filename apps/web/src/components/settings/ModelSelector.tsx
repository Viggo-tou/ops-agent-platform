import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { api } from "../../lib/api";
import { useAuth } from "../../lib/auth";

const ALL_PROVIDERS = "全部";

export function ModelSelector() {
  const { can } = useAuth();
  const queryClient = useQueryClient();
  const canEditModelConfig = can("settings:model_config");
  const [activeTab, setActiveTab] = useState<"models" | "api">("models");
  const [providerFilter, setProviderFilter] = useState(ALL_PROVIDERS);
  const [providerKeys, setProviderKeys] = useState<Record<string, string>>({});

  const providersQuery = useQuery({
    queryKey: ["model-providers"],
    queryFn: () => api.getModelProviders(),
  });

  const selectedModelQuery = useQuery({
    queryKey: ["selected-model"],
    queryFn: () => api.getSelectedModel(),
  });

  const selectModelMutation = useMutation({
    mutationFn: (modelId: string) => api.setSelectedModel({ model_id: modelId }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["selected-model"] });
    },
  });

  const providers = providersQuery.data ?? [];
  const selectedModelId = selectedModelQuery.data?.model_id ?? null;
  const selectedGroup = providers.find((group) => group.models.some((model) => model.id === selectedModelId));
  const selectedEntry = selectedGroup?.models.find((model) => model.id === selectedModelId);
  const selectedProvider = selectedGroup?.name ?? "Not selected";
  const selectedModel = selectedEntry?.display_name ?? "Not selected";
  const providerChips = providersQuery.isLoading ? [] : [ALL_PROVIDERS, ...providers.map((provider) => provider.name)];
  const visibleGroups = useMemo(
    () => (providerFilter === ALL_PROVIDERS ? providers : providers.filter((group) => group.name === providerFilter)),
    [providerFilter, providers],
  );

  function selectModel(modelId: string) {
    if (!canEditModelConfig) {
      return;
    }
    selectModelMutation.mutate(modelId);
  }

  return (
    <div className="settings-panel">
      <div className="settings-tab-row" role="tablist" aria-label="Settings sections">
        <button className={activeTab === "models" ? "tab-button active" : "tab-button"} type="button" onClick={() => setActiveTab("models")}>
          模型选择
        </button>
        <button className={activeTab === "api" ? "tab-button active" : "tab-button"} type="button" onClick={() => setActiveTab("api")}>
          API 配置
        </button>
      </div>

      {activeTab === "models" ? (
        <section className="settings-section">
          <div className="settings-section-head">
            <div className="settings-icon">
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M7 8V7a5 5 0 0 1 10 0v1" />
                <path d="M5 8h14l-1 12H6L5 8Z" />
              </svg>
            </div>
            <div>
              <h2>选择模型</h2>
              <p>选择适合您需求的 AI 模型</p>
            </div>
          </div>

          {!canEditModelConfig ? (
            <div className="permission-note">Your role can view settings but cannot change model configuration.</div>
          ) : null}

          <div className="provider-chip-row" aria-label="Provider filter">
            {providerChips.map((provider) => (
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
                  key={`${group.name}-${model.id}`}
                  type="button"
                  className={model.id === selectedModelId ? "model-row-card selected" : "model-row-card"}
                  onClick={() => selectModel(model.id)}
                  disabled={!canEditModelConfig || selectModelMutation.isPending}
                >
                  <span>
                    <strong>{model.display_name}</strong>
                    <small>{group.name}</small>
                  </span>
                  <span>{group.note}</span>
                  <i>{model.id === selectedModelId ? "✓" : ""}</i>
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
            {providers.map((group) => (
              <label className="field" key={group.name}>
                <span>{group.name}</span>
                <input
                  type="password"
                  value={providerKeys[group.name] ?? ""}
                  onChange={(event) =>
                    setProviderKeys((current) => ({ ...current, [group.name]: event.target.value }))
                  }
                  placeholder="Managed by backend"
                  disabled={!canEditModelConfig}
                />
              </label>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
