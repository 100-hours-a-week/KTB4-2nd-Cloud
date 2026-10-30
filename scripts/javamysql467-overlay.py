"""Explicit duration and observation overlays, never changing processing semantics."""

def replace_once(text,before,after):
 if text.count(before)!=1:raise ValueError('Frozen anchor differs: '+before[:80])
 return text.replace(before,after,1)

def generator(text):
 return replace_once(text,"'K6_TRIP_DURATION_SECONDS':'900'","'K6_TRIP_DURATION_SECONDS':'180'")

def scenario(text):
 for before,after in [('duration !== 900','duration !== 180'),('and 900s','and 180s'),("duration: '900s'","duration: '180s'"),("maxDuration: '1005s'","maxDuration: '285s'"),('exec.scenario.startTime + 900000','exec.scenario.startTime + 180000')]:text=replace_once(text,before,after)
 return text

def app(text):
 needle="    payload={'Image':IMAGE,'Env':[k+'='+v for k,v in env.items()],'HostConfig':hc,"
 inserted="""    # Observation only: same image/heap/cpu/concurrency. JRE-native JFR, no open port.
    jfr='/run/yeodam-javamysql467/'+run+'/'+role
    hc.setdefault('Binds',[]).append(jfr+':/run/jfr467:rw')
    env['JAVA_TOOL_OPTIONS']+=' -XX:StartFlightRecording=name=diag467,settings=/run/jfr467/settings.jfc,disk=true,maxsize=64m,maxage=15m,dumponexit=true,filename=/run/jfr467/trace.jfr'
"""
 return replace_once(text,needle,inserted+needle)

def resources(text):
 return replace_once(text,'while time.time()<a.started_at+900:','while time.time()<a.started_at+180:')
