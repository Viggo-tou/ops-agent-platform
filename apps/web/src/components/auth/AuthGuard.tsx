import { Navigate, Outlet, useLocation } from "react-router-dom";

import { useAuth } from "../../lib/auth";

export function AuthGuard() {
  const { user } = useAuth();
  const location = useLocation();

  if (!user) {
    // Send unauth users to the public Welcome page first; they can click
    // Sign in there. Direct deep-links (e.g. /tasks/<id>) preserve their
    // intended destination via state.from so login can return them.
    return <Navigate to="/welcome" replace state={{ from: location.pathname }} />;
  }

  return <Outlet />;
}
