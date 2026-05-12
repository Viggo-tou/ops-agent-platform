import type { KnowledgeDocumentSummary, KnowledgeSourceDescriptor } from "../../types";
import { PermissionGuard } from "../auth/PermissionGuard";

interface KnowledgeSourceListProps {
  sources: KnowledgeSourceDescriptor[];
  documents: KnowledgeDocumentSummary[];
  selectedSource: string | null;
  onViewSource: (sourceName: string) => void;
  onDeleteDocument: (documentId: string) => void;
  onDeleteSource: (sourceName: string) => void;
  onSync: (sourceName?: string) => void;
  isBusy?: boolean;
}

type FileRow =
  | {
      kind: "document";
      id: string;
      name: string;
      path: string;
      date: Date;
      sourceName: string;
    }
  | {
      kind: "source";
      id: string;
      name: string;
      path: string;
      date: Date;
      sourceName: string;
    };

export function KnowledgeSourceList({
  sources,
  documents,
  selectedSource,
  onViewSource,
  onDeleteDocument,
  onDeleteSource,
  onSync,
  isBusy,
}: KnowledgeSourceListProps) {
  const fileRows: FileRow[] =
    documents.length > 0
      ? documents.map((document) => ({
          kind: "document" as const,
          id: document.id,
          name: document.title || document.relative_path,
          path: `${document.source_name}:${document.relative_path}`,
          date: new Date(document.updated_at),
          sourceName: document.source_name,
        }))
      : sources.map((source) => ({
          kind: "source" as const,
          id: source.source_name,
          name: source.source_name,
          path: source.source_path,
          date: new Date(),
          sourceName: source.source_name,
        }));

  function formatShortDate(date: Date) {
    return `${date.getFullYear()}/${date.getMonth() + 1}/${date.getDate()}`;
  }

  function handleDelete(row: FileRow) {
    if (row.kind === "document") {
      onDeleteDocument(row.id);
    } else {
      onDeleteSource(row.sourceName);
    }
  }

  return (
    <section className="simple-card knowledge-file-list-card">
      <div className="section-heading">
        <div>
          <h2>知识文件</h2>
        </div>
        <PermissionGuard permission="knowledge:upload">
          <button type="button" className="subtle-button" onClick={() => onSync()} disabled={isBusy}>
            {isBusy ? "处理中" : "重新索引"}
          </button>
        </PermissionGuard>
      </div>

      {fileRows.length > 0 ? (
        <div className="knowledge-file-list">
          {fileRows.map((row) => (
            <article key={`${row.kind}-${row.id}`} className="knowledge-file-row">
              <div className="file-icon">📄</div>
              <div>
                <strong>{row.name}</strong>
                <span>
                  {formatShortDate(row.date)} · ✓ 已就绪
                  {row.kind === "source" ? " · 来源" : ""}
                </span>
                <small>{row.path}</small>
              </div>
              <div className="file-actions">
                <PermissionGuard permission="knowledge:upload">
                  <button
                    type="button"
                    onClick={() => onSync(row.sourceName)}
                    disabled={isBusy}
                    aria-label={`重新索引 ${row.name}`}
                  >
                    ⬇️
                  </button>
                </PermissionGuard>
                <button
                  type="button"
                  onClick={() => onViewSource(row.sourceName)}
                  aria-label={`${selectedSource === row.sourceName ? "显示全部" : "查看"} ${row.name}`}
                >
                  👁️
                </button>
                <PermissionGuard permission="knowledge:delete">
                  <button
                    type="button"
                    onClick={() => handleDelete(row)}
                    disabled={isBusy}
                    aria-label={`删除 ${row.name}`}
                  >
                    🗑️
                  </button>
                </PermissionGuard>
              </div>
            </article>
          ))}
        </div>
      ) : (
        <p className="muted-copy">暂无知识文件</p>
      )}
    </section>
  );
}
