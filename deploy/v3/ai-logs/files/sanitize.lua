-- No Kubernetes API access. Identity comes from the restricted host file path.
local blocked = {
 authorization=true, cookie=true, cookies=true, setcookie=true, password=true,
 passwd=true, apikey=true, secret=true, accesstoken=true, refreshtoken=true,
 csrf=true, csrftoken=true, session=true, sessionid=true, headers=true,
 requestbody=true, responsebody=true, body=true, payload=true, prompt=true,
 completion=true, photos=true, image=true, imagebase64=true, imageurl=true,
 presignedurl=true, signedurl=true, email=true, username=true, latitude=true,
 longitude=true, gps=true
}
local function clean(value, depth)
 if depth > 12 then return '[DEPTH_LIMIT]' end
 if type(value) == 'table' then
  local result = {}
  for key, item in pairs(value) do
   local normalized = string.lower(tostring(key)):gsub('[^a-z0-9]', '')
   if blocked[normalized] or normalized:find('password') or normalized:find('apikey') or normalized:find('token') or normalized:find('secret') then
    result[key] = '[REDACTED]'
   else result[key] = clean(item, depth + 1) end
  end
  return result
 end
 if type(value) == 'string' then
  local lower = string.lower(value)
  if lower:find('authorization') or lower:find('cookie') or lower:find('password') or lower:find('api_key') or lower:find('access_token') or lower:find('refresh_token') then return '[REDACTED]' end
  value = value:gsub('[Bb][Ee][Aa][Rr][Ee][Rr]%s+[^%s]+', 'Bearer [REDACTED]')
  value = value:gsub('https?://[^%s"<>]+', '[URL_REDACTED]')
  value = value:gsub('eyJ[%w_%-]+%.[%w_%-]+%.[%w_%-]+', '[JWT_REDACTED]')
  return value:gsub('[%w%._%%+%-]+@[%w%.%-]+%.[%a]+', '[EMAIL_REDACTED]')
 end
 return value
end
-- Backend fields are allowlisted: raw message, stack, object keys and unknown extras
-- never leave the node. Keep the existing AI sanitizer unchanged below.
local be_roles = {['auth-api']='auth', ['trip-api']='trip', ['photo-api']='photo', ['photo-converter']='photo', ['trip-result-worker']='trip'}
local be_fields = {
 timestamp=true, level=true, service=true, logger=true, event=true, result=true,
 request_id=true, trace_id=true, span_id=true, trip_id=true, job_id=true,
 duration_ms=true, failure_stage=true, error_code=true, attempts=true,
 expected_count=true, saved_count=true, workflow=true, stage=true,
 stage_started_at=true, stage_finished_at=true, message_type=true, batch_id=true,
 failed_count=true, generation_id=true, workflow_elapsed_ms=true, batch_no=true,
 item_id=true, photo_count=true, place_count=true, unique_coordinate_count=true,
 lookup_failure_count=true, fallback_count=true, work_duration_ms=true,
 stage_elapsed_ms=true, sample_count=true, release=true, commit_result=true
}
local function backend(tag, timestamp, record)
 for role, namespace in pairs(be_roles) do
  local escaped = role:gsub('%-', '%%-')
  local pod = tag:match('^be%.work%.var%.log%.containers%.('..escaped..'%-[^_]+)_'..namespace..'_backend%-[a-f0-9]+%.log$')
  if pod then
   if record.level == 'DEBUG' or record.level == 'TRACE' then return -1, timestamp, record end
   local app = {}
   if record.log ~= nil then
    app = {event='collector_unparsed_log', parse_error=true, omitted_bytes=#tostring(record.log)}
   else
    for key, value in pairs(record) do
     if be_fields[key] and (type(value) ~= 'table') then
      if type(value) == 'string' and #value > 512 then value='[OVERSIZE_REDACTED]' end
      if (key == 'trace_id' and type(value)=='string' and (#value~=32 or not value:match('^[a-f0-9]+$')))
       or (key == 'span_id' and type(value)=='string' and (#value~=16 or not value:match('^[a-f0-9]+$'))) then
       app[key]=flb_null
      else app[key]=clean(value, 0) end
     end
    end
    -- Keep diagnostic class/frame names, never the exception message or raw stack.
    if type(record.stack_trace)=='string' then
     local class=record.stack_trace:match('^([%w_.$]+)')
     if class and (class:match('Exception$') or class:match('Error$')) then
      app.error_type=class
      local frames={}
      for method, file, line in record.stack_trace:gmatch('\n%s*at%s+([%w_.$<>]+)%(([%w_.$]+):(%d+)%)') do
       if #frames>=8 then break end
       table.insert(frames,method..'('..file..':'..line..')')
      end
      if #frames>0 then app.stack_frames=frames end
     end
    end
    if app.event == nil then app.event=app.error_type and 'collector_exception_log' or 'collector_unclassified_log' end
   end
   return 2, timestamp, {app=app, kubernetes={namespace_name=namespace,pod_name=pod,container_name='backend',node_name=os.getenv('NODE_NAME') or 'unknown'},collector={role=role,stream=record.stream or 'unknown'}}
  end
 end
 return -1, timestamp, record
end
function sanitize(tag, timestamp, record)
 if tag:match("^be%.") then return backend(tag, timestamp, record) end
 local pod, role
 pod = tag:match('^ai%.api%.var%.log%.containers%.(ai%-api%-[^_]+)_ai_ai%-[a-f0-9]+%.log$')
 if pod then role = 'ai-api' else
  pod = tag:match('^ai%.runner%.var%.log%.containers%.(ai%-runner%-[^_]+)_ai_ai%-[a-f0-9]+%.log$')
  role = 'ai-runner'
 end
 if not pod then return -1, timestamp, record end
 local app
 if record.log ~= nil then
  -- Never forward arbitrary startup text/partial JSON that cannot be safely inspected.
  app = {event='collector_unparsed_log', parse_error=true, omitted_bytes=#tostring(record.log)}
 else
  local original = {}
  for k,v in pairs(record) do
   if k ~= 'time' and k ~= 'stream' and k ~= 'logtag' then original[k] = v end
  end
  app = clean(original, 0)
 end
 local result = {app=app, kubernetes={namespace_name='ai', pod_name=pod, container_name='ai', node_name=os.getenv('NODE_NAME') or 'unknown'}, collector={role=role, stream=record.stream or 'unknown'}}
 return 2, timestamp, result
end
