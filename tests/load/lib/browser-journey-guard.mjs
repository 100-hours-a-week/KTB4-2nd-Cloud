import fs from 'node:fs/promises';
import path from 'node:path';
import { safeText } from './browser-evidence.mjs';

export function verdict(report, uiError = null) {
  const requests = report.applicationRequests || [];
  const http = requests.filter(r => r.status >= 400);
  const transport = requests.filter(r => r.finished === false && !r.canceled);
  const apiPending = requests.filter(r => r.finished === undefined && new URL(r.url).pathname.startsWith('/api/'));
  return { ok: !uiError && !http.length && !transport.length && !apiPending.length,
    uiError: uiError ? safeText(uiError) : null,
    httpErrors: http.length, transportFailures: transport.length, apiPending: apiPending.length,
    screenOnly: requests.length === 0,
    consoleErrors: (report.console || []).filter(x => ['error','exception'].includes(x.type)).length };
}

// A failed screen stays latched even if a later screen passes. No credentials in guard files.
export async function checkpoint({ directory, captureId, round, view, report, uiError, phase }) {
  if (!path.isAbsolute(directory) || !/^[0-9a-f-]{36}$/.test(captureId) ||
      !['login','home','list','detail'].includes(view)) throw new Error('Explicit capture directory, UUID and known view required');
  await fs.mkdir(directory, { recursive:true, mode:0o700 });
  let previous = null;
  try { previous=JSON.parse(await fs.readFile(path.join(directory,'checkpoint.json'),'utf8')); }
  catch (e) { if (e.code !== 'ENOENT') throw e; }
  if (previous && previous.captureId !== captureId) throw new Error('Different capture already owns browser guard');
  const result=verdict(report,uiError);
  const completedViews=new Set(previous?.completedViews || []);
  if (result.ok) completedViews.add(view);
  const row={ version:1, captureId, origin:'https://v1-staging.yeodam-2gether.com',
    updatedAt:new Date().toISOString(), round, view, phase,
    completedViews:[...completedViews], ready:completedViews.size===4,
    failed:Boolean(previous?.failed) || !result.ok, result };
  await fs.appendFile(path.join(directory,'history.jsonl'),JSON.stringify(row)+'\n',{mode:0o600});
  if (row.failed) await fs.writeFile(path.join(directory,'GUARD'),JSON.stringify(row),{mode:0o600});
  await fs.writeFile(path.join(directory,'checkpoint.tmp'),JSON.stringify(row),{mode:0o600});
  await fs.rename(path.join(directory,'checkpoint.tmp'),path.join(directory,'checkpoint.json'));
  return row;
}
