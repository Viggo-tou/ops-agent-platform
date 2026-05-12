import { useState } from "react";

interface DiffBlockProps {
  diff: string;
  summary: string;
}

export function DiffBlock({ diff, summary }: DiffBlockProps) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="diff-block">
      <button className="diff-toggle" type="button" onClick={() => setExpanded(!expanded)}>
        {expanded ? "▼" : "▶"} {summary}
      </button>
      {expanded ? <pre className="diff-content">{diff}</pre> : null}
    </div>
  );
}
