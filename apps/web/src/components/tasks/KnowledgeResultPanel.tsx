import { titleCase } from "../../lib/format";
import type { KnowledgeAnswerTraceResult, KnowledgeCitationResult, KnowledgeSearchResult } from "../../types";

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : null;
}

function readString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function readNumber(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}

function readStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }

  return value.flatMap((item) => (typeof item === "string" ? [item] : []));
}

function readCitation(value: unknown): KnowledgeCitationResult | null {
  const record = asRecord(value);
  if (!record) {
    return null;
  }

  const documentId = readString(record.document_id);
  const title = readString(record.title);
  const relativePath = readString(record.relative_path);
  const lineStart = readNumber(record.line_start);
  const lineEnd = readNumber(record.line_end);
  const snippet = readString(record.snippet);
  const score = readNumber(record.score);

  if (!documentId || !title || !relativePath || lineStart === null || lineEnd === null || !snippet || score === null) {
    return null;
  }

  return {
    document_id: documentId,
    source_name: readString(record.source_name) ?? "unknown",
    title,
    relative_path: relativePath,
    line_start: lineStart,
    line_end: lineEnd,
    snippet,
    score,
    metadata: asRecord(record.metadata) ?? {},
  };
}

function readAnswerTrace(value: unknown): KnowledgeAnswerTraceResult | null {
  const record = asRecord(value);
  if (!record) {
    return null;
  }

  const sourceName = readString(record.source_name);
  const sourcePath = readString(record.source_path);
  const strategy = readString(record.strategy);
  const topK = readNumber(record.top_k);
  const indexedDocumentCount = readNumber(record.indexed_document_count);
  const citationCount = readNumber(record.citation_count);
  const hallucinationRisk = readString(record.hallucination_risk);
  const rationale = readString(record.rationale);

  if (
    !sourceName ||
    !sourcePath ||
    !strategy ||
    topK === null ||
    indexedDocumentCount === null ||
    citationCount === null ||
    !hallucinationRisk ||
    !rationale
  ) {
    return null;
  }

  return {
    source_name: sourceName,
    source_path: sourcePath,
    selected_sources: readStringArray(record.selected_sources),
    strategy,
    route_kind: readString(record.route_kind) ?? "unknown",
    route_reason: readString(record.route_reason) ?? "No route reasoning recorded.",
    top_k: topK,
    indexed_document_count: indexedDocumentCount,
    selected_paths: readStringArray(record.selected_paths),
    matched_tokens: readStringArray(record.matched_tokens),
    token_coverage: readNumber(record.token_coverage) ?? 0,
    top_score: readNumber(record.top_score) ?? 0,
    citation_count: citationCount,
    hallucination_risk: hallucinationRisk,
    rationale,
  };
}

export function readKnowledgeSearchResult(value: unknown): KnowledgeSearchResult | null {
  const record = asRecord(value);
  if (!record) {
    return null;
  }

  const query = readString(record.query);
  const answer = readString(record.answer);
  const packagedContext = readString(record.packaged_context);
  const answerTrace = readAnswerTrace(record.answer_trace);

  if (!query || !answer || !packagedContext || !answerTrace) {
    return null;
  }

  const citations = Array.isArray(record.citations)
    ? record.citations.flatMap((citation) => {
        const parsed = readCitation(citation);
        return parsed ? [parsed] : [];
      })
    : [];

  return {
    query,
    answer,
    citations,
    answer_trace: answerTrace,
    packaged_context: packagedContext,
  };
}

interface AnswerBlock {
  kind: "paragraph" | "list";
  items: string[];
}

function parseAnswerBlocks(answer: string): AnswerBlock[] {
  return answer
    .split(/\n\s*\n/)
    .map((block) => block.trim())
    .filter(Boolean)
    .map((block) => {
      const lines = block
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean);

      if (lines.length > 0 && lines.every((line) => /^-\s+/.test(line))) {
        return {
          kind: "list" as const,
          items: lines.map((line) => line.replace(/^-\s+/, "")),
        };
      }

      return {
        kind: "paragraph" as const,
        items: [lines.join(" ")],
      };
    });
}

interface KnowledgeResultPanelProps {
  result: KnowledgeSearchResult | null;
}

export function KnowledgeResultPanel({ result }: KnowledgeResultPanelProps) {
  if (!result) {
    return null;
  }

  const answerBlocks = parseAnswerBlocks(result.answer);
  const sourceSummary =
    result.answer_trace.selected_sources.length > 0
      ? result.answer_trace.selected_sources.join(", ")
      : result.answer_trace.source_name;
  const primaryCitation = result.citations[0] ?? null;

  return (
    <div className="stack-sm">
      <section className="knowledge-answer-card">
        <div className="section-header">
          <div>
            <div className="eyebrow">User Readable Answer</div>
            <h3>What To Check First</h3>
          </div>
        </div>

        <div className="knowledge-answer-flow">
          {answerBlocks.map((block, index) =>
            block.kind === "list" ? (
              <ul className="knowledge-answer-list" key={`list-${index}`}>
                {block.items.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            ) : (
              <p
                className={index === 0 ? "knowledge-answer-lead" : "knowledge-answer-paragraph"}
                key={`paragraph-${index}`}
              >
                {block.items[0]}
              </p>
            ),
          )}
        </div>

        <div className="knowledge-evidence-summary">
          <div className="knowledge-evidence-item">
            <span>Evidence Summary</span>
            <strong>
              {result.answer_trace.citation_count} citation{result.answer_trace.citation_count === 1 ? "" : "s"} from{" "}
              {sourceSummary}
            </strong>
          </div>
          <div className="knowledge-evidence-item">
            <span>Confidence</span>
            <strong>{titleCase(result.answer_trace.hallucination_risk)}</strong>
          </div>
          {primaryCitation ? (
            <div className="knowledge-evidence-item knowledge-evidence-item-wide">
              <span>Primary Reference</span>
              <strong>
                {primaryCitation.source_name}:{primaryCitation.relative_path} lines {primaryCitation.line_start}-
                {primaryCitation.line_end}
              </strong>
            </div>
          ) : null}
        </div>
      </section>

      <details className="collapsible-panel">
        <summary className="collapsible-summary">
          <div>
            <div className="eyebrow">Technical View</div>
            <strong>Grounding and Retrieval Trace</strong>
          </div>
          <span className={`mini-pill finding-${result.answer_trace.hallucination_risk === "high" ? "error" : result.answer_trace.hallucination_risk === "medium" ? "warning" : "info"}`}>
            {titleCase(result.answer_trace.hallucination_risk)} risk
          </span>
        </summary>

        <div className="collapsible-content stack-sm">
          <div className="review-metadata-grid">
            <div className="review-metric">
              <span>Source</span>
              <strong>{result.answer_trace.source_name}</strong>
            </div>
            <div className="review-metric">
              <span>Strategy</span>
              <strong>{titleCase(result.answer_trace.strategy)}</strong>
            </div>
            <div className="review-metric">
              <span>Route</span>
              <strong>{titleCase(result.answer_trace.route_kind)}</strong>
            </div>
            <div className="review-metric">
              <span>Citations</span>
              <strong>{result.answer_trace.citation_count}</strong>
            </div>
            <div className="review-metric">
              <span>Coverage</span>
              <strong>{result.answer_trace.token_coverage}</strong>
            </div>
            <div className="review-metric">
              <span>Top Score</span>
              <strong>{result.answer_trace.top_score}</strong>
            </div>
          </div>

          <div className="review-item">
            <div className="review-item-head">
              <strong>Answer Trace</strong>
              <span className={`mini-pill finding-${result.answer_trace.hallucination_risk === "high" ? "error" : result.answer_trace.hallucination_risk === "medium" ? "warning" : "info"}`}>
                {result.answer_trace.hallucination_risk}
              </span>
            </div>
            <p>{result.answer_trace.rationale}</p>
            <div className="muted-text">{result.answer_trace.route_reason}</div>
            {result.answer_trace.matched_tokens.length > 0 ? (
              <div className="muted-text">Matched tokens: {result.answer_trace.matched_tokens.join(", ")}</div>
            ) : null}
            {result.answer_trace.selected_sources.length > 0 ? (
              <div className="muted-text">Sources: {result.answer_trace.selected_sources.join(", ")}</div>
            ) : null}
            {result.answer_trace.selected_paths.length > 0 ? (
              <div className="muted-text">{result.answer_trace.selected_paths.join(", ")}</div>
            ) : null}
          </div>
        </div>
      </details>

      <details className="collapsible-panel">
        <summary className="collapsible-summary">
          <div>
            <div className="eyebrow">Repository Evidence</div>
            <strong>Code Citations</strong>
          </div>
          <span className="mini-pill">{result.citations.length}</span>
        </summary>

        <div className="collapsible-content">
          {result.citations.length > 0 ? (
            <div className="review-list">
              {result.citations.map((citation) => (
                <article className="review-item" key={`${citation.document_id}-${citation.line_start}`}>
                  <div className="review-item-head">
                    <strong>
                      {citation.source_name}:{citation.relative_path}
                    </strong>
                    <span className="mini-pill">score {citation.score}</span>
                  </div>
                  <div className="muted-text">
                    {citation.title} lines {citation.line_start}-{citation.line_end}
                  </div>
                  <pre className="json-panel">{citation.snippet}</pre>
                </article>
              ))}
            </div>
          ) : (
            <p>No repository citations were returned.</p>
          )}
        </div>
      </details>
    </div>
  );
}
