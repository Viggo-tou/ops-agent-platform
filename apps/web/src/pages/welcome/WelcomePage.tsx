import { useNavigate } from "react-router-dom";

export function WelcomePage() {
  const nav = useNavigate();
  return (
    <main className="welcome-page">
      <section className="welcome-hero">
        <div className="welcome-brand">Ops Agent Platform</div>
        <h1 className="welcome-title">
          Automate development tasks under audit-grade governance.
        </h1>
        <p className="welcome-tagline">
          Multi-stage agent pipeline with codegen, compile gate, symbol
          verification, semantic review, and human approval queue. Provider-
          agnostic codegen runtime (Claude Code / DeepSeek / Codex) with live
          task stream and conversational follow-up.
        </p>

        <ul className="welcome-features">
          <li>
            <strong>Codegen</strong> — Real working diffs, no hallucinated
            symbols. 4-leg correctness defense.
          </li>
          <li>
            <strong>Governance</strong> — RBAC, approval gates, risk-tiered
            policy rules. Auditable trail per task.
          </li>
          <li>
            <strong>Live</strong> — SSE-streamed pipeline progress and
            conversational continuation after task completion.
          </li>
          <li>
            <strong>Integrations</strong> — Jira, GitHub, Slack/Teams (optional,
            opt-in per workspace).
          </li>
        </ul>

        <div className="welcome-actions">
          <button className="button primary" onClick={() => nav("/login")}>
            Sign in
          </button>
          <button className="button ghost" onClick={() => nav("/login")}>
            Try demo
          </button>
        </div>

        <p className="welcome-fineprint">
          Tutor / reviewer? Click <strong>Try demo</strong> to enter as the
          default operator account.
        </p>
      </section>
    </main>
  );
}
