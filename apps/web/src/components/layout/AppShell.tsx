import { useQuery } from "@tanstack/react-query";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useEffect, useRef, useState } from "react";

import { useAuth } from "../../lib/auth";
import { api } from "../../lib/api";
import { toErrorMessage } from "../../lib/format";
import { ConversationList } from "./ConversationList";

type NavItem = {
  to: string;
  label: string;
  iconKind: string;
  permission?: "settings:view";
};

const navigationItems: NavItem[] = [
  { to: "/dashboard", label: "总览", iconKind: "home" },
  { to: "/tasks", label: "任务列表", iconKind: "list" },
  { to: "/knowledge", label: "知识库", iconKind: "book" },
  { to: "/memory", label: "记忆", iconKind: "brain" },
  { to: "/repositories", label: "仓库", iconKind: "folder", permission: "settings:view" },
  { to: "/integrations", label: "集成", iconKind: "wrench", permission: "settings:view" },
  { to: "/usage", label: "用量", iconKind: "chart", permission: "settings:view" },
  { to: "/governance", label: "治理", iconKind: "shield", permission: "settings:view" },
  { to: "/settings", label: "设置", iconKind: "gear", permission: "settings:view" },
];

function NavIcon({ kind }: { kind: string }) {
  const props = { viewBox: "0 0 24 24", "aria-hidden": true } as const;
  switch (kind) {
    case "home":
      return <svg {...props}><path d="m3 12 9-9 9 9M5 10v10h14V10" /></svg>;
    case "list":
      return <svg {...props}><rect x="3" y="4" width="18" height="4" rx="1" /><rect x="3" y="10" width="18" height="4" rx="1" /><rect x="3" y="16" width="18" height="4" rx="1" /></svg>;
    case "book":
      return <svg {...props}><path d="M4 4.5A1.5 1.5 0 0 1 5.5 3H10c1.1 0 2 .9 2 2v15c0-1.1-.9-2-2-2H5.5A1.5 1.5 0 0 1 4 16.5v-12Z" /><path d="M20 4.5A1.5 1.5 0 0 0 18.5 3H14c-1.1 0-2 .9-2 2v15c0-1.1.9-2 2-2h4.5a1.5 1.5 0 0 0 1.5-1.5v-12Z" /></svg>;
    case "brain":
      return <svg {...props}><path d="M8 2a3.5 3.5 0 0 0-3 5.3A3.5 3.5 0 0 0 6 14a3 3 0 0 0 5 2v3a3 3 0 0 0 3-3v-3a3 3 0 0 0 5-2 3.5 3.5 0 0 0 1-6.7A3.5 3.5 0 0 0 16 2a3 3 0 0 0-4 1.4A3 3 0 0 0 8 2Z" /></svg>;
    case "folder":
      return <svg {...props}><path d="M3 7.5A2.5 2.5 0 0 1 5.5 5h3.6c.55 0 1.07.22 1.45.6l1.4 1.4h7.55A2.5 2.5 0 0 1 22 9.5v8A2.5 2.5 0 0 1 19.5 20h-15A2.5 2.5 0 0 1 2 17.5v-10Z" /></svg>;
    case "wrench":
      return <svg {...props}><path d="M14.7 6.3a4 4 0 0 0-5.4 5.4l-6 6a2 2 0 0 0 2.8 2.8l6-6a4 4 0 0 0 5.4-5.4l-2.4 2.4-2-2 2.4-2.4Z" /></svg>;
    case "chart":
      return <svg {...props}><path d="M3 3v18h18M7 14l4-4 4 4 5-7" /></svg>;
    case "shield":
      return <svg {...props}><path d="M12 3 4 6v6c0 5 3.5 9 8 9s8-4 8-9V6Z" /><path d="m9 12 2 2 4-4" /></svg>;
    case "gear":
      return <svg {...props}><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .35 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.35 1.7 1.7 0 0 0-1.04 1.55V21a2 2 0 0 1-4 0v-.1a1.7 1.7 0 0 0-1.04-1.55 1.7 1.7 0 0 0-1.87.35l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .35-1.87 1.7 1.7 0 0 0-1.55-1.04H3a2 2 0 0 1 0-4h.1a1.7 1.7 0 0 0 1.55-1.04 1.7 1.7 0 0 0-.35-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.87.35h.04a1.7 1.7 0 0 0 1.04-1.55V3a2 2 0 0 1 4 0v.1a1.7 1.7 0 0 0 1.04 1.55 1.7 1.7 0 0 0 1.87-.35l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.35 1.87v.04a1.7 1.7 0 0 0 1.55 1.04H21a2 2 0 0 1 0 4h-.1a1.7 1.7 0 0 0-1.55 1.04Z" /></svg>;
    case "logout":
      return <svg {...props}><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9" /></svg>;
    case "chev-up":
      return <svg {...props}><path d="m6 15 6-6 6 6" /></svg>;
    default:
      return null;
  }
}

export function AppShell() {
  const navigate = useNavigate();
  const location = useLocation();
  const { user, logout, can } = useAuth();
  const [conversationSearch, setConversationSearch] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const accountRef = useRef<HTMLDivElement | null>(null);

  const tasksQuery = useQuery({
    queryKey: ["tasks", "sidebar"],
    queryFn: () => api.listTasks(),
    refetchInterval: 10_000,
  });

  const pathname = location.pathname;
  const recentConversationCount = tasksQuery.data?.length ?? 0;

  // Close menu on route change.
  useEffect(() => {
    setMenuOpen(false);
  }, [pathname]);

  // Close menu on outside click / Escape.
  useEffect(() => {
    if (!menuOpen) return;
    function onDoc(e: MouseEvent) {
      const t = e.target as Node;
      if (menuRef.current?.contains(t)) return;
      if (accountRef.current?.contains(t)) return;
      setMenuOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setMenuOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  const visibleNav = navigationItems.filter(
    (item) => !item.permission || can(item.permission),
  );

  return (
    <div className="workbench-shell">
      <aside className="workbench-sidebar sidebar-variant--unified">
        <div className="sidebar-top">
          <button
            className="new-chat-button"
            type="button"
            onClick={() => void navigate("/chat")}
          >
            新建聊天
          </button>

          <label className="sidebar-search">
            <span>搜索对话</span>
            <input
              value={conversationSearch}
              onChange={(event) => setConversationSearch(event.target.value)}
              placeholder="搜索对话..."
            />
          </label>
        </div>

        <div className="sidebar-section">
          <div className="sidebar-section-title">最近对话 ({recentConversationCount})</div>
          {tasksQuery.isError ? (
            <div className="sidebar-empty">{toErrorMessage(tasksQuery.error)}</div>
          ) : null}
          {tasksQuery.isLoading ? (
            <div className="sidebar-empty">加载对话中...</div>
          ) : null}
          {tasksQuery.data ? (
            <ConversationList
              conversations={tasksQuery.data.slice(0, 40)}
              search={conversationSearch}
            />
          ) : null}
        </div>

        <div className="account-card account-card-v2" ref={accountRef}>
          <button
            className="account-card-trigger"
            type="button"
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((v) => !v)}
            title="打开菜单"
          >
            <div className="avatar">{user?.name.slice(0, 1).toUpperCase()}</div>
            <div className="account-card-name">
              <strong>{user?.name}</strong>
              <span>{user?.role}</span>
            </div>
          </button>
          <button
            className="account-gear-btn"
            type="button"
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((v) => !v)}
            title="工作区菜单"
          >
            <NavIcon kind="gear" />
          </button>

          {menuOpen ? (
            <div className="account-menu" ref={menuRef} role="menu">
              <div className="account-menu-section-title">工作区</div>
              <ul className="account-menu-list">
                {visibleNav.map((item) => (
                  <li key={item.to}>
                    <NavLink
                      to={item.to}
                      className={({ isActive }) =>
                        `account-menu-item${isActive ? " active" : ""}`
                      }
                      onClick={() => setMenuOpen(false)}
                    >
                      <span className="account-menu-icon">
                        <NavIcon kind={item.iconKind} />
                      </span>
                      <span>{item.label}</span>
                    </NavLink>
                  </li>
                ))}
              </ul>
              <div className="account-menu-divider" />
              <button
                type="button"
                className="account-menu-item account-menu-logout"
                onClick={() => {
                  setMenuOpen(false);
                  logout();
                  void navigate("/login");
                }}
              >
                <span className="account-menu-icon">
                  <NavIcon kind="logout" />
                </span>
                <span>退出登录</span>
              </button>
            </div>
          ) : null}
        </div>
      </aside>

      <main className="workbench-main">
        <Outlet />
      </main>
    </div>
  );
}
