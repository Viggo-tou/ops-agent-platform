import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type CSSProperties, useEffect, useState } from "react";

import { api } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import type { MemoryItem, MemoryItemUpdate } from "../../types";

export function MemoryPanel() {
  const { can } = useAuth();
  const queryClient = useQueryClient();
  const canEditMemory = can("memory:edit");
  const [enabled, setEnabled] = useState(false);
  const [search, setSearch] = useState("");
  const [draft, setDraft] = useState({ title: "", body: "", topic: "" });
  const [editId, setEditId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState({ title: "", body: "", topic: "" });
  const [allowList, setAllowList] = useState("");
  const [blockList, setBlockList] = useState("");
  const [settingsLoaded, setSettingsLoaded] = useState(false);

  const itemsQuery = useQuery({
    queryKey: ["memory-items", search],
    queryFn: () => api.listMemoryItems(search || undefined),
    placeholderData: (previousItems) => previousItems,
  });

  const settingsQuery = useQuery({
    queryKey: ["memory-settings"],
    queryFn: () => api.getMemorySettings(),
  });

  async function invalidateMemoryItems() {
    await queryClient.invalidateQueries({ queryKey: ["memory-items"] });
  }

  const createItemMutation = useMutation({
    mutationFn: api.createMemoryItem,
    onSuccess: () => {
      setDraft({ title: "", body: "", topic: "" });
      void invalidateMemoryItems();
    },
  });

  const updateItemMutation = useMutation({
    mutationFn: ({ itemId, payload }: { itemId: string; payload: MemoryItemUpdate }) =>
      api.updateMemoryItem(itemId, payload),
    onSuccess: () => {
      setEditId(null);
      setEditDraft({ title: "", body: "", topic: "" });
      void invalidateMemoryItems();
    },
  });

  const deleteItemMutation = useMutation({
    mutationFn: (itemId: string) => api.deleteMemoryItem(itemId),
    onSuccess: () => {
      void invalidateMemoryItems();
    },
  });

  const updateSettingsMutation = useMutation({
    mutationFn: api.updateMemorySettings,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["memory-settings"] });
    },
  });

  useEffect(() => {
    if (!settingsQuery.data || settingsLoaded) {
      return;
    }
    setEnabled(settingsQuery.data.enabled);
    setAllowList(settingsQuery.data.allow_list);
    setBlockList(settingsQuery.data.block_list);
    setSettingsLoaded(true);
  }, [settingsLoaded, settingsQuery.data]);

  const filtered = itemsQuery.data ?? [];
  const allowCount = allowList.split(",").map((item) => item.trim()).filter(Boolean).length;
  const blockCount = blockList.split(",").map((item) => item.trim()).filter(Boolean).length;

  function persistSettings(nextSettings = { enabled, allowList, blockList }) {
    if (!canEditMemory) {
      return;
    }
    updateSettingsMutation.mutate({
      enabled: nextSettings.enabled,
      allow_list: nextSettings.allowList,
      block_list: nextSettings.blockList,
    });
  }

  function handleEnabledChange(checked: boolean) {
    setEnabled(checked);
    persistSettings({ enabled: checked, allowList, blockList });
  }

  function addMemory() {
    if (!canEditMemory || !draft.title.trim() || !draft.body.trim()) {
      return;
    }
    createItemMutation.mutate({
      title: draft.title.trim(),
      body: draft.body.trim(),
      topic: draft.topic.trim() || "general",
    });
  }

  function startEditing(item: MemoryItem) {
    if (!canEditMemory) {
      return;
    }
    setEditId(item.id);
    setEditDraft({ title: item.title, body: item.body, topic: item.topic });
  }

  function saveEdit() {
    if (!canEditMemory || !editId || !editDraft.title.trim() || !editDraft.body.trim()) {
      return;
    }
    updateItemMutation.mutate({
      itemId: editId,
      payload: {
        title: editDraft.title.trim(),
        body: editDraft.body.trim(),
        topic: editDraft.topic.trim() || "general",
      },
    });
  }

  function deleteMemory(itemId: string) {
    if (!canEditMemory) {
      return;
    }
    deleteItemMutation.mutate(itemId);
  }

  return (
    <div className="memory-panel">
      <section className="memory-stats">
        <article className="memory-stat-card" style={{ "--icon-bg": "#f3e8ff" } as CSSProperties}>
          <div className="memory-stat-icon purple">
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M9 4a3 3 0 0 0-3 3 3 3 0 0 0-2 5.2A3.5 3.5 0 0 0 8 18h1V4Z" />
              <path d="M15 4a3 3 0 0 1 3 3 3 3 0 0 1 2 5.2A3.5 3.5 0 0 1 16 18h-1V4Z" />
            </svg>
          </div>
          <span>自动提取</span>
          <strong>{enabled ? "开启" : "关闭"}</strong>
        </article>
        <article className="memory-stat-card" style={{ "--icon-bg": "#dcfce7" } as CSSProperties}>
          <div className="memory-stat-icon green">
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z" />
              <path d="m8.5 12 2.2 2.2 4.8-5" />
            </svg>
          </div>
          <span>白名单主题</span>
          <strong>{allowCount}</strong>
        </article>
        <article className="memory-stat-card" style={{ "--icon-bg": "#fee2e2" } as CSSProperties}>
          <div className="memory-stat-icon red">
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M4 5h16l-6 7v5l-4 2v-7L4 5Z" />
            </svg>
          </div>
          <span>黑名单主题</span>
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
              onChange={(event) => handleEnabledChange(event.target.checked)}
              disabled={!canEditMemory || updateSettingsMutation.isPending}
            />
            <span>{enabled ? "开启" : "关闭"}</span>
          </label>
        </div>
        <p className="muted-copy">
          Keep lightweight preferences and working context. Sensitive topics should stay blocked.
        </p>
        {!canEditMemory ? <div className="permission-note">Your role can view memory but cannot edit it.</div> : null}
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
            <span>白名单主题</span>
            <input
              value={allowList}
              onChange={(event) => setAllowList(event.target.value)}
              onBlur={() => persistSettings()}
              disabled={!canEditMemory || updateSettingsMutation.isPending}
            />
          </label>
          <label className="field">
            <span>黑名单主题</span>
            <input
              value={blockList}
              onChange={(event) => setBlockList(event.target.value)}
              onBlur={() => persistSettings()}
              disabled={!canEditMemory || updateSettingsMutation.isPending}
            />
          </label>
        </div>
      </section>

      <section className="simple-card">
        <div className="memory-search-row">
          <label className="memory-search-field">
            <span>🔍</span>
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="搜索记忆..."
            />
          </label>
          <button type="button" onClick={() => void itemsQuery.refetch()}>搜索</button>
        </div>

        <div className="memory-editor" id="memory-editor">
          <input
            value={draft.title}
            onChange={(event) => setDraft((current) => ({ ...current, title: event.target.value }))}
            placeholder="Short memory title"
            disabled={!canEditMemory || createItemMutation.isPending}
          />
          <input
            value={draft.topic}
            onChange={(event) => setDraft((current) => ({ ...current, topic: event.target.value }))}
            placeholder="Topic"
            disabled={!canEditMemory || createItemMutation.isPending}
          />
          <textarea
            value={draft.body}
            onChange={(event) => setDraft((current) => ({ ...current, body: event.target.value }))}
            placeholder="What should the assistant remember?"
            disabled={!canEditMemory || createItemMutation.isPending}
          />
          <button type="button" onClick={addMemory} disabled={!canEditMemory || createItemMutation.isPending}>
            + 添加记忆
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
                    disabled={!canEditMemory || updateItemMutation.isPending}
                  />
                  <input
                    value={editDraft.topic}
                    onChange={(event) => setEditDraft((current) => ({ ...current, topic: event.target.value }))}
                    disabled={!canEditMemory || updateItemMutation.isPending}
                  />
                  <textarea
                    value={editDraft.body}
                    onChange={(event) => setEditDraft((current) => ({ ...current, body: event.target.value }))}
                    disabled={!canEditMemory || updateItemMutation.isPending}
                  />
                  <div className="memory-actions">
                    <button type="button" onClick={saveEdit} disabled={!canEditMemory || updateItemMutation.isPending}>
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
                    <button type="button" onClick={() => startEditing(item)} disabled={!canEditMemory}>
                      Edit
                    </button>
                    <button
                      type="button"
                      onClick={() => deleteMemory(item.id)}
                      disabled={!canEditMemory || deleteItemMutation.isPending}
                    >
                      Delete
                    </button>
                  </div>
                </>
              )}
            </article>
          ))}
          {filtered.length === 0 ? (
            <div className="memory-empty-state">
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M9 4a3 3 0 0 0-3 3 3 3 0 0 0-2 5.2A3.5 3.5 0 0 0 8 18h1V4Z" />
                <path d="M15 4a3 3 0 0 1 3 3 3 3 0 0 1 2 5.2A3.5 3.5 0 0 1 16 18h-1V4Z" />
              </svg>
              <h2>暂无记忆</h2>
              <p>开启记忆功能后，AI 会自动从对话中提取重要信息</p>
            </div>
          ) : null}
        </div>
      </section>
    </div>
  );
}
