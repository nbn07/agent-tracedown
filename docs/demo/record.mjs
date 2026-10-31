// Records docs/demo/demo.html into docs/demo.gif (README) and docs/demo/demo.mp4 (social posts).
//
//   node docs/demo/record.mjs                 # full render
//   node docs/demo/record.mjs --stills 3.6,9  # just these moments, as PNGs, to check a layout
//
// Needs Playwright with Chromium and ffmpeg.
import { createRequire } from "node:module";
import { spawn, execFileSync } from "node:child_process";
import { mkdirSync, rmSync, renameSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// require() so a globally installed Playwright is found too
const { chromium } = createRequire(import.meta.url)("playwright");
const here = dirname(fileURLToPath(import.meta.url));
const docs = resolve(here, "..");
const work = process.env.TDN_DEMO_WORK || join(here, ".frames");
const FPS = 30, GIF_FPS = 12, GIF_WIDTH = 960, GIF_COLORS = 128;
const args = process.argv.slice(2);
const stills = args[0] === "--stills" ? args[1].split(",").map(Number) : null;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 720 }, deviceScaleFactor: 1 });
await page.goto("file://" + join(here, "demo.html"));
await page.evaluate(() => document.fonts.ready);
await page.waitForFunction(() => [...document.images].every(i => i.complete));
const duration = await page.evaluate(() => window.DURATION);

if (stills) {
  mkdirSync(work, { recursive: true });
  for (const t of stills) {
    await page.evaluate(t => window.render(t), t);
    await page.screenshot({ path: join(work, `still-${t}.png`) });
  }
  console.log(`stills in ${work}`);
  await browser.close();
  process.exit(0);
}

rmSync(work, { recursive: true, force: true });
mkdirSync(work, { recursive: true });

// The video: frames piped straight into one ffmpeg encode.
const tmp = join(work, "demo.partial.mp4");
const ff = spawn("ffmpeg", ["-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", String(FPS), "-c:v", "png", "-i", "-",
  "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", tmp],
  { stdio: ["pipe", "inherit", "inherit"] });
const total = Math.round(duration * FPS);
for (let f = 0; f < total; f++) {
  await page.evaluate(t => window.render(t), f / FPS);
  const png = await page.screenshot({ type: "png" });
  if (!ff.stdin.write(png)) await new Promise(r => ff.stdin.once("drain", r));
}
ff.stdin.end();
await new Promise((res, rej) => ff.on("close", c => (c === 0 ? res() : rej(new Error(`ffmpeg exited ${c}`)))));
await browser.close();
renameSync(tmp, join(here, "demo.mp4"));

// The GIF, from the video: a palette built from the whole video, no dithering (sharp text, small
// file), and only changed rectangles stored per frame. gifski was tried and dropped: its lossy
// frame reuse leaves faint copies of earlier scenes on the dark background.
execFileSync("ffmpeg", ["-loglevel", "error", "-y", "-i", join(here, "demo.mp4"), "-vf",
  `fps=${GIF_FPS},scale=${GIF_WIDTH}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=${GIF_COLORS}:stats_mode=full[p];` +
  "[b][p]paletteuse=dither=none:diff_mode=rectangle", join(work, "demo.gif")], { stdio: "inherit" });
renameSync(join(work, "demo.gif"), join(docs, "demo.gif"));
rmSync(work, { recursive: true, force: true });
console.log("wrote docs/demo.gif and docs/demo/demo.mp4");
