// Import through ego-browser nodejs; use the same active TaskSpace for the whole task.
// An authenticated Grafana session is required. Never export browser cookies.
export async function capture({ spaceId, directory, from, to, phase }) {
  if (!['before', 'during', 'after', 'historical'].includes(phase)) throw new Error('Unknown phase');
  if (!Number.isFinite(from) || !Number.isFinite(to) || from >= to) throw new Error('Use absolute epoch milliseconds');
  const fs = await import('node:fs/promises');
  const path = await import('node:path');
  if (!path.isAbsolute(directory)) throw new Error('Use an absolute artifact directory');
  await fs.mkdir(directory, { recursive: true, mode: 0o700 });
  const task = await taskSpace(spaceId);
  const page = task.page('p1');
  const url = `https://grafana-staging.yeodam-2gether.com/d/yeodam-v1-source?from=${from}&to=${to}&timezone=utc&refresh=`;
  await page.goto(url);
  const initialViewport = await page.evaluate(() => ({width:innerWidth,height:innerHeight}));
  await page.mouse.move(Math.round(initialViewport.width*.7),Math.round(initialViewport.height*.7));
  await page.mouse.wheel(0,-10000,{label:'start dashboard capture at top'});
  await page.waitForSelector('text="Nginx client connections"', { timeout: 15000 });
  // Force lazy dashboard panels into view before the final full-page screenshot.
  const dashboard = JSON.parse(await fs.readFile(new URL('../../../monitoring/v2-staging/grafana/dashboards/v1-source.json', import.meta.url), 'utf8'));
  const expected = dashboard.panels.filter(p => p.type !== 'logs').map(p => p.title);
  const covered = new Set();
  const shots = [];
  for (let index = 0; index < 8; index++) {
    await page.evaluate(() => {
      for (const node of document.querySelectorAll('section[data-testid]')) {
        if (node.getAttribute('data-testid').includes('V1 Nginx access logs')) node.style.visibility = 'hidden';
      }
    });
    const snapshot = await page.snapshot();
    if (snapshot.includes('Welcome to Grafana')) throw new Error('Grafana login required');
    const screenshot = path.join(directory, `${phase}-${index}.png`);
    await page.screenshot({ path: screenshot });
    await fs.writeFile(path.join(directory, `${phase}-${index}.txt`), snapshot);
    shots.push(screenshot);
    const visible = await page.evaluate(() => [...document.querySelectorAll('section[data-testid]')].filter(e => {
      const r=e.getBoundingClientRect();return r.top>=140 && r.bottom<=innerHeight && getComputedStyle(e).visibility!=='hidden';
    }).map(e=>e.getAttribute('data-testid').replace('data-testid Panel header ','')));
    visible.forEach(title=>covered.add(title));
    if (expected.every(title=>covered.has(title))) break;
    const viewport = await page.evaluate(() => ({ width: innerWidth, height: innerHeight }));
    await page.mouse.move(Math.round(viewport.width * .7), Math.round(viewport.height * .7));
    await page.mouse.wheel(0, Math.round(viewport.height * .5), { label: 'capture next dashboard section' });
  }
  const metadata = { graphPanelsCovered: [...covered], missingGraphPanels: expected.filter(title=>!covered.has(title)), logPanelHiddenForPrivacy: true, url, from, to, phase, capturedAt: new Date().toISOString(), shots,
    note: 'Grafana UI screenshots; historical recreation is distinct from live capture. Raw archives are authoritative.' };
  await fs.writeFile(path.join(directory, `${phase}-capture.json`), JSON.stringify(metadata, null, 2));
  console.log(metadata);
}
