import { sanitizeEvents, har, safeUrl, safeText, failureStage } from '../lib/browser-evidence.mjs';

// Invoke in ego-browser nodejs with its documented Page handle, not a separate browser driver.
export async function begin(page) {
  if (new URL(await page.url()).origin !== 'https://v1-staging.yeodam-2gether.com') {
    throw new Error('Exact V1 staging Page required; navigate there before observing');
  }
  await page.cdp('Network.enable');await page.cdp('Runtime.enable');
}

export async function saveRound({ page, directory, round, expected }) {
  const fs = await import('node:fs/promises');
  const path = await import('node:path');
  if (new URL(await page.url()).origin !== 'https://v1-staging.yeodam-2gether.com') {
    throw new Error('Page left the exact staging origin; do not export another site');
  }
  if (!path.isAbsolute(directory) || !/^[a-z0-9-]{1,50}$/.test(round)) throw new Error('Absolute output and safe round label required');
  await fs.mkdir(directory, { recursive: true, mode: 0o700 });
  const events = await page.events();
  const deadline = Date.now()+3000;
  while (Date.now() < deadline && sanitizeEvents(events).requests.some(r =>
    r.url.startsWith('https://v1-staging.yeodam-2gether.com/') && r.finished === undefined)) {
    await page.waitForTimeout(150);
    events.push(...await page.events());
  }
  const report = sanitizeEvents(events);
  report.requests.forEach(r => { r.outcomeStage = failureStage(r); });
  report.round = round;report.capturedAt = new Date().toISOString();report.expected = expected;
  report.url = safeUrl(await page.url());
  report.viewport = await page.evaluate(() => ({ width: innerWidth, height: innerHeight }));
  report.applicationRequests = report.requests.filter(r => r.url.startsWith('https://v1-staging.yeodam-2gether.com/'));
  report.httpErrors = report.applicationRequests.filter(r => r.status >= 400);
  report.transportFailures = report.applicationRequests.filter(r => r.finished === false && !r.canceled);
  report.pending = report.applicationRequests.filter(r => r.finished === undefined);
  const snapshot = await page.snapshot();
  await page.screenshot({ path: path.join(directory, round+'.png') });
  await fs.writeFile(path.join(directory, round+'.txt'), snapshot.split('\n').map(safeText).join('\n'), { mode: 0o600 });
  await fs.writeFile(path.join(directory, round+'.json'), JSON.stringify(report, null, 2), { mode: 0o600 });
  await fs.writeFile(path.join(directory, round+'.har'), JSON.stringify(har(report)), { mode: 0o600 });
  return { round, url: report.url, requests: report.requests.length,
    appRequests: report.applicationRequests.length, httpErrors: report.httpErrors.map(r => ({ url:r.url,status:r.status })),
    transportFailures: report.transportFailures.map(r => ({ url:r.url,error:r.error,blockedReason:r.blockedReason })),
    pending: report.pending.length, consoleErrors: report.console.filter(x => ['error','exception'].includes(x.type)).length };
}
