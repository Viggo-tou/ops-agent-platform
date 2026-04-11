import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

export type AppRole = "admin" | "operator" | "member" | "viewer";

export type Permission =
  | "task:create"
  | "task:create_high_risk"
  | "knowledge:upload"
  | "knowledge:delete"
  | "memory:edit"
  | "settings:view"
  | "settings:model_config"
  | "approval:decide";

export interface AppUser {
  name: string;
  email: string;
  role: AppRole;
}

interface AuthContextValue {
  user: AppUser | null;
  login: (user: AppUser) => void;
  logout: () => void;
  can: (permission: Permission) => boolean;
  backendActorRole: "admin" | "team_lead" | "employee";
}

const STORAGE_KEY = "ops-agent-workbench-user";

const rolePermissions: Record<AppRole, Permission[]> = {
  admin: [
    "task:create",
    "task:create_high_risk",
    "knowledge:upload",
    "knowledge:delete",
    "memory:edit",
    "settings:view",
    "settings:model_config",
    "approval:decide",
  ],
  operator: [
    "task:create",
    "task:create_high_risk",
    "knowledge:upload",
    "memory:edit",
    "settings:view",
    "settings:model_config",
    "approval:decide",
  ],
  member: ["task:create", "memory:edit"],
  viewer: [],
};

function readStoredUser(): AppUser | null {
  const raw = window.localStorage.getItem(STORAGE_KEY);
  if (!raw) {
    return null;
  }

  try {
    const parsed = JSON.parse(raw) as Partial<AppUser>;
    if (!parsed.name || !parsed.email || !parsed.role) {
      return null;
    }
    if (!["admin", "operator", "member", "viewer"].includes(parsed.role)) {
      return null;
    }
    return {
      name: parsed.name,
      email: parsed.email,
      role: parsed.role,
    };
  } catch {
    return null;
  }
}

function toBackendActorRole(role: AppRole): "admin" | "team_lead" | "employee" {
  if (role === "admin") {
    return "admin";
  }
  if (role === "operator") {
    return "team_lead";
  }
  return "employee";
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AppUser | null>(() => readStoredUser());

  const value = useMemo<AuthContextValue>(() => {
    const backendActorRole = toBackendActorRole(user?.role ?? "viewer");
    return {
      user,
      backendActorRole,
      login: (nextUser) => {
        window.localStorage.setItem(STORAGE_KEY, JSON.stringify(nextUser));
        setUser(nextUser);
      },
      logout: () => {
        window.localStorage.removeItem(STORAGE_KEY);
        setUser(null);
      },
      can: (permission) => {
        if (!user) {
          return false;
        }
        return rolePermissions[user.role].includes(permission);
      },
    };
  }, [user]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used inside AuthProvider");
  }
  return context;
}
