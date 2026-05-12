import { useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { type AppRole, useAuth } from "../../lib/auth";

const roleOptions: Array<{ role: AppRole; label: string; description: string }> = [
  { role: "operator", label: "Operator", description: "Can run tasks, approve work, and tune model settings." },
  { role: "admin", label: "Admin", description: "Full workspace control." },
  { role: "member", label: "Member", description: "Can create normal work requests and manage personal memory." },
  { role: "viewer", label: "Viewer", description: "Read-only access." },
];

export function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const { login } = useAuth();
  const [name, setName] = useState("Tomonkyo");
  const [email, setEmail] = useState("tomonkyo@example.com");
  const [role, setRole] = useState<AppRole>("operator");

  const from = typeof location.state === "object" && location.state && "from" in location.state
    ? String(location.state.from)
    : "/chat";

  return (
    <main className="login-page">
      <section className="login-card">
        <div className="login-copy">
          <span>Ops Agent</span>
          <h1>Sign in to the workspace</h1>
          <p>
            This local demo uses a frontend session so RBAC and guarded actions can be exercised without adding a
            full identity provider yet.
          </p>
        </div>

        <form
          className="login-form"
          onSubmit={(event) => {
            event.preventDefault();
            login({ name: name.trim() || "Operator", email: email.trim() || "operator@example.com", role });
            void navigate(from, { replace: true });
          }}
        >
          <label className="field">
            <span>Name</span>
            <input value={name} onChange={(event) => setName(event.target.value)} />
          </label>
          <label className="field">
            <span>Email</span>
            <input value={email} onChange={(event) => setEmail(event.target.value)} />
          </label>

          <div className="role-picker">
            {roleOptions.map((option) => (
              <button
                key={option.role}
                type="button"
                className={role === option.role ? "role-card selected" : "role-card"}
                onClick={() => setRole(option.role)}
              >
                <strong>{option.label}</strong>
                <span>{option.description}</span>
              </button>
            ))}
          </div>

          <button className="primary-action" type="submit">
            Continue
          </button>
        </form>
      </section>
    </main>
  );
}
