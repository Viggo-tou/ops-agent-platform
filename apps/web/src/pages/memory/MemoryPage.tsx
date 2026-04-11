import { MemoryPanel } from "../../components/memory/MemoryPanel";

export function MemoryPage() {
  return (
    <div className="content-page reference-page memory-page-shell">
      <header className="content-header split-header">
        <div>
          <span>Memory</span>
          <h1>Memory management</h1>
          <p>Control automatic memory and decide which topics can be remembered.</p>
        </div>
        <div className="page-actions">
          <button type="button" className="subtle-button" onClick={() => document.getElementById("memory-controls")?.scrollIntoView({ behavior: "smooth" })}>
            Memory settings
          </button>
          <button type="button" className="primary-action" onClick={() => document.getElementById("memory-editor")?.scrollIntoView({ behavior: "smooth" })}>
            Add memory
          </button>
        </div>
      </header>
      <MemoryPanel />
    </div>
  );
}
