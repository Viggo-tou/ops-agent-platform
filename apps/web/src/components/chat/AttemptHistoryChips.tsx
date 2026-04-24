interface AttemptHistoryEntry {
  provider: string;
  status: string;
  error?: string;
}

interface AttemptHistoryChipsProps {
  attempts: AttemptHistoryEntry[];
}

export function AttemptHistoryChips({ attempts }: AttemptHistoryChipsProps) {
  if (attempts.length === 0) {
    return null;
  }
  // A single successful attempt is not interesting enough to visualise; we
  // only show the provenance when there was a fallback (2+ attempts) OR when
  // the single attempt failed (informational).
  const showChips =
    attempts.length > 1 || attempts.some((a) => a.status !== "succeeded");
  if (!showChips) {
    return null;
  }
  return (
    <div className="attempt-history-chips" data-testid="attempt-history-chips">
      <span className="attempt-history-label">Provider:</span>
      {attempts.map((attempt, idx) => {
        const ok = attempt.status === "succeeded";
        const isLast = idx === attempts.length - 1;
        const className = ok
          ? "attempt-chip attempt-chip-ok"
          : "attempt-chip attempt-chip-fail";
        const tooltip = attempt.error
          ? `${attempt.provider}: ${attempt.status} — ${attempt.error}`
          : `${attempt.provider}: ${attempt.status}`;
        return (
          <span key={idx} className="attempt-chip-group">
            <span className={className} title={tooltip}>
              <span className="attempt-chip-dot" aria-hidden="true">
                {ok ? "✓" : "✗"}
              </span>
              <span className="attempt-chip-name">{attempt.provider}</span>
            </span>
            {!isLast ? <span className="attempt-chip-arrow">→</span> : null}
          </span>
        );
      })}
    </div>
  );
}
