import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { KnowledgeSourceList } from "../../components/knowledge/KnowledgeSourceList";
import { KnowledgeUploadPanel } from "../../components/knowledge/KnowledgeUploadPanel";
import { api } from "../../lib/api";
import { toErrorMessage } from "../../lib/format";

interface PendingImport {
  id: string;
  name: string;
  type: string;
  size: number;
  status: string;
}

export function KnowledgePage() {
  const queryClient = useQueryClient();
  const [pendingImports, setPendingImports] = useState<PendingImport[]>([]);
  const [selectedSource, setSelectedSource] = useState<string | null>(null);
  const [hiddenSources, setHiddenSources] = useState<string[]>([]);
  const [pageNotice, setPageNotice] = useState<string | null>(null);

  const sourcesQuery = useQuery({
    queryKey: ["knowledge-sources"],
    queryFn: () => api.getKnowledgeSources(),
  });

  const documentsQuery = useQuery({
    queryKey: ["knowledge-documents"],
    queryFn: () => api.getKnowledgeDocuments(undefined, 120),
  });

  const syncMutation = useMutation({
    mutationFn: (sourceName?: string) => api.syncKnowledge(sourceName),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["knowledge-sources"] }),
        queryClient.invalidateQueries({ queryKey: ["knowledge-documents"] }),
      ]);
    },
  });

  function addImports(files: File[]) {
    setPendingImports((current) => [
      ...files.map((file) => ({
        id: `${file.name}-${file.size}-${file.lastModified}`,
        name: file.name,
        type: file.type || file.name.split(".").pop() || "file",
        size: file.size,
        status: "Ready for backend upload endpoint",
      })),
      ...current,
    ]);
  }

  const visibleSources = (sourcesQuery.data ?? []).filter((source) => !hiddenSources.includes(source.source_name));
  const visibleDocuments = (documentsQuery.data ?? []).filter((document) => {
    if (hiddenSources.includes(document.source_name)) {
      return false;
    }
    return selectedSource ? document.source_name === selectedSource : true;
  });

  return (
    <div className="content-page reference-page knowledge-page">
      <header className="content-header split-header">
        <div>
          <span>Knowledge</span>
          <h1>Knowledge library</h1>
          <p>Upload documents to enable RAG retrieval with grounded citations.</p>
        </div>
        <button
          type="button"
          className="primary-action"
          onClick={() => document.getElementById("knowledge-file-input")?.click()}
        >
          Upload document
        </button>
      </header>

      <KnowledgeUploadPanel pendingImports={pendingImports} onImport={addImports} />

      {pageNotice ? <div className="permission-note">{pageNotice}</div> : null}
      {syncMutation.isError ? <div className="error-banner">{toErrorMessage(syncMutation.error)}</div> : null}
      {sourcesQuery.isError ? <div className="error-banner">{toErrorMessage(sourcesQuery.error)}</div> : null}
      {documentsQuery.isError ? <div className="error-banner">{toErrorMessage(documentsQuery.error)}</div> : null}

      <KnowledgeSourceList
        sources={visibleSources}
        documents={visibleDocuments}
        selectedSource={selectedSource}
        onViewSource={(sourceName) => setSelectedSource((current) => (current === sourceName ? null : sourceName))}
        onDeleteSource={(sourceName) => {
          setHiddenSources((current) => [...current, sourceName]);
          setPageNotice("This source is hidden in the UI. Add the backend delete endpoint to remove it permanently from the index.");
          if (selectedSource === sourceName) {
            setSelectedSource(null);
          }
        }}
        onSync={(sourceName) => syncMutation.mutate(sourceName)}
        isSyncing={syncMutation.isPending}
      />
    </div>
  );
}
