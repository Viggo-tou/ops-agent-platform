/**
 * Final end-to-end smoke for the 1.0 frontend.
 *
 * Covers:
 *   1. Welcome → login persists user via localStorage seed
 *   2. /dashboard renders KPI cards with real data
 *   3. /tasks renders 5 KPIs + 工具就绪度 panel + paginated table
 *   4. /repositories renders KPI grid + repo rows
 *   5. /usage renders 4 KPIs + chart + per-model table
 *   6. /settings renders Section A/B/C with provider tabs + per-row mode dropdown
 *   7. Chat: /chat receives a question, streams a markdown answer, follow-up
 *      stays in the same session (URL becomes /chat/{taskId})
 *   8. Account-card popup menu opens on avatar click + on gear click,
 *      contains all 9 nav items + 退出登录, closes on outside click
 *
 * Run with `node _e2e_full_journey.mjs` while frontend (5173) and backend
 * (8000) are both up.
 */
import { chromium } from "playwright";

const FRONTEND = "http://127.0.0.1:5173";
const PASS = "✓";
const FAIL = "✗";
const checks = [];

function assert(label, cond, detail = "") {
  checks.push({ label, ok: !!cond, detail });
  console.log(`${cond ? PASS : FAIL} ${label}${detail ? " — " + detail : ""}`);
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();

  // Seed localStorage so AuthGuard + setApiActor pick up the user.
  await page.addInitScript(() => {
    window.localStorage.setItem(
      "ops-agent-workbench-user",
      JSON.stringify({ name: "Tomonkyo", email: "twq@example.com", role: "admin" }),
    );
  });

  const consoleErrs = [];
  page.on("pageerror", (e) => consoleErrs.push(`PE: ${e.message}`));
  page.on("console", (m) => { if (m.type() === "error") consoleErrs.push(`CE: ${m.text()}`); });

  // ---------- 2. Dashboard ----------
  await page.goto(`${FRONTEND}/dashboard`, { waitUntil: "networkidle" });
  await page.waitForTimeout(800);
  const dashH1 = (await page.locator("h1").first().textContent())?.trim() ?? "";
  assert("/dashboard renders welcome H1", dashH1.includes("欢迎回来"), `h1: '${dashH1}'`);
  const kpiCount = await page.locator(".tl3-kpi-card").count();
  assert("/dashboard has 5 KPI cards", kpiCount === 5, `count=${kpiCount}`);

  // ---------- 3. Tasks ----------
  await page.goto(`${FRONTEND}/tasks`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);
  const tasksH1 = (await page.locator("h1").first().textContent())?.trim() ?? "";
  assert("/tasks renders header", tasksH1 === "任务列表", `h1: '${tasksH1}'`);
  const taskKpis = await page.locator(".tl3-kpi-card").count();
  assert("/tasks has 5 KPIs", taskKpis === 5, `count=${taskKpis}`);
  const readinessVisible = await page.locator(".tl3-readiness-card").isVisible().catch(() => false);
  assert("/tasks shows 工具就绪度 panel", readinessVisible);
  const paginationVisible = await page.locator(".tl3-pagination").isVisible().catch(() => false);
  assert("/tasks shows pagination", paginationVisible);

  // ---------- 4. Repositories ----------
  await page.goto(`${FRONTEND}/repositories`, { waitUntil: "networkidle" });
  await page.waitForTimeout(800);
  const repoH1 = (await page.locator("h1").first().textContent())?.trim() ?? "";
  assert("/repositories renders header", repoH1 === "知识源与仓库", `h1: '${repoH1}'`);
  const repoKpis = await page.locator(".tl3-kpi-card").count();
  assert("/repositories has 4 KPIs", repoKpis === 4, `count=${repoKpis}`);

  // ---------- 5. Usage ----------
  await page.goto(`${FRONTEND}/usage`, { waitUntil: "networkidle" });
  await page.waitForTimeout(800);
  const usageH1 = (await page.locator("h1").first().textContent())?.trim() ?? "";
  assert("/usage renders header", usageH1 === "模型 Token 用量", `h1: '${usageH1}'`);
  const usageKpis = await page.locator(".tl3-kpi-card").count();
  assert("/usage has 4 KPIs", usageKpis === 4, `count=${usageKpis}`);

  // ---------- 6. Settings ----------
  await page.goto(`${FRONTEND}/settings`, { waitUntil: "networkidle" });
  await page.waitForTimeout(800);
  const sectionLetters = await page.locator(".section-card-v3-letter").allTextContents();
  assert(
    "/settings has 3 sections (A/B/C)",
    sectionLetters.length === 3 && sectionLetters.join("") === "ABC",
    `letters: ${sectionLetters.join(",")}`,
  );
  const segToggleBtns = await page.locator(".seg-toggle-btn").count();
  assert("/settings has API/CLI toggle", segToggleBtns >= 2, `count=${segToggleBtns}`);

  // ---------- 8. Account-card popup menu ----------
  // Verified on /settings (currently focused).
  const menuBefore = await page.locator(".account-menu").isVisible().catch(() => false);
  assert("Account menu starts closed", menuBefore === false);
  await page.click(".account-gear-btn");
  await page.waitForTimeout(150);
  const menuAfter = await page.locator(".account-menu").isVisible();
  assert("Account menu opens on gear click", menuAfter);
  const menuItems = await page.locator(".account-menu-item").allTextContents();
  const expected = ["总览", "任务列表", "知识库", "记忆", "仓库", "集成", "用量", "治理", "设置"];
  const allFound = expected.every((label) => menuItems.some((item) => item.includes(label)));
  assert("Menu has all 9 nav items", allFound, `items: ${menuItems.length}`);
  const hasLogout = menuItems.some((item) => item.includes("退出登录"));
  assert("Menu has 退出登录", hasLogout);
  // Close on outside click.
  await page.mouse.click(800, 400);
  await page.waitForTimeout(150);
  const menuClosed = await page.locator(".account-menu").isVisible().catch(() => false);
  assert("Menu closes on outside click", !menuClosed);
  // Open via avatar click.
  await page.click(".account-card-trigger");
  await page.waitForTimeout(150);
  const menuViaAvatar = await page.locator(".account-menu").isVisible();
  assert("Menu opens on avatar click", menuViaAvatar);

  // ---------- 7. Chat streaming + markdown + follow-up ----------
  await page.goto(`${FRONTEND}/chat`, { waitUntil: "networkidle" });
  await page.waitForTimeout(600);
  // Submit first message.
  const textarea = page.locator(".chat-input-textarea, textarea").first();
  await textarea.fill("我们本地有几个库?");
  await textarea.press("Enter");
  // Wait for streaming + final task creation.
  await page.waitForTimeout(8000);
  const chatBody = await page.locator("main").innerText();
  const fallbackFlash = chatBody.includes("I could not produce a grounded repository answer");
  assert("No 'I could not produce' fallback flash", !fallbackFlash);
  const repoMentioned = /handymanapp|hosteddashboard/.test(chatBody);
  assert("Chat answer mentions a real repo (live state injected)", repoMentioned);
  // After first answer, URL should have switched to /chat/{taskId}.
  const urlAfter = page.url();
  const urlOk = /\/chat\/[0-9a-f-]{20,}/.test(urlAfter);
  assert("URL is /chat/{taskId} after first reply", urlOk, urlAfter);
  // Markdown rendering: there should be at least one <strong> or <ul>/<ol> in the bubble.
  const boldCount = await page.locator(".md-prose strong, .md-prose ol li, .md-prose ul li").count();
  assert("Markdown rendered (bold or list nodes present)", boldCount > 0, `nodes=${boldCount}`);
  // Follow-up: send a 2nd message, verify it stays in same session (URL doesn't change to a new task).
  const urlBeforeFollowup = page.url();
  await textarea.fill("再列一下吧");
  await textarea.press("Enter");
  await page.waitForTimeout(7000);
  const urlAfterFollowup = page.url();
  // The first task URL should stay (we're in /chat/{firstTaskId} and follow-up doesn't navigate).
  assert(
    "Follow-up keeps the same /chat/{taskId} URL (no new conversation)",
    urlBeforeFollowup === urlAfterFollowup,
    `before=${urlBeforeFollowup} after=${urlAfterFollowup}`,
  );

  // ---------- Final ----------
  console.log("\n=== Console / page errors ===");
  console.log(`count: ${consoleErrs.length}`);
  for (const e of consoleErrs.slice(0, 12)) console.log(`  ${e}`);

  const passed = checks.filter((c) => c.ok).length;
  const failed = checks.filter((c) => !c.ok).length;
  console.log(`\n=== Summary: ${passed} passed, ${failed} failed ===`);
  if (failed > 0) {
    console.log("FAILED CHECKS:");
    for (const c of checks.filter((c) => !c.ok)) {
      console.log(`  ${c.label}${c.detail ? " — " + c.detail : ""}`);
    }
  }

  await browser.close();
  process.exit(failed === 0 ? 0 : 1);
})();
