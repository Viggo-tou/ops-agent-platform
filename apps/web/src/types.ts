export type TaskStatus =
  | "created"
  | "planning"
  | "reviewing"
  | "awaiting_approval"
  | "executing"
  | "queued"
  | "running"
  | "waiting_approval"
  | "completed"
  | "failed"
  | "rolled_back";

export type WorkflowStage =
  | "intake"
  | "planning"
  | "knowledge"
  | "action"
  | "review"
  | "done";

export type RoleName =
  | "primary"
  | "planner"
  | "knowledge"
  | "action"
  | "reviewer"
  | "system";

export type RiskLevel = "low" | "medium" | "high";
export type RiskCategory =
  | "general"
  | "knowledge_lookup"
  | "external_broadcast"
  | "change_management"
  | "cross_team_notification"
  | "configuration_change"
  | "production_write"
  | "privileged_data_access"
  | "knowledge_exfiltration";
export type ActorRole = "admin" | "team_lead" | "employee";
export type ApprovalStatus = "pending" | "granted" | "rejected" | "expired" | "cancelled";
export type ToolExecutionStatus = "running" | "succeeded" | "failed" | "timed_out";
export type ReviewFindingSeverity = "info" | "warning" | "error";
export type ReviewPolicyStatus = "passed" | "warning" | "failed";
export type ToolPermissionCategory = "read_only" | "write" | "approval_required";

export interface ReviewFinding {
  code: string;
  severity: ReviewFindingSeverity;
  message: string;
  step_id: string | null;
  field: string | null;
}

export interface ReviewPolicyCheck {
  name: string;
  status: ReviewPolicyStatus;
  detail: string;
}

export interface ReviewApprovalRequirement {
  action_name: string;
  reason: string;
  approver_role: string;
}

export interface TaskReviewDocument {
  schema_version: string;
  review_id: string;
  task_id: string;
  plan_id: string;
  review_stage: string;
  verdict: string;
  ready_for_execution: boolean;
  summary: string;
  findings: ReviewFinding[];
  missing_information: string[];
  policy_checks: ReviewPolicyCheck[];
  approval_requirements: ReviewApprovalRequirement[];
  recommended_status: string;
  provider: Record<string, unknown> | null;
}

export interface SemanticTranslationDocument {
  schema_version: string;
  translation_id: string;
  task_id: string;
  normalized_request: string;
  intent: string;
  work_type: "bugfix" | "feature" | "investigation" | "operations" | "question" | "unknown";
  objective: string;
  issue_key: string | null;
  issue_url: string | null;
  candidate_modules: string[];
  search_queries: string[];
  constraints: string[];
  requested_outputs: string[];
  grounding_terms: string[];
  missing_information: string[];
  confidence: number;
  provider: Record<string, unknown> | null;
}

export interface PlanCodeLocation {
  source_name: string;
  relative_path: string;
  reason: string;
  line_start: number | null;
  line_end: number | null;
}

export interface PlanStepDocument {
  step_id: string;
  title: string;
  kind: "analysis" | "knowledge" | "action" | "review";
  owner_role: RoleName;
  depends_on: string[];
  tool_name: string | null;
  expected_output: string;
  success_criteria: string;
}

export interface TaskPlanDocument {
  schema_version: string;
  plan_id: string;
  task_id: string;
  objective: string;
  request_summary: string;
  scenario: string;
  change_summary: string;
  change_explanation: string;
  assumptions: string[];
  missing_information: string[];
  risk_level: RiskLevel;
  requires_approval: boolean;
  approval_reasons: string[];
  affected_code_locations: PlanCodeLocation[];
  tools: Array<{
    tool_name: string;
    permission_category: ToolPermissionCategory;
    purpose: string;
  }>;
  steps: PlanStepDocument[];
  final_output_contract: {
    type: string;
    required_fields: string[];
  };
  provider: Record<string, unknown> | null;
}

export interface KnowledgeCitationResult {
  document_id: string;
  source_name: string;
  title: string;
  relative_path: string;
  line_start: number;
  line_end: number;
  snippet: string;
  score: number;
  metadata: Record<string, unknown>;
}

export interface KnowledgeAnswerTraceResult {
  source_name: string;
  source_path: string;
  selected_sources: string[];
  strategy: string;
  route_kind: string;
  route_reason: string;
  top_k: number;
  indexed_document_count: number;
  selected_paths: string[];
  matched_tokens: string[];
  token_coverage: number;
  top_score: number;
  citation_count: number;
  hallucination_risk: string;
  rationale: string;
}

export interface KnowledgeSearchResult {
  query: string;
  answer: string;
  citations: KnowledgeCitationResult[];
  answer_trace: KnowledgeAnswerTraceResult;
  packaged_context: string;
}

export interface Approval {
  id: string;
  task_id: string;
  action_name: string;
  status: ApprovalStatus;
  requested_by_role: RoleName;
  approver_role: string;
  requested_by_actor_name: string;
  decided_by_actor_name: string | null;
  risk_level: RiskLevel;
  risk_category: RiskCategory;
  reason: string;
  request_payload_json: Record<string, unknown> | null;
  policy_snapshot_json: Record<string, unknown> | null;
  decision_payload_json: Record<string, unknown> | null;
  requested_at: string;
  expires_at: string | null;
  decided_at: string | null;
}

export interface EventRecord {
  id: string;
  task_id: string;
  session_id: string | null;
  event_type: string;
  source: string;
  stage: WorkflowStage | null;
  role: RoleName | null;
  tool_name: string | null;
  message: string;
  payload_json: Record<string, unknown> | null;
  created_at: string;
}

export interface ToolExecutionRecord {
  id: string;
  task_id: string;
  session_id: string | null;
  approval_id: string | null;
  tool_name: string;
  provider_name: string;
  permission_category: ToolPermissionCategory;
  status: ToolExecutionStatus;
  actor_name: string | null;
  attempt_count: number;
  max_retries: number;
  timeout_seconds: number;
  duration_ms: number | null;
  request_payload_json: Record<string, unknown> | null;
  response_payload_json: Record<string, unknown> | null;
  attempt_log_json: Array<Record<string, unknown>> | null;
  error_message: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface ToolRegistryEntry {
  name: string;
  display_name: string;
  description: string;
  provider_name: string;
  permission_category: ToolPermissionCategory;
  enabled: boolean;
  status_message: string;
  missing_configuration: string[];
  requires_network: boolean;
  timeout_seconds: number;
  retry_count: number;
  tags: string[];
}

export interface TaskSummary {
  id: string;
  session_id: string | null;
  actor_name: string;
  actor_role: ActorRole;
  title: string;
  scenario: string;
  status: TaskStatus;
  workflow_stage: WorkflowStage;
  current_role: RoleName | null;
  risk_level: RiskLevel;
  risk_category: RiskCategory;
  pending_approval: boolean;
  retry_count: number;
  plan_provider_name: string | null;
  plan_provider_mode: string | null;
  plan_model_name: string | null;
  plan_used_fallback: boolean;
  plan_fallback_reason: string | null;
  review_stage: string | null;
  review_verdict: string | null;
  review_summary: string | null;
  created_at: string;
  updated_at: string;
}

export interface TaskListFilters {
  search?: string;
  sessionId?: string;
  status?: TaskStatus;
  provider?: string;
  actorRole?: ActorRole;
  riskCategory?: RiskCategory;
}

export interface TaskDetail extends TaskSummary {
  request_text: string;
  governance_json: Record<string, unknown> | null;
  translation_json: Record<string, unknown> | null;
  plan_json: Record<string, unknown> | null;
  review_json: Record<string, unknown> | null;
  latest_result_json: Record<string, unknown> | null;
  approvals: Approval[];
}

export interface TaskCreateInput {
  title?: string;
  request: string;
  actor_name: string;
  actor_role?: ActorRole;
  session_id?: string | null;
  /** When set, backend prepends parent failure context to request_text and inherits parent's session. */
  previous_task_id?: string | null;
  /** Repository source override (registry name). When undefined, orchestrator uses env default. */
  source_name?: string | null;
}

export interface KnowledgeSourceDescriptor {
  source_name: string;
  source_path: string;
  indexed_document_count: number;
}

export interface KnowledgeDocumentSummary {
  id: string;
  source_name: string;
  relative_path: string;
  title: string;
  extension: string;
  language: string | null;
  size_bytes: number;
  line_count: number;
  metadata_json: Record<string, unknown> | null;
  updated_at: string;
}

export interface KnowledgeSyncResponse {
  source_name: string;
  source_path: string;
  indexed_documents: number;
  updated_documents: number;
  removed_documents: number;
}

export interface KnowledgeUploadSkipped {
  file_name: string;
  reason: string;
}

export interface KnowledgeUploadResponse {
  source_name: string;
  source_path: string;
  indexed_documents: KnowledgeDocumentSummary[];
  skipped: KnowledgeUploadSkipped[];
}

export interface KnowledgeDeleteResponse {
  source_name: string;
  removed_documents: number;
  removed_from_disk: boolean;
}

export interface MemoryItem {
  id: string;
  title: string;
  body: string;
  topic: string;
  created_at: string;
  updated_at: string;
}

export interface MemoryItemCreate {
  title: string;
  body: string;
  topic?: string;
}

export interface MemoryItemUpdate {
  title?: string;
  body?: string;
  topic?: string;
}

export interface MemorySettings {
  enabled: boolean;
  allow_list: string;
  block_list: string;
  updated_at: string;
}

export interface MemorySettingsUpdate {
  enabled?: boolean;
  allow_list?: string;
  block_list?: string;
}

export interface ModelEntry {
  id: string;
  display_name: string;
  sort_order: number;
}

export interface ModelProvider {
  name: string;
  note: string;
  sort_order: number;
  models: ModelEntry[];
}

export interface SelectedModel {
  model_id: string | null;
  updated_at: string;
}

export interface SelectedModelUpdate {
  model_id: string | null;
}
