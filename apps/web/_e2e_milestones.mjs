/**
 * E2E: send "完成 P69-21" in chat, watch milestones populate over time.
 *
 * Verifies the fix from commit 0a57c1b — chat status feed should show
 * intermediate pipeline milestones (planner / knowledge / codegen /
 * compile / repair / review) as the task runs in the background, not
 * just the terminal status.
 *
 * Total run time: up to 6 minutes (we just need to see the FIRST
 * couple of milestones — planner_done is fast, knowledge_retrieved
 * comes after a few minutes).
 */
import { chromium } from "playwright";

const FRONTEND = "http://127.0.0.1:5173";
const POLL_INTERVAL_MS = 2_000;
const MAX_WAIT_MS = 6 * 60 * 1000;

(async () => {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();

  await page.addInitScript(() => {
    window.localStorage.setItem(
      "ops-agent-workbench-user",
      JSON.stringify({ name: "Tomonkyo", email: "x@x.com", role: "admin" }),
    );
  });

  const errs = [];
  page.on("pageerror", (e) => { console.log("PE:", e.message); errs.push(`PE: ${e.message}`); });
  page.on("console", (m) => { if (m.type() === "error") { console.log("CE:", m.text()); errs.push(`CE: ${m.text()}`); } });

  await page.goto(`${FRONTEND}/chat`, { waitUntil: "networkidle" });
  await page.waitForTimeout(2000);
  console.log(`url before submit: ${page.url()}`);
  // Page debug — what does the body actually contain?
  const bodySnap = await page.locator("body").innerText();
  console.log(`body sample (first 200 chars): ${bodySnap.slice(0, 200).replace(/\n/g, " | ")}`);
  const inputCount = await page.locator("textarea").count();
  console.log(`textarea count on page: ${inputCount}`);

  // Use the explicit class — the page has multiple inputs (sidebar
  // search + chat composer). The chat composer carries chat-input-textarea.
  const textarea = page.locator(".chat-input-textarea").first();
  await textarea.waitFor({ state: "visible", timeout: 10000 });
  await textarea.fill("完成 P69-21");
  await page.waitForTimeout(300);
  // Track network for debug
  page.on("request", (req) => {
    if (req.url().includes("/api/chat/send") || req.url().includes("/api/tasks?session")) {
      console.log(`  REQ ${req.method()} ${req.url()}`);
    }
  });
  page.on("response", async (res) => {
    if (res.url().includes("/api/chat/send")) {
      console.log(`  RES ${res.status()} ${res.url()}`);
    }
  });
  // Click the actual send button rather than relying on Enter behavior.
  const sendBtn = page.locator(".send-button").first();
  await sendBtn.click();
  console.log("send button clicked");
  // Brief wait then dump the chat-scroll content to diagnose
  await page.waitForTimeout(3000);
  const messageContent = await page.locator(".message-content").count();
  console.log(`message-content blocks: ${messageContent}`);
  const taskCreateBlocks = await page.locator(".task-create-status").count();
  console.log(`task-create-status (any): ${taskCreateBlocks}`);
  const chatScrollText = await page.locator(".chat-scroll").innerText().catch(() => "(no chat-scroll)");
  console.log(`chat-scroll content (last 300 chars): ${chatScrollText.slice(-300).replace(/\n/g, " | ")}`);

  // Wait for the green "✓ 任务已创建" status block to confirm task creation.
  console.log("waiting for task_created status block...");
  let taskCreatedSeenAt = null;
  let taskId = null;
  const tStart = Date.now();
  while (Date.now() - tStart < 120_000) {
    const txt = await page.locator(".task-create-status.created").first().textContent().catch(() => null);
    if (txt && /任务已创建/.test(txt)) {
      taskCreatedSeenAt = Date.now() - tStart;
      const url = page.url();
      const m = url.match(/\/chat\/([0-9a-f-]{20,})/);
      if (m) taskId = m[1];
      console.log(`✓ task_created seen at +${(taskCreatedSeenAt / 1000).toFixed(1)}s, url=${url}`);
      break;
    }
    // Periodic debug — what state is the create-status block in?
    if ((Date.now() - tStart) % 10_000 < 600) {
      const pending = await page.locator(".task-create-status.pending").first().textContent().catch(() => null);
      const failed = await page.locator(".task-create-status.failed").first().textContent().catch(() => null);
      const url = page.url();
      console.log(`  +${((Date.now() - tStart) / 1000).toFixed(0)}s  url=${url}  pending=${!!pending}  failed=${!!failed}`);
    }
    await page.waitForTimeout(500);
  }
  if (!taskCreatedSeenAt) {
    console.log("✗ task_created bubble never appeared in 60s");
    await browser.close();
    process.exit(2);
  }

  // Now poll for milestone pills, recording each unique label as it appears.
  console.log("\nwatching for milestone pills...");
  const seenMilestones = new Map();
  const tMilestoneStart = Date.now();
  const seenStatus = new Map();
  while (Date.now() - tMilestoneStart < MAX_WAIT_MS) {
    const milestoneTexts = await page
      .locator(".status-feed-milestone .status-feed-milestone-label")
      .allTextContents()
      .catch(() => []);
    for (const m of milestoneTexts) {
      const label = m.trim();
      if (!seenMilestones.has(label)) {
        const at = ((Date.now() - tStart) / 1000).toFixed(1);
        seenMilestones.set(label, at);
        console.log(`  +${at}s  ✓ ${label}`);
      }
    }
    // Also watch for a terminal status (would mean we're done).
    const statusTitleEls = await page
      .locator(".status-feed-item .status-feed-title strong")
      .allTextContents()
      .catch(() => []);
    for (const s of statusTitleEls) {
      const label = s.trim();
      if (!seenStatus.has(label)) {
        const at = ((Date.now() - tStart) / 1000).toFixed(1);
        seenStatus.set(label, at);
        console.log(`  +${at}s  ⚐ TERMINAL STATUS: ${label}`);
      }
    }
    if (seenStatus.size > 0) {
      console.log("\nterminal status reached, stopping early");
      break;
    }
    await page.waitForTimeout(POLL_INTERVAL_MS);
  }

  console.log("\n=== Summary ===");
  console.log(`task_id: ${taskId ?? "(missing)"}`);
  console.log(`milestones seen: ${seenMilestones.size}`);
  for (const [label, at] of seenMilestones) {
    console.log(`   +${at}s  ${label}`);
  }
  console.log(`terminal statuses seen: ${seenStatus.size}`);
  for (const [label, at] of seenStatus) {
    console.log(`   +${at}s  ${label}`);
  }
  console.log(`console errors: ${errs.length}`);
  for (const e of errs.slice(0, 8)) console.log(`   ${e}`);

  // Screenshot of the chat-scroll bottom region for visual sanity.
  try {
    await page.screenshot({ path: "_milestone_chat.png", fullPage: false });
    console.log("\nscreenshot saved: _milestone_chat.png");
  } catch { /* noop */ }

  // Pass criteria: at least 1 milestone surfaced, 0 console errors related
  // to our code (the pre-existing 401 hydration race is allowed).
  const ourErrs = errs.filter((e) => !/401/.test(e));
  const passed = seenMilestones.size >= 1 && ourErrs.length === 0;
  console.log(`\nresult: ${passed ? "PASS" : "FAIL"}`);
  await browser.close();
  process.exit(passed ? 0 : 1);
})();
