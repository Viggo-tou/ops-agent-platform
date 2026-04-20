# Spec: DiffViewer Syntax Highlighting (T-DIFFHL)

## Goal

Upgrade `apps/web/src/components/chat/DiffViewer.tsx` so that code inside each
hunk is rendered with **syntax highlighting** driven by the file extension.
Keep the existing add/remove/context row colors intact; syntax highlighting
layers on top of them as token-level colors inside the `diff-line-content`
span.

**Frontend only. Pure visual upgrade.** No backend touches, no API change, no
new panels.

Use **highlight.js** (not shiki) — smaller bundle, stable, good enough. User
decision documented in session.

## Files to touch

1. `apps/web/package.json`
   - Add runtime dep: `highlight.js` (latest 11.x).
   - Do **not** add the CSS theme as a dep — we'll import one theme directly
     (see below). Do not add `@types/highlight.js` — highlight.js 11 ships its
     own types.

2. `apps/web/src/components/chat/DiffViewer.tsx` *(modified)*
   - Keep the existing parser (`parseUnifiedDiff`, `countDiffFiles`,
     `DiffFile` / `DiffHunk` / `DiffLine` types) **unchanged**.
   - Add highlighting in the render path:
     - Pull `import hljs from "highlight.js/lib/core"` and register **only
       these languages** (to keep bundle small):
       - javascript (`js`)
       - typescript (`typescript`)
       - jsx (falls under javascript with `xml` language registered? — easier:
         use `javascript` for `.jsx` too)
       - python
       - json
       - css
       - bash
       - xml (covers html)
     - Register each with `hljs.registerLanguage(name, langModule)` using the
       `highlight.js/lib/languages/<name>` import paths.
     - Map file extension to language id via a small pure function
       `detectLanguage(path: string): string | null`:
       - `.ts`/`.tsx` → `typescript`
       - `.js`/`.jsx`/`.mjs`/`.cjs` → `javascript`
       - `.py` → `python`
       - `.json` → `json`
       - `.css`/`.scss`/`.less` → `css`
       - `.sh`/`.bash` → `bash`
       - `.html`/`.xml`/`.vue`/`.svg` → `xml`
       - anything else → `null` (render as-is)
     - Per `DiffFile`, resolve language once (based on `file.newPath` falling
       back to `file.oldPath` and then `file.path`). Memoize with `useMemo`
       keyed on `diff` input.
     - Per line, when rendering `line.content`, if `language !== null` and
       `line.kind` is `"add" | "remove" | "context"`, call
       `hljs.highlight(line.content, { language, ignoreIllegals: true })`
       and render the result via `dangerouslySetInnerHTML={{ __html:
       result.value }}`. Otherwise render as plain text (existing behavior).
     - The `+`/`-` gutter prefix (`line.kind === "add" ? "+" : ...`) is **not**
       highlighted — keep it outside the highlighted span so it isn't parsed
       as operator.
     - Fallback: if `hljs.highlight` throws (it shouldn't with
       `ignoreIllegals`), catch and render as plain text.

3. `apps/web/src/components/chat/DiffViewer.tsx` (top of file)
   - Import one highlight.js theme CSS once:
     `import "highlight.js/styles/github.css";`
     (This minimizes bundle; the GitHub light theme fits the existing
     black-on-white UI.)

4. `apps/web/src/components/chat/DiffViewer.test.tsx` *(new)*
   - Use the existing vitest + testing-library setup (check `package.json`
     and any existing `.test.tsx` file for the convention).
   - Test: `detectLanguage("src/foo.ts")` returns `"typescript"`;
     `detectLanguage("foo.py")` returns `"python"`;
     `detectLanguage("bar.unknown")` returns `null`.
     (Export `detectLanguage` alongside `DiffViewer` for test access.)
   - Test: rendering a diff for a `.ts` file produces `<span class="hljs-...">`
     elements inside `.diff-line-content` (search via
     `container.querySelector(".hljs-keyword")` or equivalent — assert at
     least one hljs token class exists).
   - Test: rendering a diff for an unknown extension (e.g. `.bin`) produces
     no `.hljs-*` spans (plain text, existing behavior).
   - Test: the `+` / `-` gutter characters are still present as raw text
     (regex on `.diff-line-add .diff-line-content` should contain the literal
     code, not have its `+` prefix consumed by highlighting).

5. `apps/web/src/components/chat/DiffViewer.css` **or** the current diff-viewer
   styles — find them first. Add minimal overrides so highlighted tokens don't
   fight the row background:
   - `.diff-line-add .hljs-comment, .diff-line-remove .hljs-comment { color:
     inherit; opacity: 0.7; }` *(or similar — keep it minimal; let github.css
     drive the rest)*.
   - If the existing `.diff-line-content` CSS sets `color: ...`, wrap syntax
     tokens with sufficient specificity to let github.css colors win on
     `.diff-line-context .hljs-*` but stay readable on `.diff-line-add` /
     `.diff-line-remove` (green / red row backgrounds).

## Performance

- Parse once, highlight on render. For very large diffs (>500 lines),
  highlighting per line is cheap with hljs but still O(N) string work.
  Memoize highlighted HTML per `(language, content)` pair using `useMemo`
  over the whole `files` array rather than per line (no per-line memo — too
  granular).
- Do **not** add a web-worker or dynamic import. Keep it sync.

## Bundle size check

- After install, run `pnpm --filter web build` (or whatever build cmd is) and
  confirm bundle size increase is under **~80 KB gzipped**. Only 8 language
  modules + github.css theme should land in the bundle. If it's higher,
  investigate whether we accidentally pulled the full hljs core.

## Non-requirements (do NOT implement)

- No DiffViewer parser changes.
- No theme switcher / dark mode.
- No "copy code" button.
- No in-place editing.
- No GateStatusPanel integration — separate task.
- No changes to `TaskDetailPage` or any file outside `apps/web/src/components/chat/`
  and `apps/web/package.json`.

## Acceptance criteria

- A TypeScript-file diff renders with token colors (keywords blue-ish,
  strings red-ish per github.css theme), while the row background stays
  green for add lines and red for remove lines.
- The P0 E2E evidence screenshot scenario (P69-8 firebase.js + database.rules.json)
  would now show colored JS tokens.
- All new tests pass: `pnpm --filter web test` (or the equivalent from
  `package.json` scripts).
- `pnpm --filter web build` succeeds.
- No lint errors introduced.
- Bundle delta ≤ ~80 KB gz vs the prior build (document in the finish
  summary).

## Out of scope

- Removing existing DiffViewer row coloring.
- Any GateStatusPanel or TaskDetailPage work (separate task already in flight).
- Refactoring the diff parser.
