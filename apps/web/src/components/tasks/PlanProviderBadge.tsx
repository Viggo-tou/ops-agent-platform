interface PlanProviderBadgeProps {
  providerName: string | null;
  providerMode?: string | null;
  usedFallback?: boolean;
}

export function PlanProviderBadge({
  providerName,
  providerMode,
  usedFallback = false,
}: PlanProviderBadgeProps) {
  const provider = (providerName ?? "unknown").toLowerCase();
  const toneClass =
    provider === "openai"
      ? "provider-openai"
      : provider === "minimax"
        ? "provider-minimax"
        : provider === "mock"
          ? "provider-mock"
          : "provider-unknown";
  const label = providerName ? providerName : "unknown";

  return (
    <div className="provider-stack">
      <span className={`pill ${toneClass}`}>{label}</span>
      {providerMode ? <span className="muted-text compact-line">{providerMode.replace(/_/g, " ")}</span> : null}
      {usedFallback ? <span className="mini-pill provider-fallback">fallback</span> : null}
    </div>
  );
}
