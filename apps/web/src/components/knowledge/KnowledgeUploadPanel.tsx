import { useRef, useState } from "react";

import { type Permission, useAuth } from "../../lib/auth";

interface KnowledgeUploadPanelProps {
  onImport: (files: File[]) => void;
  isUploading?: boolean;
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

export function KnowledgeUploadPanel({ onImport, isUploading }: KnowledgeUploadPanelProps) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const guardedImport = useGuardedImport("knowledge:upload", onImport);
  const { can } = useAuth();
  const [isDragging, setIsDragging] = useState(false);
  const canUpload = can("knowledge:upload");

  return (
    <section
      className={["upload-dropzone", canUpload ? "" : "disabled", isDragging ? "is-dragging" : ""]
        .filter(Boolean)
        .join(" ")}
      role="button"
      tabIndex={0}
      onClick={() => fileInputRef.current?.click()}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          fileInputRef.current?.click();
        }
      }}
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
        <div className="upload-icon">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M12 16V4m0 0L7 9m5-5 5 5" />
            <path d="M5 15v3a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-3" />
          </svg>
        </div>
        <h2>{isUploading ? "正在上传..." : "拖拽文件到此处，或点击上传"}</h2>
        <p>支持 Markdown、TXT、JSON、YAML、Properties 格式</p>
        {!canUpload ? (
          <div className="permission-note">Your role can view knowledge sources but cannot upload new material.</div>
        ) : null}
      </div>

      <input
        id="knowledge-file-input"
        ref={fileInputRef}
        type="file"
        multiple
        hidden
        accept=".md,.txt,.json,.yml,.yaml,.properties"
        onChange={(event) => {
          const selected = Array.from(event.target.files ?? []);
          if (selected.length === 0) {
            return;
          }
          guardedImport(selected);
          event.target.value = "";
        }}
      />
    </section>
  );
}
