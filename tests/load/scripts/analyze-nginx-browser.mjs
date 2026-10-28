#!/usr/bin/env node
// Offline, credential-free summary. Never merge screen-only rounds into network success.
import fs from 'node:fs/promises';
import path from 'node:path';
import { failureStage } from '../lib/browser-evidence.mjs';

const directory = process.argv[2];
if (!directory || process.argv.length !== 3) throw new Error('Usage: node analyze-nginx-browser.mjs /absolute/browser-directory');
if (!path.isAbsolute(directory)) throw new Error('Absolute browser evidence directory required');
const rounds = [];
for (const name of (await fs.readdir(directory)).sort()) {
  if (!/^\d{2}-[a-z0-9-]+\.json$/.test(name)) continue;
  const r = JSON.parse(await fs.readFile(path.join(directory,name),'utf8'));
  const requests = r.requests.filter(x => x.url.startsWith('https://v1-staging.yeodam-2gether.com/'));
  const outcomes = {};
  for (const x of requests) {const stage=failureStage(x);outcomes[stage]=(outcomes[stage] || 0)+1;}
  rounds.push({round:r.round,capturedAt:r.capturedAt,expected:r.expected,
    allRequests:r.requests.length,applicationRequests:requests.length,
    screenOnly:requests.length===0,outcomes,
    consoleErrors:r.console.filter(x=>['error','exception'].includes(x.type)).length,
    unmatchedEvents:r.unmatchedEvents,
    requestIds:requests.map(x=>x.requestId).filter(Boolean),
    failures:requests.filter(x=>x.status>=400 || (x.finished===false && !x.canceled))
      .map(x=>({url:x.url,status:x.status ?? null,error:x.error,stage:failureStage(x)}))});
}
if (!rounds.length) throw new Error('No numbered browser evidence rounds');
console.log(JSON.stringify({rounds,scope:'Observation rounds only; controlled browser blocking and pending requests are not server outage evidence'},null,2));
