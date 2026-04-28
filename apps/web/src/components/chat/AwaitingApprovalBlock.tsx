interface DiagnosisOutput {
  summary: string;
  root_cause: string;
  likely_fix: string;
  confidence: "high" | "medium" | "low";
  related_files: string[];
}

function readRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function readString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function readRelatedFiles(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string" && Boolean(item.trim()))
    : [];
}

export function readFailureDiagnosis(value: unknown): DiagnosisOutput | null {
  const record = readRecord(value);
  if (!record) return null;
  const nested = readRecord(record.failure_diagnosis);
  const result = readRecord(record.result);
  const resultNested = readRecord(result?.failure_diagnosis);
  const source = nested ?? resultNested;
  if (!source) return null;

  const confidence = readString(source.confidence);
  if (confidence !== "high" && confidence !== "medium" && confidence !== "low") {
    return null;
  }

  const summary = readString(source.summary).trim();
  if (!summary) return null;

  return {
    summary,
    root_cause: readString(source.root_cause).trim(),
    likely_fix: readString(source.likely_fix).trim(),
    confidence,
    related_files: readRelatedFiles(source.related_files),
  };
}

interface AwaitingApprovalBlockProps {
  diagnosis: DiagnosisOutput;
}

export function AwaitingApprovalBlock({ diagnosis }: AwaitingApprovalBlockProps) {
  const confidenceLabel =
    diagnosis.confidence === "low" ? "Best guess" : diagnosis.confidence === "medium" ? "Medium confidence" : "High confidence";

  return (
    <section className="failure-diagnosis-block">
      <div className="failure-diagnosis-header">
        <strong>{diagnosis.summary}</strong>
        <span className={`confidence-badge confidence-${diagnosis.confidence}`}>{confidenceLabel}</span>
      </div>

      <details className="failure-diagnosis-details">
        <summary>Technical detail</summary>
        {diagnosis.root_cause ? (
          <div>
            <h4>Root cause</h4>
            <p>{diagnosis.root_cause}</p>
          </div>
        ) : null}
        {diagnosis.likely_fix ? (
          <div>
            <h4>Likely fix</h4>
            <p>{diagnosis.likely_fix}</p>
          </div>
        ) : null}
        {diagnosis.related_files.length > 0 ? (
          <div>
            <h4>Related files</h4>
            <ul>
              {diagnosis.related_files.map((file) => (
                <li key={file}>{file}</li>
              ))}
            </ul>
          </div>
        ) : null}
      </details>
    </section>
  );
}
