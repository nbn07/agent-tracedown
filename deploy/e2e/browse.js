// Drive the web app in Chromium: every page loads, shows real data, no console errors.
const { chromium } = require('playwright');
const fs = require('fs');
const [base, trace, out] = process.argv.slice(2);
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1440, height: 950 } });
  const errors = [];
  p.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  p.on('pageerror', e => errors.push(String(e)));
  const log = [];
  const check = async (path, text, shot) => {
    await p.goto(base + path, { waitUntil: 'networkidle' });
    let ok = true;
    // visible text outside <select> menus (option labels are hidden until opened)
    try { await p.locator('main :not(option):not(select)', { hasText: text }).last().waitFor({ state: 'visible', timeout: 15000 }); } catch { ok = false; }
    await p.screenshot({ path: `${out}/${shot}.png` });
    log.push(`${ok ? 'OK  ' : 'FAIL'} ${path} shows "${text}"`);
  };
  await check('/runs?agent=CalendarAssistant', 'CalendarAssistant', 'runs');
  await check('/runs?project=demo-eu', 'EuBot', 'runs-eu');
  await check(`/runs/${trace}`, 'Bad LLM output', 'run-detail');
  const nodes = await p.locator('.react-flow__node').count();
  log.push(`${nodes > 3 ? 'OK  ' : 'FAIL'} run graph has ${nodes} nodes`);
  await check('/network', 'SupportBot', 'network');
  await check('/incidents', 'Bad LLM output', 'incidents');
  await check('/drift', 'billing@v4', 'drift');
  await check('/analytics', 'Success', 'analytics');
  await p.goto(base + '/live', { waitUntil: 'domcontentloaded' });
  await p.waitForTimeout(3000);
  await p.screenshot({ path: `${out}/live.png` });
  log.push('OK   /live opened');
  log.push(errors.length ? `FAIL console errors: ${errors.slice(0, 5).join(' | ')}` : 'OK   no console errors');
  fs.writeFileSync(`${out}/browse.log`, log.join('\n') + '\n');
  await b.close();
})();
