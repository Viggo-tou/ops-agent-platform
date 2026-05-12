import { useRef, useState } from "react";

export interface SourceOption {
  name: string;
  origin: string;
}

export interface ModelOption {
  /** Provider name shown as optgroup label, e.g. "Anthropic". */
  provider: string;
  /** Model id used as value, e.g. "claude-opus-4-6". */
  id: string;
  /** Display name shown to user, e.g. "Claude Opus 4.6". */
  display: string;
}

interface ChatInputProps {
  disabled?: boolean;
  isSubmitting?: boolean;
  onSubmit: (message: string, files: File[]) => void;
  /** When provided + isSubmitting=true, replaces the send button with a Stop button. */
  onStop?: () => void;
  permissionDenied?: string | null;
  /** Repository sources available for per-conversation override. */
  sources?: SourceOption[];
  /** Currently selected source name; "" = use env default. */
  sourceValue?: string;
  /** Setter for source change (per-chat, persists in localStorage upstream). */
  onSourceChange?: (name: string) => void;
  /** Available models grouped by provider. */
  models?: ModelOption[];
  /** Currently selected model id (global setting). */
  modelValue?: string;
  /** Setter for model change. PATCHes /api/model-config/selected upstream. */
  onModelChange?: (modelId: string) => void;
}

export function ChatInput({
  disabled,
  isSubmitting,
  onSubmit,
  onStop,
  permissionDenied,
  sources = [],
  sourceValue = "",
  onSourceChange,
  models = [],
  modelValue = "",
  onModelChange,
}: ChatInputProps) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [message, setMessage] = useState("");
  const [files, setFiles] = useState<File[]>([]);

  const showSource = sources.length > 0 && Boolean(onSourceChange);
  const showModel = models.length > 0 && Boolean(onModelChange);

  // Group models by provider for optgroup rendering.
  const modelsByProvider: Record<string, ModelOption[]> = {};
  for (const m of models) {
    (modelsByProvider[m.provider] = modelsByProvider[m.provider] ?? []).push(m);
  }

  return (
    <form
      className="chat-input-card"
      onSubmit={(event) => {
        event.preventDefault();
        if (!message.trim() || disabled || isSubmitting) {
          return;
        }
        onSubmit(message.trim(), files);
        setMessage("");
        setFiles([]);
      }}
    >
      {permissionDenied ? <div className="permission-note">{permissionDenied}</div> : null}
      {files.length > 0 ? (
        <div className="attachment-row">
          {files.map((file) => (
            <span key={`${file.name}-${file.size}`}>{file.name}</span>
          ))}
        </div>
      ) : null}

      <textarea
        className="chat-input-textarea"
        value={message}
        onChange={(event) => setMessage(event.target.value)}
        onKeyDown={(event) => {
          // Enter submits; Shift+Enter inserts newline (Claude / ChatGPT muscle memory).
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            if (message.trim() && !disabled && !isSubmitting) {
              onSubmit(message.trim(), files);
              setMessage("");
              setFiles([]);
            }
          }
        }}
        placeholder="给 Assistant 发送消息..."
        rows={2}
        disabled={disabled || isSubmitting}
      />

      <div className="chat-input-toolbar">
        <div className="chat-input-toolbar-left">
          <button
            type="button"
            className="icon-button"
            onClick={() => fileInputRef.current?.click()}
            disabled={disabled || isSubmitting}
            aria-label="Attach files"
            title="附加文件 — 上传 .txt/.md/图片等作为 task 上下文"
          >
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M8 12.5 14.8 5.7a3.2 3.2 0 0 1 4.5 4.5l-8 8a5 5 0 0 1-7.1-7.1l8.4-8.4" />
            </svg>
          </button>

          {showSource ? (
            <label
              className="chat-input-pill"
              title="本次会话使用哪个仓库源(env / upload / clone)。空 = 用 backend .env 配置的默认源。每个会话独立选择,localStorage 持久化"
            >
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M3 7v10a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-7L9 5H5a2 2 0 0 0-2 2Z" />
              </svg>
              <select
                value={sourceValue}
                onChange={(e) => onSourceChange?.(e.target.value)}
                disabled={disabled || isSubmitting}
                aria-label="Repository source"
              >
                <option value="">仓库: .env 默认</option>
                {sources.map((s) => (
                  <option key={s.name} value={s.name}>
                    仓库: {s.name} ({s.origin})
                  </option>
                ))}
              </select>
            </label>
          ) : null}

          {showModel ? (
            <label
              className="chat-input-pill"
              title="选择 AI 模型 — 全局设置(影响所有后续 task,包括其他会话)。改这个等同于到 /settings 改"
            >
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <circle cx="12" cy="12" r="3" />
                <path d="M12 4v3M12 17v3M4 12h3M17 12h3M6 6l2 2M16 16l2 2M6 18l2-2M16 8l2-2" />
              </svg>
              <select
                value={modelValue}
                onChange={(e) => onModelChange?.(e.target.value)}
                disabled={disabled || isSubmitting}
                aria-label="AI model"
              >
                {Object.entries(modelsByProvider).map(([provider, list]) => (
                  <optgroup key={provider} label={provider}>
                    {list.map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.display}
                      </option>
                    ))}
                  </optgroup>
                ))}
              </select>
            </label>
          ) : null}
        </div>

        <div className="chat-input-toolbar-right">
          <button
            type="button"
            className="icon-button"
            disabled={disabled || isSubmitting}
            aria-label="Voice input"
            title="语音输入(暂未启用,1.1 上线)"
          >
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M12 4a3 3 0 0 0-3 3v5a3 3 0 0 0 6 0V7a3 3 0 0 0-3-3Z" />
              <path d="M6 11a6 6 0 0 0 12 0M12 17v3m-4 0h8" />
            </svg>
          </button>

          {isSubmitting && onStop ? (
            <button
              type="button"
              className="send-button send-button-stop"
              onClick={(e) => { e.preventDefault(); onStop(); }}
              aria-label="Stop"
              title="停止生成"
            >
              ■
            </button>
          ) : (
            <button
              className="send-button"
              type="submit"
              disabled={disabled || isSubmitting || !message.trim()}
              aria-label="Send"
              title="发送 (Enter) | 换行 (Shift+Enter)"
            >
              {isSubmitting ? "..." : "↑"}
            </button>
          )}
        </div>
      </div>

      <input
        ref={fileInputRef}
        type="file"
        multiple
        hidden
        onChange={(event) => setFiles(Array.from(event.target.files ?? []))}
      />
    </form>
  );
}
