import { Navigate, Outlet, useLocation } from "react-router-dom";

import { useAuth } from "../../lib/auth";

export function AuthGuard() {
  const { user } = useAuth();
  const location = useLocation();

  if (!user) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }

  return <Outlet />;
}
