import { MemoryPanel } from "../../components/memory/MemoryPanel";

export function MemoryPage() {
  return (
    <div className="content-page reference-page memory-page-shell">
      <header className="content-header split-header">
        <div>
          <h1>记忆管理</h1>
          <p>管理自动提取的记忆，控制哪些内容会被记住</p>
        </div>
        <div className="page-actions">
          <button type="button" className="subtle-button" onClick={() => document.getElementById("memory-controls")?.scrollIntoView({ behavior: "smooth" })}>
            ⚙️ 提取设置
          </button>
          <button type="button" className="primary-action" onClick={() => document.getElementById("memory-editor")?.scrollIntoView({ behavior: "smooth" })}>
            + 添加记忆
          </button>
        </div>
      </header>
      <MemoryPanel />
    </div>
  );
}
