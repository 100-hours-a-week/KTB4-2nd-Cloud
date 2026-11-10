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
function sanitize(tag, timestamp, record)
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
