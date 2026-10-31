// Build the web app as static files and copy them into the Python server package, so
// `pip install agent-tracedown-server` and `tracedown up` include the full UI with no Node.
//
//   cd web && npm ci && node scripts/build-static.mjs
//
// Output: server/tracedown_server/web/ (not committed; the release workflow builds it).
import { execFileSync } from "node:child_process";
import { cpSync, existsSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const web = join(dirname(fileURLToPath(import.meta.url)), "..");
const dest = join(web, "..", "server", "tracedown_server", "web");
rmSync(join(web, "out"), { recursive: true, force: true });
execFileSync("npx", ["next", "build"], {
  cwd: web, stdio: "inherit",
  env: { ...process.env, TDN_STATIC_EXPORT: "1", NEXT_TELEMETRY_DISABLED: "1" },
});
if (!existsSync(join(web, "out", "index.html"))) throw new Error("export produced no index.html");
rmSync(dest, { recursive: true, force: true });
cpSync(join(web, "out"), dest, { recursive: true });
console.log(`web app copied to ${dest}`);
