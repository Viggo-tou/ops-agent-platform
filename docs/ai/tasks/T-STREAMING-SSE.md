# T-STREAMING-SSE — 通过 SSE 流式返回 pipeline 进度 + LLM token

**Status:** 待开（依赖 T-KB-LLM-SYNTHESIS 先落地）
**Priority:** P1
**Created:** 2026-04-22

## 问题

用户前端测试反馈"响应很慢"。目前前端提交任务后要等整条 pipeline 跑完（translation → plan → review → knowledge → 模板拼接）才返回一次性 JSON。以 knowledge Q&A 场景为例，中位耗时 6-12s 没有任何可见反馈。

## 目标

在 `POST /api/tasks` 或新增 `/api/tasks/stream` 端点上用 SSE（Server-Sent Events）推送：

1. pipeline 阶段事件（translation_done / plan_done / retrieval_done / synthesis_started / answer_chunk / done）
2. 若 T-KB-LLM-SYNTHESIS 已落地，从 MiniMax 拉取流式 token 并转发。

## 最小实现建议

1. FastAPI 端用 `StreamingResponse` + `text/event-stream` content type。
2. 后端从 orchestrator 的 `emit_event` 钩子转发阶段事件。
3. 前端 `EventSource` 订阅，渐进渲染。
4. MiniMax chatcompletion_v2 支持 `stream: true`（已有 flag 在别处用过——确认后复用）。

## 验收

- 用户提问到**第一个字符出现**延迟 < 2s（现在 > 6s）。
- 全部阶段事件都能到前端并打到 console（调试期）。
- 网络断开时前端自动回落到 non-stream 轮询一次。

## 依赖

- T-KB-LLM-SYNTHESIS（需要 answer_chunk 事件，模板答案没有 token 流）

## 不做

- WebSocket（SSE 够用，成本低）
- 多路复用 / 并发任务订阅（下一迭代）
