from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    app_name: str = "Ops Agent Platform API"
    debug: bool = True
    api_prefix: str = "/api"
    database_url: str = f"sqlite:///{(BASE_DIR / 'ops_agent_platform.db').as_posix()}"
    # Pipeline worker pool size. Each worker runs one task end-to-end
    # (planner → knowledge → codegen → sandbox → review). Most of the
    # time is I/O wait (LLM HTTP, git apply, gradle compile), so 6 is
    # safe on a typical dev machine. Bumped from 2 — at 2, every backend
    # restart's _resume_interrupted_tasks would consume both slots and
    # leave new chat-triggered tasks queued for minutes. Override via
    # OPS_AGENT_PIPELINE_MAX_WORKERS in .env.
    pipeline_max_workers: int = 6
    resumability_enabled: bool = True
    resumability_max_age_hours: int = 6
    resumability_orphan_threshold_hours: int = 6
    primary_agent_provider: Literal["auto", "mock", "openai", "minimax", "anthropic", "deepseek", "ollama", "claude_code", "codex"] = "auto"
    planner_provider: Literal["auto", "claude_code", "anthropic", "openai", "minimax", "deepseek", "mock"] | None = None
    codegen_provider: Literal["auto", "claude_code", "codex", "anthropic", "openai", "minimax", "deepseek", "ollama", "mock"] | None = None
    # Codegen output format. "auto" picks per-provider: deepseek + openai
    # → aider_blocks (15-25 pp gain on mid-tier per Aider data); others
    # → unified_diff. Force a specific value for A/B measurement.
    codegen_output_format: Literal["auto", "unified_diff", "aider_blocks"] = "auto"
    # Agent mode (Tier 4 main course). "static" = the existing 1-shot
    # codegen + retry + structural-gate pipeline. "loop" = multi-turn
    # agent that issues read_file/search_symbol/list_directory tool
    # calls then commits via apply_diff. Static stays default until
    # the loop validates ≥ static quality on the 4-task regression.
    codegen_agent_mode: Literal["static", "loop"] = "static"
    # Agent-loop turn cap (hard stop).
    codegen_agent_max_turns: int = 12
    # Agent-loop wall-clock cap, seconds.
    codegen_agent_max_seconds: float = 600.0
    primary_agent_model: str = "gpt-4o-mini"
    primary_agent_timeout_seconds: float = 30.0
    minimax_planner_timeout_seconds: float = 90.0
    knowledge_synthesis_enabled: bool = True
    knowledge_synthesis_provider: Literal["minimax", "deepseek"] = "minimax"
    knowledge_synthesis_model: str = "MiniMax-M2.7"
    # Empty = inherit settings.deepseek_model (e.g. deepseek-v4-pro from .env).
    # Set only when synthesis needs a different model from codegen/cc_agent.
    knowledge_synthesis_deepseek_model: str = ""
    knowledge_synthesis_timeout_seconds: float = 45.0
    # Per-citation snippet cap fed to the answer synthesiser. AST chunking
    # now returns whole function bodies, so keep enough context for later
    # control-flow and validation logic inside a cited function.
    knowledge_synthesis_max_snippet_chars: int = 6000
    knowledge_cards_enabled: bool = True
    knowledge_cards_provider: str = "minimax"
    knowledge_cards_model: str = "MiniMax-M2.7"
    knowledge_cards_max_chars: int = 400 * 6
    knowledge_cards_concurrency: int = 5
    knowledge_retrieval_cache_enabled: bool = True
    knowledge_retrieval_cache_ttl_seconds: int = 3600
    knowledge_retrieval_cache_max_entries: int = 1000
    knowledge_source_router_enabled: bool = True
    knowledge_source_router_timeout_seconds: float = 15.0
    llm_retry_on_rate_limit: bool = True
    llm_retry_max_attempts: int = 3
    llm_retry_base_delay_sec: float = 2.0
    # Tool 2: LLM HTTP cache for fast iteration. When "record", real LLM
    # calls go through and responses are persisted to disk. When "replay",
    # cache hits return immediately without HTTP calls; misses fall
    # through to real call. When "off" (default), bypass entirely.
    # Hard kill switch for jira_issue_writeback scenario. When True,
    # _execute_writeback_plan returns immediately without posting any
    # Jira comment or transitioning issue status. Default True after
    # 2026-05-07 incident: continuation classifier mis-routed v48/v48b
    # to writeback and posted spurious comments. Re-enable explicitly
    # when intentional writeback is needed.
    jira_writeback_disabled: bool = True
    llm_cache_mode: str = "off"  # off | record | replay
    llm_cache_dir: str = "apps/backend/data/llm_cache"
    llm_cache_replay_on_miss: str = "passthrough"  # passthrough | raise
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-coder"
    deepseek_timeout_seconds: float = 120.0
    # Phase A.2 (2026-05-11): DeepSeek V4-Pro thinking-mode config.
    # Per DeepSeek docs: low/medium → high; xhigh → max. We map our
    # stages explicitly so planner / codegen / synth each get the
    # right effort without depending on the implicit default.
    deepseek_reasoning_effort_planner: Literal["low", "medium", "high", "max"] = "max"
    deepseek_reasoning_effort_codegen: Literal["low", "medium", "high", "max"] = "high"
    deepseek_reasoning_effort_synth: Literal["low", "medium", "high", "max"] = "high"
    # max_tokens bumped (was 8K). V4-Pro outputs up to 384K; long
    # patches + reasoning_content easily fit in 32K, and the old 8K
    # caused empty-response cliffs (v14 agent loop turn 8).
    deepseek_max_tokens_planner: int = 16384
    deepseek_max_tokens_codegen: int = 32768
    deepseek_max_tokens_agent_loop: int = 32768
    # Phase B (2026-05-11): per-file byte budget the codegen pipeline
    # uses for AST truncation. 500K bumps the old 18K cap so DeepSeek-
    # V4-Pro's 1M input window can actually carry full files. Only
    # files larger than this still go through AST truncation as
    # emergency fallback.
    codegen_per_file_byte_budget: int = 500_000
    # Phase B.2 (2026-05-11): total bytes the codegen evidence pack may
    # consume across all included files. Independent from the planner's
    # tighter ``evidence_pack_max_total_bytes`` so codegen can take 4-8
    # full must_touch files (e.g. 4 Kotlin Composables of 60 KB each)
    # without overflowing the model context. 2 MB is well under the
    # DeepSeek-V4-Pro 1M-token reliable window once prompt overhead is
    # accounted for.
    codegen_total_file_byte_budget: int = 2_000_000
    # v15 Ticket 3 (2026-05-11): preplan_discover -> evidence manifest
    # bridge. The candidates returned by preplan_discover are written
    # as cc_read EvidenceItems so evidence_chain can close even when
    # the heavier knowledge.search path wasn't taken. ``snippet_bytes``
    # is how many bytes of real file content to embed per item.
    evidence_preplan_candidate_limit: int = 10
    evidence_preplan_snippet_bytes: int = 1024
    # v15 Ticket 5 (2026-05-11): semantic_review JSON hardening. v14
    # showed the reviewer returning invalid JSON and the harness
    # silently skipping the gate. New flow: 1 review attempt; on
    # parse-fail run 1 focused JSON-repair pass that re-shapes the
    # prior output without re-running the review; if repair still
    # fails, the gate is recorded as ``status=unavailable`` rather
    # than masquerading as ``skipped``/``passed``. raw_preview keeps
    # ~500 chars of the bad output for audit; longer values bloat
    # event payloads without adding insight.
    semantic_review_max_review_attempts: int = 1
    semantic_review_max_repair_attempts: int = 1
    semantic_review_raw_preview_chars: int = 500
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_model: str = "qwen3.5"
    ollama_timeout_seconds: float = 600.0
    ollama_max_context_files: int = 2
    ollama_max_file_chars: int = 8000
    anthropic_api_key: str | None = None
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_model: str = "claude-sonnet-4-20250514"
    # Claude Code CLI (planner)
    claude_code_command: str = "npx"
    claude_code_args: str = "--yes @anthropic-ai/claude-code"
    claude_code_timeout_seconds: float = 600.0  # bumped 240→600 after P69-19 timeout
    claude_code_git_bash_path: str | None = None
    # Codex CLI (codegen)
    codex_command: str = "codex"
    codex_timeout_seconds: float = 240.0
    codex_model: str = "gpt-5.4"
    # CLI retry: retry codegen/planning subprocess on timeout or transient failure
    cli_max_retries: int = 1
    gate_repair_max_attempts: int = 1
    gate_repair_timeout_seconds: float = 300.0
    # Per-file parallel codegen: concurrent workers for develop pipeline codegen.
    # 1 = serial (old batched behavior), 2-3 = parallel (faster, no truncation).
    codegen_parallel_max: int = 2
    # T-PIPELINE-REPAIR-CAP: multi-round compile_gate repair loop. When a
    # round exceeds the timeout it counts as a failed round (not a stall).
    # When all rounds exhaust and `..._to_approval` is True (default) the
    # task transitions to AWAITING_APPROVAL with a structured payload so a
    # reviewer can decide what to do; setting it False keeps the legacy
    # fail-fast behaviour.
    # Empirical (2026-05-06): repair rounds rarely converge after round 2.
    # v33 semantic_review thrashed 10%→40%→15%; v36 compile_repair completed
    # 3 rounds before reaching success in round 3 (saved by L4f name-lock).
    # Lowering 3→2 cuts ~5-10 min off runs that exhaust without success
    # while still allowing one repair attempt for the common "off-by-one"
    # codegen mistake. Override per-task via settings if needed.
    codegen_max_repair_rounds: int = 1  # 2026-05-07: cut 2→1; rounds 2+ rarely converge in practice
    # Stage A codegen self-validation: validate diff applies + parses
    # before codegen returns. Catches hunk drift at source.
    codegen_react_loop_enabled: bool = False
    codegen_self_validation_enabled: bool = True
    codegen_self_validation_max_retries: int = 1
    # v16 P0 #8: per-batch deadline so a single stuck codegen batch can't
    # hold the action-stage clock past the 30-min watchdog. Batches that
    # exceed this get a tool_timed_out event and the coverage gate runs
    # on the partial result.
    codegen_batch_deadline_seconds: float = 720.0
    # If a parallel codegen batch hits the Python-level deadline while
    # sibling batches produced usable diffs, retry only the missing batch
    # once with a shorter, sequential call. This recovers transient provider
    # stalls without weakening batch_coverage.
    codegen_timeout_salvage_enabled: bool = True
    codegen_timeout_salvage_seconds: float = 240.0
    codegen_timeout_late_result_grace_seconds: float = 360.0
    codegen_repair_files_per_round: int = 5
    # Bumped from 180s. Empirically a single codegen.generate_patch call
    # via Claude Code CLI takes 3-4 min (~180-240s). The 180s deadline
    # would fire BEFORE codegen returned, so the patch was generated but
    # the round was marked timed_out and the result discarded — leaving
    # the task stuck in an infinite "round N timed out → start round N+1
    # → timed out again" loop. 600s gives one codegen call comfortable
    # headroom AND room for a second pass when the round queues 2+ files.
    codegen_repair_round_timeout_seconds: float = 600.0
    # C7 liveness fix (2026-05-12): per-call wall-clock limit inside the
    # repair round. Without this, a single hung provider/tool socket
    # bypasses the round deadline (which is only checked between files)
    # and the whole compile_repair stage loses its bounded-terminal
    # guarantee. The actual timeout at runtime is
    # ``min(this, remaining_round_budget - safety_margin)`` so the
    # per-call cap can never overshoot the round deadline. Empirically
    # 120s is generous for Claude Code / DeepSeek-V4-Pro repair calls
    # (median 30–60s, p95 ~90s) while still letting a 5-file round
    # complete within the 600s round deadline.
    codegen_repair_per_call_timeout_seconds: float = 120.0
    # Safety margin subtracted from the remaining round budget before
    # computing each call timeout. Prevents the call deadline from
    # racing the round deadline and the round emitting both
    # tool_call_timeout AND round_timed_out for the same wall-clock
    # instant.
    codegen_repair_call_safety_margin_seconds: float = 5.0
    codegen_repair_cap_exceeded_to_approval: bool = True
    repair_intent_preservation_threshold: float = 0.4
    verification_compile_fail_to_approval: bool = False  # Stage 25 contract: cap-exceeded -> fail
    verification_profile_enabled: bool = True
    verification_compile_timeout_seconds: int = 240
    kotlinc_precheck_enabled: bool = True
    kotlinc_precheck_timeout_seconds: int = 30
    verification_max_repair_rounds: int = 1  # See codegen_max_repair_rounds note
    failure_diagnosis_enabled: bool = True
    failure_diagnosis_timeout_seconds: float = 30.0
    failure_diagnosis_max_events: int = 30
    failure_diagnosis_keyfile_head_chars: int = 500
    semantic_translator_provider: Literal["auto", "mock", "minimax"] = "auto"
    semantic_translator_model: str = "MiniMax-M2.7"
    semantic_translator_timeout_seconds: float = 30.0
    semantic_review_high_blocks_on_exhausted: bool = True
    minimax_api_key: str | None = None
    minimax_base_url: str = "https://api.minimaxi.com"
    knowledge_source_name: str = "handymanapp"
    knowledge_source_path: str | None = None
    knowledge_source_specs: str | None = None
    # P0-3 (2026-05-11): deterministic Jira project key → KB source map.
    # Format: "P69:handymanapp;ABC:hosteddashboard". When a request
    # references a Jira issue like "P69-19" and source_name is not
    # explicitly set, the task router picks the mapped source instead
    # of falling back to env default. Avoids the v1 failure where P69-*
    # got routed to hosteddashboard (admin web) and anchor lookup fails.
    jira_project_source_map: str | None = None
    knowledge_top_k: int = 4
    knowledge_max_file_bytes: int = 120_000
    knowledge_chunk_max_lines: int = 300
    knowledge_chunk_min_lines: int = 5
    knowledge_chunk_fallback_radius: int = 10
    knowledge_excluded_extensions: str = (
        ".css,.scss,.sass,.less,"
        ".svg,.png,.jpg,.jpeg,.gif,.webp,.ico,.bmp,"
        ".woff,.woff2,.ttf,.otf,.eot,"
        ".pdf,.zip,.tar,.gz,.7z,.rar,"
        ".mp3,.mp4,.mov,.wav,.avi,.mkv,"
        ".lock,.min.js,.min.css"
    )
    evidence_must_touch_excluded_extensions: str = (
        ".lock,.min.js,.min.css,.map,.tar,.gz,.zip,.7z,.rar,.pdf,"
        ".png,.jpg,.jpeg,.gif,.svg,.webp,.ico,.bmp,"
        ".woff,.woff2,.ttf,.otf,.eot,"
        ".mp3,.mp4,.mov,.wav,.avi,.mkv,"
        ".pyc,.pyo,.class,.dll,.so,.dylib,.exe,"
        # Documentation + i18n + repo metadata: a Python bug fix never
        # lives in these. Surfaced as 2026-05-10 task 4 regression
        # where the planner left must_touch empty and retrieval
        # surfaced .po / .rst files which then got dispatched as
        # codegen targets.
        ".po,.pot,.mo,.rst,.md,"
        # Templates + frontend assets: surfaced 2026-05-10 v9 task 3
        # where empty planner must_touch let retrieval inject 20 admin
        # HTML / JS / CSS files as codegen batch targets — every batch
        # honestly emitted PLAN_CONFLICT but burned tokens.
        ".html,.htm,.js,.jsx,.ts,.tsx,.css,.scss,.sass,.vue,.svelte"
    )
    evidence_must_touch_excluded_path_segments: str = (
        "build/,build-before/,build-after/,dist/,node_modules/,"
        "__pycache__/,.next/,.cache/,.tmp/,data/sandboxes/,data/agent_workspace/,"
        "locale/,locales/,docs/,doc/,"
        # Static + template directories — Django convention puts admin
        # HTML in `templates/` and frontend bundles in `static/`. None
        # of those are valid codegen targets for an ORM/query bug.
        "templates/,static/,assets/,public/"
    )
    evidence_must_touch_excluded_filenames: str = (
        "package.json,package-lock.json,yarn.lock,pnpm-lock.yaml,"
        "tsconfig.json,jsconfig.json,.eslintrc*,.prettierrc*,.editorconfig,"
        "cors.json,firebase.json,poetry.lock,requirements.txt,requirements-*.txt,"
        "go.sum,cargo.lock,"
        # Repo-root metadata files (often surfaced by retrieval as
        # high-coverage hits but never the actual fix target).
        "license,license.*,authors,install,install.*,changelog,changelog.*,"
        "copying,copying.*,manifest.in,readme,readme.*,contributing,contributing.*,"
        "notice,trove_classifiers,py.typed"
    )
    evidence_must_touch_include_configs: bool = False
    # Semantic reranker: when enabled, the keyword-based retriever picks
    # knowledge_rerank_pool_size top candidates, then an LLM reranks them
    # and the final top_k slice is taken from the LLM-ranked order.
    knowledge_rerank_enabled: bool = True
    knowledge_rerank_pool_size: int = 15
    knowledge_rerank_timeout_seconds: float = 20.0
    knowledge_rerank_snippet_chars: int = 600
    knowledge_fts5_enabled: bool = True
    knowledge_fts5_pool_multiplier: int = 5
    # Query expansion: ask an LLM for additional likely-source tokens to
    # add to the retrieval token set, addressing the recall gap where
    # natural-language phrases don't share surface tokens with actual
    # identifiers (e.g. "approval workflow" vs HandymanVerification.js).
    # ON by default: expansion is additive-only, deterministic, timeout
    # bounded, and fails safe to the original retrieval token set.
    knowledge_query_rewrite_enabled: bool = True
    knowledge_query_rewrite_timeout_seconds: float = 15.0
    memory_enabled: bool = True
    memory_judge_provider: str = "minimax"
    memory_judge_model: str = "MiniMax-M2.7"
    memory_top_n_per_query: int = 3
    memory_max_lines_in_prompt: int = 30
    memory_dedup_threshold: float = 0.85
    memory_judge_timeout_seconds: int = 30
    cc_agentic_enabled: bool = True
    cc_agent_provider_chain: str = "claude_code,codex,minimax"
    cc_agent_max_rounds: int = 3
    cc_agent_max_tool_calls: int = 8
    cc_agent_overall_timeout_s: float = 30.0
    cc_agent_per_call_timeout_s: float = 20.0
    cc_grep_default_excludes: list[str] = [
        "*.css", "*.scss", "*.svg", "*.png", "*.jpg", "*.jpeg",
        "*.gif", "*.webp", "*.lock", "*.min.js", "*.min.css",
        "node_modules/**", "dist/**", "build/**", ".git/**",
    ]
    knowledge_upload_root: str = str((BASE_DIR / "data" / "uploads").as_posix())
    knowledge_upload_default_source: str = "uploads"
    knowledge_upload_max_bytes: int = 2_000_000
    tool_permission_overrides: str | None = None
    tool_default_timeout_seconds: float = 15.0
    tool_default_retry_count: int = 1
    # Default points to a relative path next to backend; can be overridden
    # via env to escape non-ASCII parent paths (Windows Android Gradle plugin
    # rejects non-ASCII paths).
    sandbox_base_dir: str = "data/sandboxes"
    sandbox_external_root: str | None = None
    sandbox_clone_timeout_seconds: float = 120.0
    # Sandbox retention: at startup, delete sandbox dirs whose owning task
    # is in a terminal status AND completed earlier than this many hours
    # ago. Active / pending tasks are never swept regardless of age.
    sandbox_retention_hours: float = 168.0  # 7 days
    sandbox_command_timeout_seconds: float = 60.0
    sandbox_max_output_bytes: int = 65536
    agent_workspace_root: str = str((BASE_DIR / "data" / "agent_workspace").as_posix())
    agent_workspace_retention_hours: int = 168
    agent_workspace_archive_on_complete: bool = False
    agent_workspace_snippet_inline_threshold: int = 4000
    evidence_chain_gate_enabled: bool = True
    evidence_chain_min_confident_claims: int = 3
    evidence_chain_strong_sources: str = (
        "rag_lexical,rag_fts5,rag_card,cc_glob,cc_grep,cc_read,spec_anchor"
    )
    evidence_chain_block_on_attestation_mismatch: bool = True
    alert_webhook_url: str | None = None
    slack_base_url: str = "https://slack.com"
    slack_bot_token: str | None = None
    slack_default_channel: str | None = None
    slack_post_message_timeout_seconds: float = 10.0
    slack_post_message_retry_count: int = 1
    jira_base_url: str | None = None
    jira_email: str | None = None
    jira_api_token: str | None = None
    jira_bearer_token: str | None = None
    jira_project_key: str | None = None
    jira_issue_type: str = "Task"
    jira_timeout_seconds: float = 15.0
    jira_retry_count: int = 1
    # T-039: require human approval between code-generation-pass and
    # jira.transition_issue writeback. When True, the develop pipeline
    # pauses in AWAITING_APPROVAL after spec_conformance.attest pass,
    # exposing the diff + goal_attestation in the approval record, and
    # only transitions Jira once the approval is granted.
    develop_require_jira_approval: bool = True
    internal_api_base_url: str | None = None
    internal_api_token: str | None = None
    internal_api_auth_header: str = "Authorization"
    internal_api_timeout_seconds: float = 10.0
    internal_api_retry_count: int = 1
    internal_db_url: str | None = None
    internal_db_timeout_seconds: float = 8.0
    internal_db_retry_count: int = 0
    internal_db_max_rows: int = 50
    frontend_origins: list[str] = [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ]
    # MCP (Model Context Protocol) client config. Format matches Claude
    # Desktop's claude_desktop_config.json mcpServers block, e.g.:
    #   {"filesystem":{"command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","/tmp"]}}
    # Tools from connected servers surface as mcp.<server>.<tool> in the
    # ToolRegistry. Empty = MCP disabled, backend boots normally.
    mcp_servers_json: str = ""
    # Per-server initialize handshake timeout (seconds). Some servers spawn
    # slowly via npx/uvx/etc. on first run while caching the package.
    mcp_init_timeout_seconds: float = 30.0
    # Per-tool-call default timeout (seconds). Individual tools may override
    # via ToolDefinition.timeout_seconds in the registry.
    mcp_call_timeout_seconds: float = 60.0

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parents[2] / ".env"),
        env_prefix="OPS_AGENT_",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if not settings.knowledge_source_specs and not settings.knowledge_source_path:
        default_candidates = [
            Path(r"D:\projects\HostedDashboard\handyman-admin-dashboard"),
            Path(r"D:\项目\HandymanApp-master"),
            Path(r"C:\Users\Tomonkyo\handyman-agent-system"),
        ]
        for candidate in default_candidates:
            if candidate.exists():
                settings.knowledge_source_path = str(candidate)
                break
    return settings
