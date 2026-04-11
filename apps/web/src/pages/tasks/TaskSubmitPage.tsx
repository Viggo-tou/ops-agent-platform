import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { startTransition, useState } from "react";
import { useNavigate } from "react-router-dom";

import { TaskTable } from "../../components/tasks/TaskTable";
import { ToolRegistryPanel } from "../../components/tasks/ToolRegistryPanel";
import { api } from "../../lib/api";
import { formatSyncTime, toErrorMessage } from "../../lib/format";

const starterPrompts = [
  "Where should I look to debug customer login in the Handyman app?",
  "Why might ChatBoxTest fail in the Handyman app codebase?",
  "Plan Jira issue OPS-123 for implementation and rollout.",
  "Plan this Jira task for implementation: https://p69projecta.atlassian.net/jira/software/projects/P69/boards/34/backlog?selectedIssue=P69-10",
  "Post to #ops-alerts: Deployment is delayed by 20 minutes while we investigate login errors.",
  "Create a Jira bug for the login regression in the mobile app project OPS.",
  "Notify all teams about an access change and wait for approval.",
];

export function TaskSubmitPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [title, setTitle] = useState("");
  const [request, setRequest] = useState(starterPrompts[0]);
  const [actorName, setActorName] = useState("employee");

  const tasksQuery = useQuery({
    queryKey: ["tasks"],
    queryFn: () => api.listTasks(),
    refetchInterval: 5_000,
  });

  const registryQuery = useQuery({
    queryKey: ["tool-registry"],
    queryFn: () => api.getToolRegistry(),
    refetchInterval: 15_000,
  });

  const createTaskMutation = useMutation({
    mutationFn: api.createTask,
    onSuccess: async (task) => {
      await queryClient.invalidateQueries({ queryKey: ["tasks"] });
      startTransition(() => {
        void navigate(`/tasks/${task.id}`);
      });
    },
  });

  return (
    <div className="page-grid">
      <section className="hero-panel">
        <div className="hero-copy">
          <div className="eyebrow">Task Intake</div>
          <h2>Submit a request into the primary runtime</h2>
          <p>
            The backend will create a persistent task, record session events, generate a plan, and expose
            progress in the dashboard.
          </p>
        </div>

        <div className="hero-stats">
          <div className="stat-card">
            <span>Runtime</span>
            <strong>Single Primary Agent</strong>
          </div>
          <div className="stat-card">
            <span>Persistence</span>
            <strong>Task / Event / Approval</strong>
          </div>
          <div className="stat-card">
            <span>Tools</span>
            <strong>Knowledge + Slack + Jira + Internal</strong>
          </div>
        </div>
      </section>

      {registryQuery.data ? <ToolRegistryPanel tools={registryQuery.data} compact /> : null}
      {registryQuery.isError ? <div className="error-banner">{toErrorMessage(registryQuery.error)}</div> : null}

      <section className="form-card">
        <div className="section-header">
          <div>
            <div className="eyebrow">New Request</div>
            <h3>Task Submission</h3>
          </div>
        </div>

        <form
          className="task-form"
          onSubmit={(event) => {
            event.preventDefault();
            createTaskMutation.mutate({
              title: title.trim() || undefined,
              request,
              actor_name: actorName,
            });
          }}
        >
          <label className="field">
            <span>Title</span>
            <input
              className="text-input"
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="Optional short summary"
            />
          </label>

          <label className="field">
            <span>Actor Name</span>
            <input
              className="text-input"
              value={actorName}
              onChange={(event) => setActorName(event.target.value)}
              placeholder="employee"
            />
          </label>

          <label className="field">
            <span>Request</span>
            <textarea
              className="text-area"
              value={request}
              onChange={(event) => setRequest(event.target.value)}
              rows={8}
            />
          </label>

          <div className="muted-text">
            Slack requests work best when you include a channel like `#ops-alerts`. Jira requests work best when
            you say `bug`, `story`, or `task` and optionally include `project OPS`. If you want the agent to
            plan an existing Jira task, include the issue key or paste the Jira URL together with a verb like
            `plan`.
          </div>

          <div className="prompt-row">
            {starterPrompts.map((prompt) => (
              <button key={prompt} type="button" className="prompt-chip" onClick={() => setRequest(prompt)}>
                {prompt}
              </button>
            ))}
          </div>

          {createTaskMutation.isError ? (
            <div className="error-banner">{toErrorMessage(createTaskMutation.error)}</div>
          ) : null}

          <div className="button-row">
            <button className="button primary" type="submit" disabled={createTaskMutation.isPending}>
              {createTaskMutation.isPending ? "Submitting..." : "Create Task"}
            </button>
          </div>
        </form>
      </section>

      <section className="detail-card">
        <div className="section-header">
          <div>
            <div className="eyebrow">Live Backlog</div>
            <h3>Recent Tasks</h3>
          </div>
          <div className="live-hint">{formatSyncTime(tasksQuery.dataUpdatedAt)}</div>
        </div>

        {tasksQuery.isLoading ? <div className="loading-panel">Loading tasks...</div> : null}
        {tasksQuery.isError ? <div className="error-banner">{toErrorMessage(tasksQuery.error)}</div> : null}
        {tasksQuery.data ? <TaskTable tasks={tasksQuery.data.slice(0, 5)} /> : null}
      </section>
    </div>
  );
}
