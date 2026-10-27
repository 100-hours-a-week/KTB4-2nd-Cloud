import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { verdict, checkpoint } from '../lib/browser-journey-guard.mjs';

test('normal user 429 and unfinished API fail, canceled prefetch and pending font remain distinct',()=>{
  const r=x=>({applicationRequests:[x],console:[]});
  assert.equal(verdict(r({status:429,url:'https://a.test/api/trips',finished:true})).ok,false);
  assert.equal(verdict(r({url:'https://a.test/api/trips'})).ok,false);
  assert.equal(verdict(r({url:'https://a.test/font.woff2'})).ok,true);
  assert.equal(verdict(r({url:'https://a.test/trips',finished:false,canceled:true})).ok,true);
  assert.equal(verdict(r({status:200,finished:true}),'Expected map did not render').ok,false);
});
test('baseline must cover all four views and first failure remains latched',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'browser-guard447-'));
  try {
    const args={directory:dir,captureId:'b9507e84-5b21-4b5f-b8d0-151b0124f56c',phase:'baseline',report:{applicationRequests:[],console:[]}};
    let row;
    for(const view of ['login','home','list'])row=await checkpoint({...args,view,round:view});
    assert.equal(row.ready,false);
    row=await checkpoint({...args,view:'detail',round:'detail'});assert.equal(row.ready,true);
    row=await checkpoint({...args,view:'home',round:'bad-map',uiError:'Map missing'});assert.equal(row.failed,true);
    row=await checkpoint({...args,view:'home',round:'recovery'});assert.equal(row.failed,true);
    assert.ok(await fs.stat(path.join(dir,'GUARD')));
  } finally {await fs.rm(dir,{recursive:true,force:true});}
});
