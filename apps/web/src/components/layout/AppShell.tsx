import { useQuery } from "@tanstack/react-query";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useState } from "react";

import { useAuth } from "../../lib/auth";
import { api } from "../../lib/api";
import { toErrorMessage } from "../../lib/format";
import { ConversationList } from "./ConversationList";

const navigationItems = [
  { to: "/home", label: "Home" },
  { to: "/knowledge", label: "Knowledge" },
  { to: "/memory", label: "Memory" },
  { to: "/settings", label: "Settings", permission: "settings:view" as const },
];

export function AppShell() {
  const navigate = useNavigate();
  const { user, logout, can } = useAuth();
  const [conversationSearch, setConversationSearch] = useState("");

  const tasksQuery = useQuery({
    queryKey: ["tasks", "sidebar"],
    queryFn: () => api.listTasks(),
    refetchInterval: 10_000,
  });

  return (
    <div className="workbench-shell">
      <aside className="workbench-sidebar">
        <div className="sidebar-top">
          <button
            className="new-chat-button"
            type="button"
            onClick={() => {
              void navigate("/chat");
            }}
          >
            Start chat
          </button>

          <label className="sidebar-search">
            <span>Search conversations</span>
            <input
              value={conversationSearch}
              onChange={(event) => setConversationSearch(event.target.value)}
              placeholder="Search"
            />
          </label>

          <nav className="workbench-nav" aria-label="Workspace navigation">
            {navigationItems.map((item) => {
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

          <div className="sidebar-toggle-group" aria-label="Workspace feature switches">
            <div className="sidebar-toggle-row">
              <span>Knowledge RAG</span>
              <strong>On</strong>
            </div>
            <div className="sidebar-toggle-row">
              <span>Long memory</span>
              <strong>On</strong>
            </div>
          </div>
        </div>

        <div className="sidebar-section">
          <div className="sidebar-section-title">Recent conversations</div>
          {tasksQuery.isError ? <div className="sidebar-empty">{toErrorMessage(tasksQuery.error)}</div> : null}
          {tasksQuery.isLoading ? <div className="sidebar-empty">Loading conversations...</div> : null}
          {tasksQuery.data ? (
            <ConversationList conversations={tasksQuery.data.slice(0, 40)} search={conversationSearch} />
          ) : null}
        </div>

        <div className="account-card">
          <div className="avatar">{user?.name.slice(0, 1).toUpperCase()}</div>
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
        </div>
      </aside>

      <main className="workbench-main">
        <Outlet />
      </main>
    </div>
  );
}
