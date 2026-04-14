# T-UI-01 Reference Alignment (Frontend)

## Goal
Make the React/Vite frontend in `apps/web` visually match the 5 screenshots in `references/` exactly — Chinese copy, contextual sidebar, correct icons, correct composer, correct knowledge/memory/settings layouts.

## Reference map
| File | Page | Notes |
|---|---|---|
| `references/7b34a4116070faeb200f7f266b6ca87.jpg` | Home | minimal sidebar + centered hero + 3 cards + "开始体验 →" pill |
| `references/2481bade51d3e7d707373f6be03a7cf.jpg` | Chat | full sidebar w/ 功能开关 + 最近对话; header "× Knowledge Assistant" + "GLM-5 智谱 AI"; composer 📎 🎤 + ↑; footer "AI 生成内容仅供参考" |
| `references/1867fbe3bfbafa7330059ab816c0b16.jpg` | Knowledge | simplified sidebar [← 返回聊天] + 首页/设置; Ollama toggle card; dashed drop zone; compact file list w/ ⬇️ 👁️ 🗑️ |
| `references/bfe834370c059d0b93de83d7098bc3f.jpg` | Memory | simplified sidebar + 记忆管理; 3 colored stat cards; search + separate button; brain empty state |
| `references/41bdeaeb682756b47d71c863b1a2cc5.jpg` | Settings | simplified sidebar; "设置" h1; tabs 模型选择/API 配置; provider chips 全部/OpenAI/Anthropic/.../阿里云/智谱 AI/Moonshot/Cohere/Mistral; model rows w/ selected = blue border + ✓ |

## Chinese copy glossary (use verbatim)
- Home hero subtitle: `您的智能知识管理与学习助手`
- Home cards: `AI 对话` / `智能聊天，支持记忆与知识库` · `知识库` / `上传文档，RAG 智能检索` · `长期记忆` / `自动提取与存储重要信息`
- Home CTA: `开始体验 →`
- Sidebar nav labels: `首页` / `知识库` / `记忆` / `设置`
- Chat new button: `新建聊天`
- Chat search placeholder: `搜索对话...`
- Chat sidebar feature-switch header: `功能开关`; rows `知识库 RAG` / `长期记忆`
- Chat sidebar recent header: `最近对话 (N)` where N = count
- Chat conversation row meta: `2026/3/8 · N 条消息`
- Chat composer placeholder: `给 Assistant 发送消息...`
- Chat footer: `AI 生成内容仅供参考`
- Simplified-sidebar back button: `← 返回聊天`
- Knowledge h1: `知识库`; subtitle `上传文档以启用 RAG 智能检索功能`; action button `📤 上传文档`
- Knowledge embedding card: title `使用本地 Ollama Embedding`; subtitle `已检测到 nomic-embed-text 模型`
- Knowledge drop zone: `拖拽文件到此处，或点击上传` + sub `支持 PDF、Word、TXT、Markdown 格式`
- Knowledge file status: `已就绪`
- Memory h1: `记忆管理`; subtitle `管理自动提取的记忆，控制哪些内容会被记住`; buttons `⚙️ 提取设置` / `+ 添加记忆`
- Memory stats: `自动提取` (value `关闭`), `白名单主题`, `黑名单主题`
- Memory search placeholder: `搜索记忆...`; button `搜索`
- Memory empty: `暂无记忆` + `开启记忆功能后，AI 会自动从对话中提取重要信息`
- Settings h1: `设置`; tabs `模型选择` / `API 配置`
- Settings card header: `选择模型` + `选择适合您需求的 AI 模型`
- Settings provider chips (order): `全部` · `OpenAI` · `Anthropic` · `Google AI` · `DeepSeek` · `阿里云` · `智谱 AI` · `Moonshot` · `Cohere` · `Mistral`

## Structural changes required

### 1. `apps/web/src/components/layout/AppShell.tsx` — split into three sidebar variants
Currently renders ONE sidebar on every route. Must branch on `useLocation().pathname`:

- **`/home`** → `sidebar-variant--minimal`: top 开始聊天 button + nav (首页/知识库/记忆/设置). **No** search, **no** 功能开关, **no** recent list. Account avatar at bottom-left (just "N" circle, no name/role).
- **`/chat`** → `sidebar-variant--chat`: 新建聊天 + 搜索对话 + nav + 功能开关 section + 最近对话 (N) list + account.
- **`/knowledge`, `/memory`, `/settings`** → `sidebar-variant--compact`: top `← 返回聊天` pill button (navigates to `/chat`) + nav only (首页 + current page's siblings). Account avatar at bottom.

Rename nav labels to Chinese. Route `/home` label = `首页`. Add `/chat` implicit — it's triggered via 新建聊天 button, not in nav.

### 2. `apps/web/src/pages/home/HomePage.tsx`
- Remove the pre-header `<p>Knowledge Assistant</p>` — keep only the big `<h1>Knowledge Assistant</h1>` (English, per reference).
- Subtitle below h1: `您的智能知识管理与学习助手`.
- Replace `{card.title.slice(0, 1)}` letter icons with inline SVG icons:
  - AI 对话 → chat bubble SVG
  - 知识库 → open book SVG
  - 长期记忆 → brain SVG
- Card titles in Chinese per glossary.
- Single centered pill button `开始体验 →` (black bg, white text, rounded-full) → `navigate('/chat')`.

### 3. `apps/web/src/pages/chat/ChatPage.tsx`
- Header: left `× Knowledge Assistant` (× button closes/returns to /home), right dropdown `GLM-5 智谱 AI ▼`.
- Remove the existing separate task-title h1 — the chat header is just the close × + brand + model pill.
- Footer strip below composer: centered muted `AI 生成内容仅供参考`.

### 4. `apps/web/src/components/chat/MessageList.tsx`
- User row: right-aligned bubble + circular "U" avatar on right.
- Assistant row: left-aligned bubble + small "AI" avatar on left.
- Remove "You" / "Assistant" text labels — just avatar + bubble.
- Empty-state heading in English `Knowledge Assistant` OK, but subtitle copy in Chinese: `你的智能知识管理与学习助手`.

### 5. `apps/web/src/components/chat/ChatInput.tsx`
- Replace text `+` with 📎 paperclip SVG (inside icon-button).
- Add 🎤 microphone SVG icon-button next to the paperclip.
- Placeholder: `给 Assistant 发送消息...`.
- Replace text `Send` button with circular ↑ up-arrow button (48px, black bg).

### 6. `apps/web/src/pages/knowledge/KnowledgePage.tsx`
- Remove pre-header `Knowledge` label.
- h1 = `知识库`, subtitle = `上传文档以启用 RAG 智能检索功能`.
- Header right: `📤 上传文档` pill button.
- Add Ollama embedding status card (green dot + title + subtitle + toggle switch on right, ON state).

### 7. `apps/web/src/components/knowledge/KnowledgeUploadPanel.tsx`
- Replace the 4-button layout (Choose files/folder/zip/Local path) with a single dashed-border drop zone:
  - Center upload SVG icon
  - Heading `拖拽文件到此处，或点击上传`
  - Sub `支持 PDF、Word、TXT、Markdown 格式`
  - Click = open file picker (accept: `.pdf,.doc,.docx,.txt,.md`)

### 8. `apps/web/src/components/knowledge/KnowledgeSourceList.tsx`
- Collapse the two-section table layout into a single compact file list:
  - Each row: 📄 icon · filename (bold) · meta line `YYYY/M/D · ✓ 已就绪` · right-side icon triad ⬇️ 👁️ 🗑️
- Remove the "Sources" vs "Documents" split.

### 9. `apps/web/src/pages/memory/MemoryPage.tsx`
- h1 = `记忆管理`, subtitle = `管理自动提取的记忆，控制哪些内容会被记住`.
- Header right: `⚙️ 提取设置` (secondary, white bg + border) + `+ 添加记忆` (primary, black bg, white text).

### 10. `apps/web/src/components/memory/MemoryPanel.tsx`
- Three stat cards with **colored circular icons** on left side:
  - `自动提取` — purple brain icon, value `关闭` (or `开启`)
  - `白名单主题` — green target/check icon, value = count
  - `黑名单主题` — red funnel icon, value = count
- Search row: input with 🔍 + placeholder `搜索记忆...` + **separate** `搜索` button on the right (not combined).
- Empty state: centered — brain SVG icon + `暂无记忆` + `开启记忆功能后，AI 会自动从对话中提取重要信息`. No "Add memory" button inside empty state (it lives in page header).

### 11. `apps/web/src/pages/settings/SettingsPage.tsx`
- h1 = `设置`. No pre-header.

### 12. `apps/web/src/components/settings/ModelSelector.tsx`
- Tab labels: `模型选择` / `API 配置`.
- Replace the `M` letter icon with a shopping-bag SVG (24px, rounded gray square bg).
- Card header: title `选择模型` + subtitle `选择适合您需求的 AI 模型`.
- Provider chips row — **each provider is its own chip**, no grouping:
  `全部` · `OpenAI` · `Anthropic` · `Google AI` · `DeepSeek` · `阿里云` · `智谱 AI` · `Moonshot` · `Cohere` · `Mistral`
  - Active chip = solid black bg + white text. Inactive = white bg + light border + dark text.
- Model rows: selected state = 2px blue border (`#2563eb`) + ✓ on right side. Unselected = light gray border, no check.

## CSS updates (`apps/web/src/styles.css` or equivalent)
- Add `.sidebar-variant--minimal`, `.sidebar-variant--chat`, `.sidebar-variant--compact` variants.
- `.back-to-chat` pill button style (white bg, 1px border, rounded, left arrow).
- `.upload-dropzone` dashed-border style (2px dashed `#d4d4d8`, rounded-lg, centered content, hover → darker border).
- `.memory-stat-card` with `--icon-bg` CSS variable for colored circle.
- `.provider-chip` + `.provider-chip.active` (solid black).
- `.model-row-card.selected` → blue border + check mark.
- `.icon-button` circular (36px) for composer attach/mic; `.send-button` circular (48px, black).
- Footer strip: `.chat-footer-hint` muted center text.
- Keep minimal visual language: white bg, black text, light gray borders, no gradients.

## Acceptance criteria
1. `npm --prefix apps/web run build` passes without TS errors.
2. Every label/placeholder listed in the glossary appears verbatim somewhere in the rendered DOM (Chinese matches reference).
3. `/home`, `/chat`, `/knowledge`, `/memory`, `/settings` each render the correct sidebar variant.
4. Chat composer renders 3 controls: paperclip, mic, circular up-arrow send.
5. Knowledge page renders the dashed drop zone (no 4-button grid).
6. Memory page renders 3 stat cards with colored icons + separate 搜索 button.
7. Settings page provider chip row contains all 10 chips including `阿里云` AND `智谱 AI` as separate chips.
8. No decorative gradients, no purple/blue hero backgrounds — only white/black/gray + the 3 accent icon colors in memory stats + the 1 blue selected border in settings.

## Out of scope (don't touch)
- Backend APIs (knowledge import, memory CRUD) — still scaffolded, will land in T-026.
- Auth/login page visual.
- RBAC permission matrix wiring.
- Any changes under `apps/backend`.

## Files to edit (concrete list)
- `apps/web/src/components/layout/AppShell.tsx`
- `apps/web/src/pages/home/HomePage.tsx`
- `apps/web/src/pages/chat/ChatPage.tsx`
- `apps/web/src/pages/knowledge/KnowledgePage.tsx`
- `apps/web/src/pages/memory/MemoryPage.tsx`
- `apps/web/src/pages/settings/SettingsPage.tsx`
- `apps/web/src/components/chat/MessageList.tsx`
- `apps/web/src/components/chat/ChatInput.tsx`
- `apps/web/src/components/knowledge/KnowledgeUploadPanel.tsx`
- `apps/web/src/components/knowledge/KnowledgeSourceList.tsx`
- `apps/web/src/components/memory/MemoryPanel.tsx`
- `apps/web/src/components/settings/ModelSelector.tsx`
- `apps/web/src/styles.css` (or the main CSS file — grep for existing classnames like `workbench-sidebar`)

## Workflow (for the executor, i.e. Codex)
1. Read each file in the concrete list before editing.
2. Apply edits page-by-page in the order above.
3. After all edits: `npm --prefix apps/web run build` and fix any type errors.
4. Report: per-file diff summary + build result.
