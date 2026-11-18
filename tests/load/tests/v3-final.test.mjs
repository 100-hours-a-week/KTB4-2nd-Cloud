import test from 'node:test';
import assert from 'node:assert/strict';
import { Session, readLoad, validateStages, preparePhotos } from '../lib/v3-final.mjs';
const token=Buffer.from('{}').toString('base64url')+'.'+Buffer.from(JSON.stringify({sub:'synthetic-user',exp:Date.now()/1000+3600})).toString('base64url')+'.signature';
const cookies=[{name:'accessToken',value:token},{name:'refreshToken',value:'synthetic-refresh'}];
test('arrival budget rejects overrun, nonfinite and excessive concurrency',async()=>{
  for(const stages of [[{rate:81,seconds:1}],[{rate:1,seconds:181}],[{rate:80,seconds:180},{rate:80,seconds:180}],[]])assert.throws(()=>validateStages(stages));
  assert.equal(validateStages([{rate:10,seconds:2}]),20);
  await assert.rejects(readLoad(new Session(cookies),73,[{rate:1,seconds:1}],{concurrency:65}));
});
test('slow responses produce visible generator drops; only valid business replies form latency sample',async()=>{
  let n=0;const session=new Session(cookies,async url=>{const isTrip=url.endsWith('/73');if(isTrip&&n++>0)await new Promise(r=>setTimeout(r,150));return new Response(JSON.stringify({data:{tripId:73}}),{status:200});});
  const r=await readLoad(session,73,[{rate:20,seconds:1}],{concurrency:1});const s=r.stages[0];
  assert(s.droppedBusy>0);assert.equal(s.offered,s.emitted+s.droppedBusy+s.droppedLate);assert(s.validGoodputRps<20);assert(s.p95Ms>=100);
});
test('an expired session stops measured traffic without mixing refresh or login into HTTP results',async()=>{
  let n=0;const session=new Session(cookies,async url=>new Response(JSON.stringify({data:{tripId:73}}),{status:url.endsWith('/73')&&n++>0?401:200}));
  const r=await readLoad(session,73,[{rate:5,seconds:1},{rate:5,seconds:1}]);assert(r.aborted);assert.equal(r.stages.length,1);assert.equal(r.refreshesAfter,0);
});
test('unrelated private paths and changed photo sets are blocked before any request/upload',async()=>{
  const s=new Session(cookies,()=>{throw Error('must not send');});await assert.rejects(s.request('/api/internal/run'));await assert.rejects(s.request('/api/trips/../users'));
  await assert.rejects(preparePhotos({files:[]},()=>{throw Error('must not read');}));
});
