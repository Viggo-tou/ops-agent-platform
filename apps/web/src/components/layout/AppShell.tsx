import { useQuery } from "@tanstack/react-query";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useState } from "react";

import { useAuth } from "../../lib/auth";
import { api } from "../../lib/api";
import { toErrorMessage } from "../../lib/format";
import { ConversationList } from "./ConversationList";

const navigationItems = [
  { to: "/home", label: "首页" },
  { to: "/knowledge", label: "知识库" },
  { to: "/memory", label: "记忆" },
  { to: "/repositories", label: "仓库", permission: "settings:view" as const },
  { to: "/integrations", label: "集成", permission: "settings:view" as const },
  { to: "/governance", label: "治理", permission: "settings:view" as const },
  { to: "/settings", label: "设置", permission: "settings:view" as const },
];

export function AppShell() {
  const navigate = useNavigate();
  const location = useLocation();
  const { user, logout, can } = useAuth();
  const [conversationSearch, setConversationSearch] = useState("");

  const tasksQuery = useQuery({
    queryKey: ["tasks", "sidebar"],
    queryFn: () => api.listTasks(),
    refetchInterval: 10_000,
  });
  const pathname = location.pathname;
  const sidebarVariant = pathname.startsWith("/home")
    ? "minimal"
    : pathname.startsWith("/knowledge") || pathname.startsWith("/memory") || pathname.startsWith("/settings") || pathname.startsWith("/governance") || pathname.startsWith("/repositories") || pathname.startsWith("/integrations")
      ? "compact"
      : "chat";
  const visibleNavigationItems =
    sidebarVariant === "compact"
      ? navigationItems.filter((item) => item.to === "/home" || item.to === pathname)
      : navigationItems;
  const recentConversationCount = tasksQuery.data?.length ?? 0;

  return (
    <div className="workbench-shell">
      <aside className={`workbench-sidebar sidebar-variant--${sidebarVariant}`}>
        <div className="sidebar-top">
          {sidebarVariant === "compact" ? (
            <button className="back-to-chat" type="button" onClick={() => void navigate("/chat")}>
              ← 返回聊天
            </button>
          ) : (
            <button
              className="new-chat-button"
              type="button"
              onClick={() => {
                void navigate("/chat");
              }}
            >
              {sidebarVariant === "chat" ? "新建聊天" : "开始聊天"}
            </button>
          )}

          {sidebarVariant === "chat" ? (
            <label className="sidebar-search">
              <span>搜索对话</span>
              <input
                value={conversationSearch}
                onChange={(event) => setConversationSearch(event.target.value)}
                placeholder="搜索对话..."
              />
            </label>
          ) : null}

          <nav className="workbench-nav" aria-label="Workspace navigation">
            {visibleNavigationItems.map((item) => {
              if (item.permission && !can(item.permission)) {
                return null;
              }
              return (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}
                >
                  {item.label}
                </NavLink>
              );
            })}
          </nav>

          {/* Removed hardcoded "知识库 RAG / 长期记忆 = On" labels.
              They were cosmetic — backend has no per-conversation toggle.
              Real per-conversation overrides ship in 1.1 (backend support
              needed first). When they ship, add toggle icons into ChatInput
              toolbar alongside the source picker. */}
        </div>

        {sidebarVariant === "chat" ? (
          <div className="sidebar-section">
            <div className="sidebar-section-title">最近对话 ({recentConversationCount})</div>
            {tasksQuery.isError ? <div className="sidebar-empty">{toErrorMessage(tasksQuery.error)}</div> : null}
            {tasksQuery.isLoading ? <div className="sidebar-empty">加载对话中...</div> : null}
            {tasksQuery.data ? (
              <ConversationList conversations={tasksQuery.data.slice(0, 40)} search={conversationSearch} />
            ) : null}
          </div>
        ) : (
          <div />
        )}

        <div className="account-card">
          <div className="avatar">{user?.name.slice(0, 1).toUpperCase()}</div>
          {sidebarVariant === "chat" ? (
            <>
              <div>
                <strong>{user?.name}</strong>
                <span>{user?.role}</span>
              </div>
              <button
                type="button"
                className="text-button"
                onClick={() => {
                  logout();
                  void navigate("/login");
                }}
              >
                Log out
              </button>
            </>
          ) : null}
        </div>
      </aside>

      <main className="workbench-main">
        <Outlet />
      </main>
    </div>
  );
}
