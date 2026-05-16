---
language: javascript
applies_to:
  - "*.js"
  - "*.jsx"
  - "*.ts"
  - "*.tsx"
  - "src/**/*.js"
  - "src/**/*.jsx"
  - "src/**/*.ts"
  - "src/**/*.tsx"
audience: codegen-llm
priority: high
---

# React hook and session-state discipline

Use this when editing React components or pages.

- Never place `useState`, `useEffect`, `useMemo`, `useCallback`, or any
  custom hook after a conditional `return`.
- If adding an access guard, loading guard, or role guard, declare all hooks
  first, then put the conditional return below the hook declarations.
- Prefer fixing shared user/session cache behavior in the context/provider,
  login flow, or dashboard entry point. Do not edit every page that only reads
  `localStorage.getItem("currentUser")` unless the task explicitly requires
  per-page behavior changes.
- If a context module exports `useUser` or another named hook, import the named
  hook, e.g. `import { useUser } from "../context/UserContext"`. Do not default
  import a context module unless the file actually has `export default`.
- Before using a custom context hook, inspect its exported return shape. If
  `useUser()` returns the context object, destructure it consistently, e.g.
  `const { currentUser } = useUser()`.
- Do not clear `currentUser` on ordinary page unmount. That logs the user out
  during navigation and usually worsens cache bugs.
- Do not add a context action such as `logout` unless at least one component
  calls it. For stale-user bugs, prefer a wired provider/login/dashboard fix
  over orphan context APIs.
- When simplifying roles, keep stored role values, option values, filters, and
  access checks consistent. If existing data uses lowercase roles, compare with
  normalized lowercase values or migrate the stored literals in the same patch.
- Include common legacy role spellings in the migration path, not only the new
  display labels. For admin dashboards, `admin`, `Admin`, `master admin`,
  `Master Admin`, `master_admin`, `administrator`, `staff`, `Staff`,
  `staff member`, `Staff Member`, and `staff_member` should normalize to the
  canonical role values the task requests.
- If a task fixes stale logged-in user/cache behavior, use one authoritative
  session source per surface. Do not leave sibling dashboard pages split between
  context state and direct `localStorage` reads unless the existing architecture
  already documents that split.
- Do not normalize arbitrary display names into roles. Only transform values
  that are clearly role fields or role option constants.
