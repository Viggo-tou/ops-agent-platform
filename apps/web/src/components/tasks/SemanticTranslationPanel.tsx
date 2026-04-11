import type { SemanticTranslationDocument } from "../../types";
import { titleCase } from "../../lib/format";

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function readStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((item): item is string => typeof item === "string" && item.trim().length > 0);
}

export function readSemanticTranslationDocument(
  payload: Record<string, unknown> | null,
): SemanticTranslationDocument | null {
  if (!payload) {
    return null;
  }

  const translationId = readString(payload.translation_id);
  const taskId = readString(payload.task_id);
  const normalizedRequest = readString(payload.normalized_request);
  const intent = readString(payload.intent);
  const workType = readString(payload.work_type);
  const objective = readString(payload.objective);
  const schemaVersion = readString(payload.schema_version);

  if (!translationId || !taskId || !normalizedRequest || !intent || !workType || !objective || !schemaVersion) {
    return null;
  }

  return {
    schema_version: schemaVersion,
    translation_id: translationId,
    task_id: taskId,
    normalized_request: normalizedRequest,
    intent,
    work_type: workType as SemanticTranslationDocument["work_type"],
    objective,
    issue_key: readString(payload.issue_key),
    issue_url: readString(payload.issue_url),
    candidate_modules: readStringArray(payload.candidate_modules),
    search_queries: readStringArray(payload.search_queries),
    constraints: readStringArray(payload.constraints),
    requested_outputs: readStringArray(payload.requested_outputs),
    grounding_terms: readStringArray(payload.grounding_terms),
    missing_information: readStringArray(payload.missing_information),
    confidence: typeof payload.confidence === "number" ? payload.confidence : 0,
    provider:
      payload.provider && typeof payload.provider === "object"
        ? (payload.provider as Record<string, unknown>)
        : null,
  };
}

interface SemanticTranslationPanelProps {
  translation: SemanticTranslationDocument | null;
  rawTranslationJson: Record<string, unknown> | null;
}

export function SemanticTranslationPanel({
  translation,
  rawTranslationJson,
}: SemanticTranslationPanelProps) {
  if (!translation) {
    return <p>No semantic translation has been recorded for this task.</p>;
  }

  const providerName =
    translation.provider && typeof translation.provider.name === "string"
      ? translation.provider.name
      : "unknown";
  const providerMode =
    translation.provider && typeof translation.provider.mode === "string"
      ? translation.provider.mode
      : null;

  return (
    <div className="stack tight-stack">
      <p className="lead-copy">{translation.objective}</p>

      <dl className="metadata-grid compact">
        <div>
          <dt>Intent</dt>
          <dd>{titleCase(translation.intent.replace(/_/g, " "))}</dd>
        </div>
        <div>
          <dt>Work Type</dt>
          <dd>{titleCase(translation.work_type)}</dd>
        </div>
        <div>
          <dt>Confidence</dt>
          <dd>{Math.round(translation.confidence * 100)}%</dd>
        </div>
        <div>
          <dt>Provider</dt>
          <dd>{providerMode ? `${providerName} / ${providerMode}` : providerName}</dd>
        </div>
        <div>
          <dt>Issue Key</dt>
          <dd>{translation.issue_key ?? "N/A"}</dd>
        </div>
        <div>
          <dt>Issue URL</dt>
          <dd>
            {translation.issue_url ? (
              <a className="link-button subtle-link" href={translation.issue_url} target="_blank" rel="noreferrer">
                Open Jira Link
              </a>
            ) : (
              "N/A"
            )}
          </dd>
        </div>
      </dl>

      <div className="translation-summary-card">
        <span>Normalized Request</span>
        <strong>{translation.normalized_request}</strong>
      </div>

      {translation.candidate_modules.length > 0 ? (
        <div className="stack tight-stack">
          <span className="detail-subheading">Candidate Modules</span>
          <div className="tag-row">
            {translation.candidate_modules.map((moduleName) => (
              <span key={moduleName} className="mini-pill">
                {moduleName}
              </span>
            ))}
          </div>
        </div>
      ) : null}

      {translation.search_queries.length > 0 ? (
        <div className="stack tight-stack">
          <span className="detail-subheading">Search Queries</span>
          <ul className="detail-list">
            {translation.search_queries.map((query) => (
              <li key={query}>{query}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {translation.constraints.length > 0 ? (
        <div className="stack tight-stack">
          <span className="detail-subheading">Constraints</span>
          <ul className="detail-list">
            {translation.constraints.map((constraint) => (
              <li key={constraint}>{constraint}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {translation.requested_outputs.length > 0 ? (
        <div className="stack tight-stack">
          <span className="detail-subheading">Requested Outputs</span>
          <div className="tag-row">
            {translation.requested_outputs.map((output) => (
              <span key={output} className="mini-pill secondary-pill">
                {output}
              </span>
            ))}
          </div>
        </div>
      ) : null}

      {translation.grounding_terms.length > 0 ? (
        <div className="stack tight-stack">
          <span className="detail-subheading">Grounding Terms</span>
          <div className="tag-row">
            {translation.grounding_terms.map((term) => (
              <span key={term} className="mini-pill muted-pill">
                {term}
              </span>
            ))}
          </div>
        </div>
      ) : null}

      {translation.missing_information.length > 0 ? (
        <div className="warning-banner">
          Missing information: {translation.missing_information.join(", ")}
        </div>
      ) : null}

      {rawTranslationJson ? (
        <details className="details-panel">
          <summary>Raw Translation JSON</summary>
          <pre className="json-panel">{JSON.stringify(rawTranslationJson, null, 2)}</pre>
        </details>
      ) : null}
    </div>
  );
}
