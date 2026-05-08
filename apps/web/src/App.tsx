import { Navigate, Route, Routes } from "react-router-dom";

import { AuthGuard } from "./components/auth/AuthGuard";
import { AppShell } from "./components/layout/AppShell";
import { LoginPage } from "./pages/auth/LoginPage";
import { ChatPage } from "./pages/chat/ChatPage";
import { HomePage } from "./pages/home/HomePage";
import { KnowledgePage } from "./pages/knowledge/KnowledgePage";
import { MemoryPage } from "./pages/memory/MemoryPage";
import { SettingsPage } from "./pages/settings/SettingsPage";
import { TaskDetailPage } from "./pages/tasks/TaskDetailPage";
import { TaskListPage } from "./pages/tasks/TaskListPage";
import { GovernancePage } from "./pages/governance/GovernancePage";
import { IntegrationsPage } from "./pages/integrations/IntegrationsPage";
import { OnboardingPage } from "./pages/onboarding/OnboardingPage";
import { RepositoriesPage } from "./pages/repositories/RepositoriesPage";
import { WelcomePage } from "./pages/welcome/WelcomePage";

export default function App() {
  return (
    <Routes>
      <Route path="/welcome" element={<WelcomePage />} />
      <Route path="/login" element={<LoginPage />} />
      <Route element={<AuthGuard />}>
        <Route path="/onboarding" element={<OnboardingPage />} />
        <Route element={<AppShell />}>
          <Route index element={<Navigate to="/chat" replace />} />
          <Route path="/home" element={<HomePage />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/chat/:taskId" element={<ChatPage />} />
          <Route path="/knowledge" element={<KnowledgePage />} />
          <Route path="/memory" element={<MemoryPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/tasks" element={<TaskListPage />} />
          <Route path="/tasks/:taskId" element={<TaskDetailPage />} />
          <Route path="/governance" element={<GovernancePage />} />
          <Route path="/repositories" element={<RepositoriesPage />} />
          <Route path="/integrations" element={<IntegrationsPage />} />
          <Route path="*" element={<Navigate to="/chat" replace />} />
        </Route>
      </Route>
    </Routes>
  );
}
