import { useRef, useState } from "react";

interface ChatInputProps {
  disabled?: boolean;
  isSubmitting?: boolean;
  onSubmit: (message: string, files: File[]) => void;
  permissionDenied?: string | null;
}

export function ChatInput({ disabled, isSubmitting, onSubmit, permissionDenied }: ChatInputProps) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [message, setMessage] = useState("");
  const [files, setFiles] = useState<File[]>([]);

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
      <div className="chat-input-row">
        <button
          type="button"
          className="icon-button"
          onClick={() => fileInputRef.current?.click()}
          disabled={disabled || isSubmitting}
          aria-label="Attach files"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M8 12.5 14.8 5.7a3.2 3.2 0 0 1 4.5 4.5l-8 8a5 5 0 0 1-7.1-7.1l8.4-8.4" />
          </svg>
        </button>
        <button
          type="button"
          className="icon-button"
          disabled={disabled || isSubmitting}
          aria-label="Voice input"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M12 4a3 3 0 0 0-3 3v5a3 3 0 0 0 6 0V7a3 3 0 0 0-3-3Z" />
            <path d="M6 11a6 6 0 0 0 12 0M12 17v3m-4 0h8" />
          </svg>
        </button>
        <textarea
          value={message}
          onChange={(event) => setMessage(event.target.value)}
          placeholder="给 Assistant 发送消息..."
          rows={1}
          disabled={disabled || isSubmitting}
        />
        <button className="send-button" type="submit" disabled={disabled || isSubmitting || !message.trim()}>
          {isSubmitting ? "..." : "↑"}
        </button>
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
