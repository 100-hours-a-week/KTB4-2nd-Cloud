#!/usr/bin/env node
// Local Unix socket, mode 0600; authenticated cookies stay in process memory.
import http from 'node:http';
import { readFile, writeFile, mkdir, chmod, unlink } from 'node:fs/promises';
import { Session, readLoad, photoTrip, preparePhotos } from '../lib/v3-final.mjs';
import { pathToFileURL } from 'node:url';

export async function serve(socket, output) {
  if(!socket.startsWith('/tmp/yeodam-726-')||!output.includes('/.local-docs/private/726-'))throw Error('Private task paths required');
  await mkdir(output,{recursive:true,mode:0o700});
  const sessions=new Map();let active=null;const results=new Map();let closed=false;
  const save=async(name,data)=>writeFile(output+'/'+name+'.json',JSON.stringify(data,null,2),{mode:0o600,flag:'wx'});
  const server=http.createServer(async(req,res)=>{
    res.setHeader('content-type','application/json');let raw='';
    try {
      for await(const part of req){raw+=part;if(raw.length>65536)throw Error('Command too large');}
      const c=raw?JSON.parse(raw):{};
      if(req.method==='POST'&&req.url==='/session') {
        if(!['A','B'].includes(c.label)||sessions.has(c.label))throw Error('Unique account label required');
        const s=new Session(c.cookies);const identity=await s.identity();
        for(const v of sessions.values())if((await v.identity()).subjectSha256===identity.subjectSha256)throw Error('Second account must differ');
        sessions.set(c.label,s);res.end(JSON.stringify({label:c.label,identity}));return;
      }
      if(req.method==='GET'&&req.url==='/status'){res.end(JSON.stringify({accounts:[...sessions.keys()],active,resultNames:[...results.keys()]}));return;}
      if(req.method==='GET'&&req.url?.startsWith('/result/')){res.end(JSON.stringify(results.get(req.url.slice(8))??null));return;}
      if(req.method==='POST'&&req.url==='/run'){
        if(active||!/^726-[a-z0-9-]{1,70}$/.test(c.name)||results.has(c.name))throw Error('One unique finite run at a time');
        const s=sessions.get(c.account||'A');if(!s)throw Error('Account unavailable');
        active=c.name;await save(c.name+'-plan',{...c,at:new Date().toISOString(),credentialsPersisted:false});
        const events=[];const emit=e=>{events.push(e);console.log(JSON.stringify(e));};
        const operation=(async()=>{
          if(c.kind==='read')return readLoad(s,c.tripId,c.stages,{emit});
          if(c.kind==='photo'){
            if(![1,2,3,4].includes(c.trips)||!Number.isInteger(c.intervalSeconds)||c.intervalSeconds<0||c.intervalSeconds>120)throw Error('At most four fixed photo trips');
            const manifest=JSON.parse(await readFile(c.manifest,'utf8'));const files=await preparePhotos(manifest,readFile);const running=[];let admissionFailure=false;
            for(let i=0;i<c.trips;i++){
              if(i)await new Promise(r=>setTimeout(r,c.intervalSeconds*1000));
              if(admissionFailure)break;
              running.push(photoTrip(s,files,{name:c.name+'-'+(i+1),regionCode:c.regionCode,startDate:c.startDate,endDate:c.endDate,emit}).catch(e=>{admissionFailure=true;return {failed:true,error:e.message};}));
            }
            return {startedAt:events[0]?.at,trips:await Promise.all(running),endedAt:new Date().toISOString()};
          }
          throw Error('Unsupported final test');
        })();
        operation.then(async result=>{await save(c.name,{...result,events});results.set(c.name,{done:true,result});}).catch(async e=>{const error={done:true,failed:true,error:e.message,events};await save(c.name+'-failed',error).catch(()=>{});results.set(c.name,error);}).finally(()=>{active=null;});
        res.end(JSON.stringify({accepted:true,name:c.name}));return;
      }
      if(req.method==='POST'&&req.url==='/read'){
        const s=sessions.get(c.account||'A');if(!s)throw Error('Account unavailable');
        if(!['/api/trips?size=20','/api/regions?keyword=%EC%84%B1%EB%82%A8'].includes(c.path)&&!/^\/api\/trips\/[0-9]+(?:\/processing-status)?$/.test(c.path))throw Error('Read-only discovery only');
        const r=await s.request(c.path);const d=r.data?.data;
        // Never export names, photos, signed URLs, contact info or raw credentials.
        res.end(JSON.stringify({status:r.status,ms:r.ms,data:Array.isArray(d?.items)?d.items.map(t=>({tripId:t.tripId,status:t.status})):c.path.includes('regions')?d:d?{tripId:d.tripId,status:d.status,progress:d.progress,result:d.result,regionCodes:d.regions?.map(x=>x.regionCode)}:null}));return;
      }
      if(req.method==='POST'&&req.url==='/close'){
        if(active)throw Error('Cannot close during work');sessions.clear();closed=true;res.end('{}');server.close();return;
      }
      res.statusCode=404;res.end('{}');
    }catch(e){res.statusCode=400;res.end(JSON.stringify({error:e.message}));}
  });
  await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(socket,resolve);});await chmod(socket,0o600);
  console.log(JSON.stringify({socket,credentialsOnDisk:false,ready:true}));
  process.on('SIGTERM',()=>{if(!active){sessions.clear();server.close();}});
  server.on('close',()=>unlink(socket).catch(()=>{}));return server;
}
if(import.meta.url===pathToFileURL(process.argv[1]||'').href){await serve(process.argv[2],process.argv[3]);}
