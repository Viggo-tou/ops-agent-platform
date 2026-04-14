import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../../lib/api";
import { useAuth } from "../../lib/auth";

interface ApprovalActionsProps {
  approvalId: string;
  actionName: string;
  taskId?: string;
  onDecision?: () => void;
}

export function ApprovalActions({ approvalId, actionName, taskId, onDecision }: ApprovalActionsProps) {
  const { user, backendActorRole, can } = useAuth();
  const queryClient = useQueryClient();
  const [decided, setDecided] = useState(false);

  const refreshTaskViews = async () => {
    setDecided(true);
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["tasks"] }),
      taskId ? queryClient.invalidateQueries({ queryKey: ["task", taskId] }) : Promise.resolve(),
      taskId ? queryClient.invalidateQueries({ queryKey: ["task-events", taskId] }) : Promise.resolve(),
    ]);
    onDecision?.();
  };

  const decisionPayload = {
    actor_name: user?.name ?? "unknown",
    actor_role: backendActorRole,
  };

  const grantMutation = useMutation({
    mutationFn: () => api.grantApproval(approvalId, decisionPayload),
    onSuccess: refreshTaskViews,
  });

  const rejectMutation = useMutation({
    mutationFn: () => api.rejectApproval(approvalId, decisionPayload),
    onSuccess: refreshTaskViews,
  });

  if (!can("approval:decide") || decided) {
    return null;
  }

  const isPending = grantMutation.isPending || rejectMutation.isPending;

  return (
    <div className="approval-actions">
      <span className="approval-label">审批：{actionName}</span>
      <button
        className="approval-btn approve"
        type="button"
        onClick={() => grantMutation.mutate()}
        disabled={isPending}
      >
        批准
      </button>
      <button
        className="approval-btn reject"
        type="button"
        onClick={() => rejectMutation.mutate()}
        disabled={isPending}
      >
        拒绝
      </button>
      {grantMutation.isError || rejectMutation.isError ? (
        <span className="approval-error">审批失败，请重试。</span>
      ) : null}
    </div>
  );
}
