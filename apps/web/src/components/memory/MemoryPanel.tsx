import { useEffect, useMemo, useState } from "react";

import { useAuth } from "../../lib/auth";

interface MemoryItem {
  id: string;
  title: string;
  body: string;
  topic: string;
  updatedAt: string;
}

const STORAGE_KEY = "ops-agent-memory-items";
const SETTINGS_STORAGE_KEY = "ops-agent-memory-settings";

function readMemoryItems(): MemoryItem[] {
  const raw = window.localStorage.getItem(STORAGE_KEY);
  if (!raw) {
    return [
      {
        id: "default-1",
        title: "Preferred planning style",
        body: "Use concise implementation plans with code locations, risk notes, and validation steps.",
        topic: "planning",
        updatedAt: new Date().toISOString(),
      },
    ];
  }
  try {
    return JSON.parse(raw) as MemoryItem[];
  } catch {
    return [];
  }
}

function storeMemoryItems(items: MemoryItem[]) {
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(items));
}

function readMemorySettings() {
  try {
    const raw = window.localStorage.getItem(SETTINGS_STORAGE_KEY);
    if (!raw) {
      throw new Error("No stored settings");
    }
    const parsed = JSON.parse(raw) as { enabled?: boolean; allowList?: string; blockList?: string };
    return {
      enabled: parsed.enabled ?? true,
      allowList: parsed.allowList ?? "planning, coding, debugging",
      blockList: parsed.blockList ?? "passwords, tokens, personal secrets",
    };
  } catch {
    return {
      enabled: true,
      allowList: "planning, coding, debugging",
      blockList: "passwords, tokens, personal secrets",
    };
  }
}

export function MemoryPanel() {
  const { can } = useAuth();
  const initialSettings = useMemo(() => readMemorySettings(), []);
  const [enabled, setEnabled] = useState(initialSettings.enabled);
  const [items, setItems] = useState<MemoryItem[]>(() => readMemoryItems());
  const [search, setSearch] = useState("");
  const [draft, setDraft] = useState({ title: "", body: "", topic: "" });
  const [editId, setEditId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState({ title: "", body: "", topic: "" });
  const [allowList, setAllowList] = useState(initialSettings.allowList);
  const [blockList, setBlockList] = useState(initialSettings.blockList);

  useEffect(() => {
    window.localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify({ enabled, allowList, blockList }));
  }, [enabled, allowList, blockList]);

  const filtered = useMemo(() => {
    const normalized = search.trim().toLowerCase();
    if (!normalized) {
      return items;
    }
    return items.filter((item) => [item.title, item.body, item.topic].join(" ").toLowerCase().includes(normalized));
  }, [items, search]);
  const allowCount = allowList.split(",").map((item) => item.trim()).filter(Boolean).length;
  const blockCount = blockList.split(",").map((item) => item.trim()).filter(Boolean).length;

  function saveItems(nextItems: MemoryItem[]) {
    setItems(nextItems);
    storeMemoryItems(nextItems);
  }

  function addMemory() {
    if (!can("memory:edit") || !draft.title.trim() || !draft.body.trim()) {
      return;
    }
    saveItems([
      {
        id: crypto.randomUUID(),
        title: draft.title.trim(),
        body: draft.body.trim(),
        topic: draft.topic.trim() || "general",
        updatedAt: new Date().toISOString(),
      },
      ...items,
    ]);
    setDraft({ title: "", body: "", topic: "" });
  }

  function startEditing(item: MemoryItem) {
    if (!can("memory:edit")) {
      return;
    }
    setEditId(item.id);
    setEditDraft({ title: item.title, body: item.body, topic: item.topic });
  }

  function saveEdit() {
    if (!can("memory:edit") || !editId || !editDraft.title.trim() || !editDraft.body.trim()) {
      return;
    }
    saveItems(
      items.map((item) =>
        item.id === editId
          ? {
              ...item,
              title: editDraft.title.trim(),
              body: editDraft.body.trim(),
              topic: editDraft.topic.trim() || "general",
              updatedAt: new Date().toISOString(),
            }
          : item,
      ),
    );
    setEditId(null);
    setEditDraft({ title: "", body: "", topic: "" });
  }

  return (
    <div className="memory-panel">
      <section className="memory-stats">
        <article className="memory-stat-card">
          <span>Automatic capture</span>
          <strong>{enabled ? "On" : "Off"}</strong>
        </article>
        <article className="memory-stat-card">
          <span>Allowed topics</span>
          <strong>{allowCount}</strong>
        </article>
        <article className="memory-stat-card">
          <span>Blocked topics</span>
          <strong>{blockCount}</strong>
        </article>
      </section>

      <section className="simple-card" id="memory-controls">
        <div className="section-heading">
          <div>
            <span>Memory</span>
            <h2>Automatic memory</h2>
          </div>
          <label className="switch-row">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(event) => setEnabled(event.target.checked)}
              disabled={!can("memory:edit")}
            />
            <span>{enabled ? "On" : "Off"}</span>
          </label>
        </div>
        <p className="muted-copy">
          Keep lightweight preferences and working context. Sensitive topics should stay blocked.
        </p>
        {!can("memory:edit") ? <div className="permission-note">Your role can view memory but cannot edit it.</div> : null}
      </section>

      <section className="simple-card">
        <div className="section-heading">
          <div>
            <span>Controls</span>
            <h2>Allowed and blocked topics</h2>
          </div>
        </div>
        <div className="two-column-fields">
          <label className="field">
            <span>Whitelist topics</span>
            <input value={allowList} onChange={(event) => setAllowList(event.target.value)} disabled={!can("memory:edit")} />
          </label>
          <label className="field">
            <span>Blacklist topics</span>
            <input value={blockList} onChange={(event) => setBlockList(event.target.value)} disabled={!can("memory:edit")} />
          </label>
        </div>
      </section>

      <section className="simple-card">
        <div className="section-heading">
          <div>
            <span>Entries</span>
            <h2>Saved memory</h2>
          </div>
          <input
            className="compact-input"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Search memory"
          />
        </div>

        <div className="memory-editor" id="memory-editor">
          <input
            value={draft.title}
            onChange={(event) => setDraft((current) => ({ ...current, title: event.target.value }))}
            placeholder="Short memory title"
            disabled={!can("memory:edit")}
          />
          <input
            value={draft.topic}
            onChange={(event) => setDraft((current) => ({ ...current, topic: event.target.value }))}
            placeholder="Topic"
            disabled={!can("memory:edit")}
          />
          <textarea
            value={draft.body}
            onChange={(event) => setDraft((current) => ({ ...current, body: event.target.value }))}
            placeholder="What should the assistant remember?"
            disabled={!can("memory:edit")}
          />
          <button type="button" onClick={addMemory} disabled={!can("memory:edit")}>
            Add memory
          </button>
        </div>

        <div className="memory-list">
          {filtered.map((item) => (
            <article key={item.id} className="memory-item">
              {editId === item.id ? (
                <div className="memory-editor">
                  <input
                    value={editDraft.title}
                    onChange={(event) => setEditDraft((current) => ({ ...current, title: event.target.value }))}
                    disabled={!can("memory:edit")}
                  />
                  <input
                    value={editDraft.topic}
                    onChange={(event) => setEditDraft((current) => ({ ...current, topic: event.target.value }))}
                    disabled={!can("memory:edit")}
                  />
                  <textarea
                    value={editDraft.body}
                    onChange={(event) => setEditDraft((current) => ({ ...current, body: event.target.value }))}
                    disabled={!can("memory:edit")}
                  />
                  <div className="memory-actions">
                    <button type="button" onClick={saveEdit} disabled={!can("memory:edit")}>
                      Save
                    </button>
                    <button type="button" onClick={() => setEditId(null)}>
                      Cancel
                    </button>
                  </div>
                </div>
              ) : (
                <>
                  <div>
                    <strong>{item.title}</strong>
                    <span>{item.topic}</span>
                  </div>
                  <p>{item.body}</p>
                  <div className="memory-actions">
                    <button type="button" onClick={() => startEditing(item)} disabled={!can("memory:edit")}>
                      Edit
                    </button>
                    <button
                      type="button"
                      onClick={() => saveItems(items.filter((candidate) => candidate.id !== item.id))}
                      disabled={!can("memory:edit")}
                    >
                      Delete
                    </button>
                  </div>
                </>
              )}
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}
