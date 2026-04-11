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
          +
        </button>
        <textarea
          value={message}
          onChange={(event) => setMessage(event.target.value)}
          placeholder="Message Assistant..."
          rows={1}
          disabled={disabled || isSubmitting}
        />
        <button className="send-button" type="submit" disabled={disabled || isSubmitting || !message.trim()}>
          {isSubmitting ? "..." : "Send"}
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
