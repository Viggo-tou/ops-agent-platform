import type { KnowledgeDocumentSummary, KnowledgeSourceDescriptor } from "../../types";
import { formatDateTime } from "../../lib/format";
import { PermissionGuard } from "../auth/PermissionGuard";

interface KnowledgeSourceListProps {
  sources: KnowledgeSourceDescriptor[];
  documents: KnowledgeDocumentSummary[];
  selectedSource: string | null;
  onViewSource: (sourceName: string) => void;
  onDeleteSource: (sourceName: string) => void;
  onSync: (sourceName?: string) => void;
  isSyncing?: boolean;
}

export function KnowledgeSourceList({
  sources,
  documents,
  selectedSource,
  onViewSource,
  onDeleteSource,
  onSync,
  isSyncing,
}: KnowledgeSourceListProps) {
  return (
    <div className="knowledge-grid">
      <section className="simple-card">
        <div className="section-heading">
          <div>
            <span>Sources</span>
            <h2>Uploaded sources</h2>
          </div>
          <PermissionGuard permission="knowledge:upload">
            <button type="button" className="subtle-button" onClick={() => onSync()} disabled={isSyncing}>
              {isSyncing ? "Indexing" : "Re-index"}
            </button>
          </PermissionGuard>
        </div>

        {sources.length > 0 ? (
          <div className="source-list">
            {sources.map((source) => (
              <article key={source.source_name} className="source-card">
                <strong>{source.source_name}</strong>
                <span>{source.indexed_document_count} documents</span>
                <small>{source.source_path}</small>
                <span className="source-status">Ready</span>
                <div className="source-actions">
                  <button type="button" onClick={() => onViewSource(source.source_name)}>
                    {selectedSource === source.source_name ? "Show all" : "View"}
                  </button>
                  <PermissionGuard permission="knowledge:upload">
                    <button type="button" onClick={() => onSync(source.source_name)} disabled={isSyncing}>
                      Re-index
                    </button>
                  </PermissionGuard>
                  <PermissionGuard
                    permission="knowledge:delete"
                    fallback={<span className="muted-inline">Delete requires admin access.</span>}
                  >
                    <button type="button" onClick={() => onDeleteSource(source.source_name)}>
                      Delete
                    </button>
                  </PermissionGuard>
                </div>
              </article>
            ))}
          </div>
        ) : (
          <p className="muted-copy">No knowledge sources are registered yet.</p>
        )}
      </section>

      <section className="simple-card">
        <div className="section-heading">
          <div>
            <span>Documents</span>
            <h2>{selectedSource ? `Files in ${selectedSource}` : "Indexed files"}</h2>
          </div>
        </div>

        <div className="document-list">
          {documents.map((document) => (
            <article key={document.id} className="document-row">
              <div>
                <strong>{document.title}</strong>
                <span>{document.source_name}:{document.relative_path}</span>
              </div>
              <div>
                <small>{document.extension || "file"}</small>
                <small>{document.line_count} lines</small>
                <small>{formatDateTime(document.updated_at)}</small>
              </div>
            </article>
          ))}
          {documents.length === 0 ? <p className="muted-copy">No indexed documents to show.</p> : null}
        </div>
      </section>
    </div>
  );
}
