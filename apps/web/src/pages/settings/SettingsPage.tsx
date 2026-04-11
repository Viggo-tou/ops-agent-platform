import { PermissionGuard } from "../../components/auth/PermissionGuard";
import { ModelSelector } from "../../components/settings/ModelSelector";

export function SettingsPage() {
  return (
    <div className="content-page reference-page settings-page-shell">
      <header className="content-header">
        <span>Settings</span>
        <h1>Settings</h1>
        <p>Choose model and connection settings for the assistant.</p>
      </header>

      <PermissionGuard
        permission="settings:view"
        fallback={<div className="permission-note">Your role cannot view system settings.</div>}
      >
        <ModelSelector />
      </PermissionGuard>
    </div>
  );
}
