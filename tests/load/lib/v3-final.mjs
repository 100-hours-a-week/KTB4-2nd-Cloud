import { randomBytes, createHash } from 'node:crypto';
import { performance } from 'node:perf_hooks';

export const BASE = 'https://yeodam-2gether.com';
export const digest = value => createHash('sha256').update(value).digest('hex');
export const traceContext = () => ({traceId:randomBytes(16).toString('hex'), parent:randomBytes(8).toString('hex')});
export const percentile = (values, p) => values.length ? [...values].sort((a,b)=>a-b)[Math.ceil(values.length*p)-1] : null;
export const sleep = ms => new Promise(r=>setTimeout(r,ms));

// Only cookies supplied by the logged-in browser; none are written to evidence.
export class Session {
  constructor(cookies, transport=fetch) {
    this.cookies=new Map(cookies.filter(c=>['accessToken','refreshToken','CSRF_CONTEXT'].includes(c.name)).map(c=>[c.name,c.value]));
    if(!this.cookies.has('accessToken')) throw Error('Authenticated browser session required');
    this.transport=transport;this.refreshes=0;this.refreshing=null;
  }
  async request(path, {method='GET',body,headers={},timeout=10000,refresh=true}={}) {
    if(!/^\/api\/(users\/me|auth\/(csrf|token\/refresh)|trips(?:[/?]|$)|regions(?:[/?]|$))/.test(path)||path.includes('..')) throw Error('Path outside final-validation allowlist');
    const start=performance.now();
    const response=await this.transport(BASE+path,{method,headers:{cookie:[...this.cookies].map(([k,v])=>k+'='+v).join('; '),...(body===undefined?{}:{'content-type':'application/json'}),...headers},body:body===undefined?undefined:JSON.stringify(body),redirect:'error',signal:AbortSignal.timeout(timeout)});
    for(const line of response.headers.getSetCookie?.()||[]) {const pair=line.split(';')[0];const index=pair.indexOf('=');const name=pair.slice(0,index);if(['accessToken','refreshToken','CSRF_CONTEXT'].includes(name))this.cookies.set(name,pair.slice(index+1));}
    const raw=await response.text();if(raw.length>4*1024*1024)throw Error('Response budget exceeded');
    let data;try {data=JSON.parse(raw);} catch {data=null;}
    if(response.status===401&&refresh&&this.cookies.has('refreshToken')) {
      if(!this.refreshing)this.refreshing=this.renew().finally(()=>{this.refreshing=null;});
      await this.refreshing;return this.request(path,{method,body,headers,timeout,refresh:false});
    }
    return {status:response.status,data,ms:performance.now()-start,requestId:response.headers.get('x-request-id'),retryAfter:response.headers.get('retry-after'),waf:response.headers.get('x-yeodam-edge')};
  }
  async renew() {
    const csrf=await this.request('/api/auth/csrf',{refresh:false});
    if(csrf.status!==200||!csrf.data?.data?.token)throw Error('CSRF renewal failed');
    const refreshed=await this.request('/api/auth/token/refresh',{method:'POST',headers:{'X-CSRF-TOKEN':csrf.data.data.token},refresh:false,timeout:20000});
    if(refreshed.status!==200&&refreshed.status!==204)throw Error('Authentication renewal failed');
    this.refreshes++;
  }
  async identity() {
    const r=await this.request('/api/users/me');if(r.status!==200)throw Error('Identity failed: '+r.status);
    const token=this.cookies.get('accessToken');const claims=JSON.parse(Buffer.from(token.split('.')[1],'base64url'));
    if(!claims.sub||!(claims.exp*1000>Date.now()))throw Error('Expired or invalid authenticated identity');
    return {subjectSha256:digest(String(claims.sub)),expiresAt:new Date(claims.exp*1000).toISOString(),verified:true};
  }
}

export function validateStages(stages) {
  if(!Array.isArray(stages)||!stages.length||stages.length>6)throw Error('1–6 finite stages required');
  let count=0;for(const s of stages){if(!Number.isInteger(s.rate)||s.rate<1||s.rate>80||!Number.isInteger(s.seconds)||s.seconds<1||s.seconds>180)throw Error('Rate/duration budget exceeded');count+=s.rate*s.seconds;}
  if(count>20000)throw Error('Total request budget exceeded');return count;
}

// Open-loop arrival schedule: late generator slots and concurrency drops remain failures,
// rather than silently reducing the offered rate during a slow service response.
export async function readLoad(session, tripId, stages, {concurrency=48,emit=()=>{},safe=async()=>true}={}) {
  validateStages(stages);if(!Number.isSafeInteger(tripId)||tripId<1||concurrency>64||concurrency<1)throw Error('Invalid read target or concurrency');
  const identity=await session.identity();const warm=await session.request('/api/trips/'+tripId);
  if(warm.status!==200||warm.data?.data?.tripId!==tripId)throw Error('Known owned trip must return expected ID');
  const result={startedAt:new Date().toISOString(),tripId,identity,stages:[],aborted:false,refreshesBefore:session.refreshes};
  const inflight=new Set();let fatal=null;
  for(const [index,s] of stages.entries()){
    if(!await safe())throw Error('Pre-stage service health check failed');
    const origin=performance.now();const rows=[];const expected=s.rate*s.seconds;let droppedLate=0,droppedBusy=0;const counts={};
    emit({event:'stage_started',index,...s,at:new Date().toISOString()});
    for(let slot=0;slot<expected;slot++){
      const due=origin+slot*1000/s.rate;const delay=due-performance.now();if(delay>0)await sleep(delay);
      if(fatal)break;
      if(performance.now()-due>Math.max(250,1000/s.rate*2)){droppedLate++;continue;}
      if(inflight.size>=concurrency){droppedBusy++;continue;}
      const operation=(async()=>{const at=new Date().toISOString();try{
        const r=await session.request('/api/trips/'+tripId,{timeout:10000,refresh:false});
        const valid=r.status===200&&r.data?.data?.tripId===tripId;counts[String(r.status)]=(counts[String(r.status)]||0)+1;
        rows.push({at,ms:Math.round(r.ms*1000)/1000,status:r.status,valid});
        if(r.status===401||r.status===403)fatal='Authentication or edge block; stop offered traffic';
      }catch{counts.transport=(counts.transport||0)+1;rows.push({at,status:0,valid:false,ms:null});}})();
      inflight.add(operation);operation.finally(()=>inflight.delete(operation));
      if(slot>0&&slot%s.rate===0){const last=rows.slice(-Math.max(20,s.rate*5));if(last.length>=20&&last.filter(r=>r.status===0||r.status>=500).length/last.length>0.05)fatal='Rolling transport/5xx budget exceeded';}
    }
    const remaining=origin+s.seconds*1000-performance.now();if(remaining>0)await sleep(remaining);
    await Promise.all(inflight);const valid=rows.filter(x=>x.valid);const elapsed=(performance.now()-origin)/1000;
    const stage={...s,index,offered:expected,emitted:rows.length,droppedLate,droppedBusy,counts,elapsedSeconds:elapsed,achievedRps:rows.length/elapsed,validGoodputRps:valid.length/elapsed,p50Ms:percentile(valid.map(x=>x.ms),.5),p95Ms:percentile(valid.map(x=>x.ms),.95),p99Ms:percentile(valid.map(x=>x.ms),.99),samples:rows};
    result.stages.push(stage);emit({event:'stage_completed',index,counts,p95Ms:stage.p95Ms,droppedLate,droppedBusy,at:new Date().toISOString()});
    if(fatal){result.aborted=true;result.reason=fatal;break;}
  }
  result.endedAt=new Date().toISOString();result.refreshesAfter=session.refreshes;return result;
}

export async function preparePhotos(manifest,readFile) {
  if(manifest.files?.length!==20||manifest.inputSetSha256!=='fbac05c1210e61ec06730d2129ef96574f7395c38cb16bf9eb7e5e05bbaea142')throw Error('Fixed JPEG20 input required');
  const files=[];for(const f of manifest.files){if(f.relativePath.includes('..')||f.format!=='jpeg')throw Error('Unsafe photo path');const bytes=await readFile(manifest.root+'/'+f.relativePath);if(bytes.length!==f.bytes||digest(bytes)!==f.sha256)throw Error('Photo content changed');files.push({name:f.relativePath.split('/').at(-1),bytes});}return files;
}

export async function photoTrip(session,files,{name,regionCode,startDate,endDate,emit=()=>{},safe=async()=>true,pollSeconds=3,budgetSeconds=900,transport=fetch}){
  if(files.length!==20||!/^726-/.test(name)||budgetSeconds>1200||budgetSeconds<60)throw Error('Photo trial budget mismatch');
  if(!await safe())throw Error('Photo preflight failed');
  const csrf=await session.request('/api/auth/csrf');const token=csrf.data?.data?.token;if(csrf.status!==200||!token)throw Error('No CSRF token');
  const context=traceContext();const headers={'X-CSRF-TOKEN':token,traceparent:`00-${context.traceId}-${context.parent}-01`};
  const startedAt=new Date().toISOString();const created=await session.request('/api/trips',{method:'POST',body:{tripName:name,startDate,endDate,regionCodes:[regionCode]},headers});
  const tripId=created.data?.data?.tripId;if(created.status!==201||!Number.isSafeInteger(tripId))throw Error('Create rejected: '+created.status);
  const result={tripId,traceId:context.traceId,startedAt,batches:[],polls:[]};emit({event:'trip_created',tripId,traceId:context.traceId,at:startedAt});
  for(let batchNo=0;batchNo<2;batchNo++){
    if(!await safe())throw Error('Photo admission health changed');const batch=files.slice(batchNo*10,(batchNo+1)*10);
    const issued=await session.request(`/api/trips/${tripId}/initial-attachments/upload-urls`,{method:'POST',headers,body:{batchNo:batchNo+1,totalAttachmentCount:20,complete:batchNo===1,attachments:batch.map(f=>({fileName:f.name,contentType:'image/jpeg',sizeBytes:f.bytes.length}))}});
    const targets=issued.data?.data?.attachments;const uploadId=issued.data?.data?.uploadId;
    if(issued.status!==200||targets?.length!==10||!uploadId)throw Error('Upload admission rejected: '+issued.status);
    const t=performance.now();await Promise.all(targets.map(async(target,i)=>{
      const u=new URL(target.uploadUrl);if(u.protocol!=='https:'||!/^.+\.s3[.-]ap-northeast-2\.amazonaws\.com$/.test(u.hostname)||target.method!=='PUT')throw Error('Unexpected presigned upload destination');
      const r=await transport(u,{method:'PUT',headers:target.headers,body:batch[i].bytes,redirect:'error',signal:AbortSignal.timeout(60000)});await r.arrayBuffer();if(r.status!==200&&r.status!==412)throw Error('Storage upload failed: '+r.status);
    }));
    const accepted=await session.request(`/api/trips/${tripId}/initial-attachments`,{method:'POST',headers,body:{uploadId},timeout:30000});
    if(![200,202,204].includes(accepted.status))throw Error('Batch completion rejected: '+accepted.status);
    result.batches.push({batchNo,acceptedAt:new Date().toISOString(),status:accepted.status,putMs:performance.now()-t,ackMs:accepted.ms,requestId:accepted.requestId});
    emit({event:'batch_accepted',tripId,...result.batches.at(-1)});
  }
  const stop=performance.now()+budgetSeconds*1000;
  while(performance.now()<stop){
    // Polling deliberately has no workflow traceparent: observer GETs are not children of business execution.
    const r=await session.request(`/api/trips/${tripId}/processing-status`);const d=r.data?.data;
    result.polls.push({at:new Date().toISOString(),http:r.status,state:d?.status??null,done:d?.progress?.done??null,total:d?.progress?.total??null});
    if(r.status!==200)throw Error('Business observation failed: '+r.status);
    if(['COMPLETED','FAILED','CANCELED'].includes(d?.status)){result.finalStatus=d.status;result.completedAt=new Date().toISOString();result.finalCounts=d.result;result.observedCompletionSeconds=(Date.parse(result.completedAt)-Date.parse(result.batches.at(-1).acceptedAt))/1000;emit({event:'trip_terminal',tripId,status:d.status,at:result.completedAt});return result;}
    await sleep(pollSeconds*1000);
  }
  result.finalStatus='OBSERVATION_TIMEOUT';return result;
}
