import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { KnowledgeSourceList } from "../../components/knowledge/KnowledgeSourceList";
import { KnowledgeUploadPanel } from "../../components/knowledge/KnowledgeUploadPanel";
import { api } from "../../lib/api";
import { toErrorMessage } from "../../lib/format";
import type { KnowledgeUploadSkipped } from "../../types";

export function KnowledgePage() {
  const queryClient = useQueryClient();
  const [selectedSource, setSelectedSource] = useState<string | null>(null);
  const [pageNotice, setPageNotice] = useState<string | null>(null);
  const [uploadErrors, setUploadErrors] = useState<KnowledgeUploadSkipped[]>([]);

  const sourcesQuery = useQuery({
    queryKey: ["knowledge-sources"],
    queryFn: () => api.getKnowledgeSources(),
  });

  const documentsQuery = useQuery({
    queryKey: ["knowledge-documents"],
    queryFn: () => api.getKnowledgeDocuments(undefined, 200),
  });

  async function invalidateKnowledge() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["knowledge-sources"] }),
      queryClient.invalidateQueries({ queryKey: ["knowledge-documents"] }),
    ]);
  }

  const syncMutation = useMutation({
    mutationFn: (sourceName?: string) => api.syncKnowledge(sourceName),
    onSuccess: (result) => {
      setPageNotice(
        `已同步 ${result.source_name}：新增 ${result.indexed_documents}，更新 ${result.updated_documents}，移除 ${result.removed_documents}`,
      );
      void invalidateKnowledge();
    },
  });

  const uploadMutation = useMutation({
    mutationFn: (files: File[]) => api.uploadKnowledgeFiles(files),
    onSuccess: (result) => {
      setUploadErrors(result.skipped);
      setPageNotice(
        `上传完成：${result.indexed_documents.length} 个文件已入库${
          result.skipped.length > 0 ? `，${result.skipped.length} 个被跳过` : ""
        }`,
      );
      void invalidateKnowledge();
    },
  });

  const deleteDocumentMutation = useMutation({
    mutationFn: (documentId: string) => api.deleteKnowledgeDocument(documentId),
    onSuccess: (result) => {
      setPageNotice(`已删除 1 个文档（${result.source_name}）`);
      void invalidateKnowledge();
    },
  });

  const deleteSourceMutation = useMutation({
    mutationFn: (sourceName: string) => api.deleteKnowledgeSource(sourceName),
    onSuccess: (result) => {
      setPageNotice(
        `已删除来源 ${result.source_name}：共 ${result.removed_documents} 个文档${
          result.removed_from_disk ? "，并清理了上传目录" : ""
        }`,
      );
      if (selectedSource === result.source_name) {
        setSelectedSource(null);
      }
      void invalidateKnowledge();
    },
  });

  function handleImport(files: File[]) {
    if (files.length === 0) {
      return;
    }
    setUploadErrors([]);
    uploadMutation.mutate(files);
  }

  const visibleSources = sourcesQuery.data ?? [];
  const visibleDocuments = useMemo(() => {
    const documents = documentsQuery.data ?? [];
    return selectedSource ? documents.filter((document) => document.source_name === selectedSource) : documents;
  }, [documentsQuery.data, selectedSource]);

  const mutationError =
    uploadMutation.error ??
    deleteDocumentMutation.error ??
    deleteSourceMutation.error ??
    syncMutation.error;

  return (
    <div className="content-page reference-page knowledge-page">
      <header className="content-header split-header">
        <div>
          <h1>知识库</h1>
          <p>上传文档以启用 RAG 智能检索功能</p>
        </div>
        <button
          type="button"
          className="primary-action"
          onClick={() => document.getElementById("knowledge-file-input")?.click()}
        >
          📤 上传文档
        </button>
      </header>

      <section className="embedding-status-card" aria-label="Embedding status">
        <div className="status-dot" />
        <div>
          <h2>使用本地 Ollama Embedding</h2>
          <p>已检测到 nomic-embed-text 模型</p>
        </div>
        <span className="toggle-switch on" aria-hidden="true">
          <span />
        </span>
      </section>

      <KnowledgeUploadPanel
        onImport={handleImport}
        isUploading={uploadMutation.isPending}
      />

      {pageNotice ? <div className="permission-note">{pageNotice}</div> : null}
      {uploadErrors.length > 0 ? (
        <div className="error-banner">
          {uploadErrors.map((item) => (
            <div key={`${item.file_name}-${item.reason}`}>
              跳过 {item.file_name}：{item.reason}
            </div>
          ))}
        </div>
      ) : null}
      {mutationError ? <div className="error-banner">{toErrorMessage(mutationError)}</div> : null}
      {sourcesQuery.isError ? <div className="error-banner">{toErrorMessage(sourcesQuery.error)}</div> : null}
      {documentsQuery.isError ? <div className="error-banner">{toErrorMessage(documentsQuery.error)}</div> : null}

      <KnowledgeSourceList
        sources={visibleSources}
        documents={visibleDocuments}
        selectedSource={selectedSource}
        onViewSource={(sourceName) => setSelectedSource((current) => (current === sourceName ? null : sourceName))}
        onDeleteDocument={(documentId) => deleteDocumentMutation.mutate(documentId)}
        onDeleteSource={(sourceName) => deleteSourceMutation.mutate(sourceName)}
        onSync={(sourceName) => syncMutation.mutate(sourceName)}
        isBusy={
          syncMutation.isPending ||
          deleteDocumentMutation.isPending ||
          deleteSourceMutation.isPending
        }
      />
    </div>
  );
}
