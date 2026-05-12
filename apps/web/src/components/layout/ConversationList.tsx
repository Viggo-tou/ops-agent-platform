import { Link, useParams } from "react-router-dom";
import { useMemo, useState } from "react";

import type { TaskSummary } from "../../types";

interface ConversationListProps {
  conversations: TaskSummary[];
  search: string;
}

interface ConversationGroup {
  key: string;
  first: TaskSummary;
  latest: TaskSummary;
  turns: number;
  searchText: string;
}

const TITLE_STORAGE_KEY = "ops-agent-conversation-titles";

function readTitleOverrides(): Record<string, string> {
  try {
    const raw = window.localStorage.getItem(TITLE_STORAGE_KEY);
    return raw ? (JSON.parse(raw) as Record<string, string>) : {};
  } catch {
    return {};
  }
}

export function ConversationList({ conversations, search }: ConversationListProps) {
  const { taskId } = useParams();
  const [titleOverrides, setTitleOverrides] = useState<Record<string, string>>(() => readTitleOverrides());
  const normalizedSearch = search.trim().toLowerCase();
  const activeConversation = conversations.find((conversation) => conversation.id === taskId);
  const activeSessionKey = activeConversation?.session_id ?? taskId ?? null;

  const groupedConversations = useMemo<ConversationGroup[]>(() => {
    const groups = new Map<string, TaskSummary[]>();

    for (const conversation of conversations) {
      const key = conversation.session_id ?? conversation.id;
      groups.set(key, [...(groups.get(key) ?? []), conversation]);
    }

    return Array.from(groups.entries())
      .map(([key, groupItems]) => {
        const chronological = [...groupItems].sort(
          (left, right) => new Date(left.created_at).getTime() - new Date(right.created_at).getTime(),
        );
        const latestFirst = [...groupItems].sort(
          (left, right) => new Date(right.updated_at).getTime() - new Date(left.updated_at).getTime(),
        );
        return {
          key,
          first: chronological[0],
          latest: latestFirst[0],
          turns: groupItems.length,
          searchText: groupItems
            .map((item) => [item.title, item.scenario, item.status, item.plan_provider_name ?? "", item.session_id ?? ""].join(" "))
            .join(" ")
            .toLowerCase(),
        };
      })
      .sort((left, right) => new Date(right.latest.updated_at).getTime() - new Date(left.latest.updated_at).getTime());
  }, [conversations]);

  const filtered = useMemo(
    () =>
      groupedConversations.filter((conversation) => {
        const title = titleOverrides[conversation.key] ?? conversation.first.title;
        if (!normalizedSearch) {
          return true;
        }
        return `${title} ${conversation.searchText}`.toLowerCase().includes(normalizedSearch);
      }),
    [groupedConversations, normalizedSearch, titleOverrides],
  );

  function renameConversation(conversation: ConversationGroup) {
    const currentTitle = titleOverrides[conversation.key] ?? conversation.first.title;
    const nextTitle = window.prompt("Rename conversation", currentTitle)?.trim();

    if (!nextTitle || nextTitle === currentTitle) {
      return;
    }

    const nextOverrides = {
      ...titleOverrides,
      [conversation.key]: nextTitle,
    };
    setTitleOverrides(nextOverrides);
    window.localStorage.setItem(TITLE_STORAGE_KEY, JSON.stringify(nextOverrides));
  }

  if (filtered.length === 0) {
    return <div className="sidebar-empty">未找到对话。</div>;
  }

  function formatConversationDate(value: string) {
    const date = new Date(value);
    return `${date.getFullYear()}/${date.getMonth() + 1}/${date.getDate()}`;
  }

  return (
    <div className="conversation-list">
      {filtered.map((conversation) => {
        const title = titleOverrides[conversation.key] ?? conversation.first.title;
        const isFailed = conversation.latest.status === "failed";
        const itemClass = [
          "conversation-item",
          conversation.key === activeSessionKey ? "active" : "",
          isFailed ? "failed" : "",
        ]
          .filter(Boolean)
          .join(" ");
        return (
          <article key={conversation.key} className={itemClass}>
            <div className="conversation-item-title">
              <Link to={`/chat/${conversation.latest.id}`}>
                {isFailed ? <span className="conversation-status-dot" aria-hidden="true" /> : null}
                <span>{title}</span>
              </Link>
              <button
                type="button"
                className="conversation-rename"
                onClick={() => renameConversation(conversation)}
                aria-label={`Rename ${title}`}
              >
                Rename
              </button>
            </div>
            <small>
              {formatConversationDate(conversation.latest.updated_at)} · {conversation.turns} 条消息
              {isFailed ? (
                <Link
                  to={`/chat/${conversation.latest.id}?continue=1`}
                  className="conversation-continue-link"
                  title="跳转聊天并预激活继续修复模式"
                >
                  继续修复 →
                </Link>
              ) : null}
            </small>
          </article>
        );
      })}
    </div>
  );
}
