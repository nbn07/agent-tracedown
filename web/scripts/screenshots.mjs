// Regenerate the README screenshots in web/docs-screenshots/ from a running stack with demo data.
//
//   node web/scripts/build-static.mjs && tracedown up --db demo.db --no-open   # app and API on :7070
//   node web/scripts/screenshots.mjs http://localhost:7070 http://localhost:7070
//
// Light theme, 1440 px wide, like the existing images. Needs Playwright with Chromium.
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const { chromium } = createRequire(import.meta.url)("playwright");
const out = join(dirname(fileURLToPath(import.meta.url)), "..", "docs-screenshots");
const [web = "http://localhost:3000", api = "http://localhost:7070"] = process.argv.slice(2);

const runs = (await (await fetch(`${api}/api/v1/runs?limit=500`)).json()).runs;
const pick = (agent, pred = () => true) => runs.find(r => r.agent_name === agent && pred(r));
// the demo replays one of its failed calendar runs with a fix: show that pair
const replay = pick("CalendarAssistant", r => r.replay_of);
const calendar = runs.find(r => r.trace_id === replay?.replay_of);
const sales = pick("SalesAssistant");
if (!calendar || !replay || !sales) throw new Error("demo data missing: start with `tracedown up` on an empty database");

const shots = [
  ["runs", "/runs"],
  ["run-detail", `/runs/${calendar.trace_id}`],
  ["run-claims", `/runs/${sales.trace_id}`, async p => { await p.getByRole("tab", { name: "Claims" }).click(); }],
  ["network", "/network"],
  ["incidents", "/incidents"],
  ["analytics", "/analytics", null, 1030],
  ["diff", `/diff?a=${calendar.trace_id}&b=${replay.trace_id}`],
];

const browser = await chromium.launch();
for (const [name, path, act, height = 950] of shots) {
  const page = await browser.newPage({ viewport: { width: 1440, height }, colorScheme: "light" });
  await page.goto(web + path, { waitUntil: "networkidle" });
  if (act) { await act(page); await page.waitForLoadState("networkidle"); }
  await page.waitForTimeout(1200); // charts and the step graph animate in
  await page.screenshot({ path: join(out, `${name}.png`) });
  await page.close();
  console.log("wrote", name);
}
await browser.close();
