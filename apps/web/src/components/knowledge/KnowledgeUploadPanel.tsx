import { useRef, useState } from "react";

import { type Permission, useAuth } from "../../lib/auth";

interface PendingImport {
  id: string;
  name: string;
  type: string;
  size: number;
  status: string;
}

interface KnowledgeUploadPanelProps {
  pendingImports: PendingImport[];
  onImport: (files: File[]) => void;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${Math.round(bytes / 1024)} KB`;
  }
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function useGuardedImport(permission: Permission, onImport: (files: File[]) => void) {
  const { can } = useAuth();
  return (files: File[]) => {
    if (!can(permission)) {
      return false;
    }
    onImport(files);
    return true;
  };
}

export function KnowledgeUploadPanel({ pendingImports, onImport }: KnowledgeUploadPanelProps) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const folderInputRef = useRef<HTMLInputElement | null>(null);
  const zipInputRef = useRef<HTMLInputElement | null>(null);
  const guardedImport = useGuardedImport("knowledge:upload", onImport);
  const { can } = useAuth();
  const [isDragging, setIsDragging] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const canUpload = can("knowledge:upload");

  return (
    <section
      className={["upload-panel", canUpload ? "" : "disabled", isDragging ? "is-dragging" : ""]
        .filter(Boolean)
        .join(" ")}
      onDragEnter={(event) => {
        event.preventDefault();
        setIsDragging(true);
      }}
      onDragOver={(event) => event.preventDefault()}
      onDragLeave={() => setIsDragging(false)}
      onDrop={(event) => {
        event.preventDefault();
        setIsDragging(false);
        guardedImport(Array.from(event.dataTransfer.files));
      }}
    >
      <div>
        <div className="upload-icon">Upload</div>
        <h2>Import knowledge</h2>
        <p>Drag files here, or choose files, folders, and zip archives for indexing.</p>
        {!canUpload ? (
          <div className="permission-note">Your role can view knowledge sources but cannot upload new material.</div>
        ) : null}
        {notice ? <div className="permission-note">{notice}</div> : null}
      </div>

      <div className="upload-actions">
        <button type="button" onClick={() => fileInputRef.current?.click()} disabled={!canUpload}>
          Choose files
        </button>
        <button type="button" onClick={() => folderInputRef.current?.click()} disabled={!canUpload}>
          Choose folder
        </button>
        <button type="button" onClick={() => zipInputRef.current?.click()} disabled={!canUpload}>
          Choose zip
        </button>
        <button
          type="button"
          onClick={() =>
            setNotice(
              "Browser apps cannot read a local path until the backend grants access. Use files, folders, or zip now; this entry is ready for a desktop or backend path picker.",
            )
          }
          disabled={!canUpload}
        >
          Local path
        </button>
      </div>

      <input
        id="knowledge-file-input"
        ref={fileInputRef}
        type="file"
        multiple
        hidden
        accept=".txt,.md,.pdf,.json,.csv,.zip,.kt,.java,.ts,.tsx,.py"
        onChange={(event) => guardedImport(Array.from(event.target.files ?? []))}
      />
      <input
        ref={folderInputRef}
        type="file"
        multiple
        hidden
        {...({ webkitdirectory: "", directory: "" } as Record<string, string>)}
        onChange={(event) => guardedImport(Array.from(event.target.files ?? []))}
      />
      <input
        ref={zipInputRef}
        type="file"
        hidden
        accept=".zip"
        onChange={(event) => guardedImport(Array.from(event.target.files ?? []))}
      />

      {pendingImports.length > 0 ? (
        <div className="pending-imports">
          {pendingImports.map((item) => (
            <div key={item.id} className="pending-import">
              <strong>{item.name}</strong>
              <span>
                {item.type || "file"} / {formatSize(item.size)} / {item.status}
              </span>
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}
