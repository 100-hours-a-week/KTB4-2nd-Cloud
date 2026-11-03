import java.nio.file.Path;
import java.util.*;
import jdk.jfr.consumer.*;

/** Offline drill-down of safe numeric/profile fields only. */
public final class Jfr467Detail {
 static boolean has(List<String> f,String s){return f.stream().anyMatch(v->v.contains(s));}
 static String path(List<String> f){
  if(has(f,"ActiveLoginSessionValidator"))return "login_session_validation";
  if(has(f,"CsrfTokenStore")||has(f,"RdbCsrfTokenRepository"))return "csrf_validation";
  if(has(f,"MigrationTripApplyService"))return "trip_apply";
  if(has(f,"MigrationTripAdmissionService"))return "trip_admission";
  if(has(f,"MigrationAuthApplyService"))return "auth_queue_apply";
  if(has(f,"org.springframework.security.oauth2.jwt.")||has(f,"com.nimbusds.jose."))return "jwt_decode_verify";
  if(has(f,"org.hibernate.")||has(f,"org.springframework.jdbc.")||has(f,"org.springframework.data."))return "persistence_without_feature_frame";
  if(has(f,"tools.jackson.")||has(f,"com.fasterxml.jackson."))return "json_without_feature_frame";
  if(has(f,"org.springframework.security."))return "security_other";
  return "other";
 }
 static void add(Map<String,Long> m,String k,long v){m.merge(k,v,Long::sum);}
 static Map<String,Long> top(Map<String,Long> m,int n){var out=new LinkedHashMap<String,Long>();m.entrySet().stream().sorted(Map.Entry.<String,Long>comparingByValue().reversed()).limit(n).forEach(e->out.put(e.getKey(),e.getValue()));return out;}
 static Double millis(RecordedEvent e,String f){try{return e.hasField(f)?e.getDuration(f).toNanos()/1e6:null;}catch(IllegalArgumentException x){return null;}}
 public static void main(String[] a)throws Exception{
  long from=Long.parseLong(a[1]),to=Long.parseLong(a[2]);var exec=new HashMap<String,Long>();var nativeLeaf=new HashMap<String,Long>();var paths=new TreeMap<String,Long>();var categoryLeaf=new TreeMap<String,Map<String,Long>>();var previous=new HashMap<Long,Long>();var cpuWeights=new TreeMap<String,Double>();var gc=new TreeMap<String,Double>();var compiler=new ArrayList<Map<String,Object>>();long execution=0,nativeCount=0,truncated=0;
  try(var r=new RecordingFile(Path.of(a[0]))){while(r.hasMoreEvents()){
   var e=r.readEvent();String name=e.getEventType().getName();long at=e.getStartTime().toEpochMilli();boolean selected=from<=at&&at<to;
   if(name.equals("jdk.ThreadCPULoad")){
    var thread=e.getThread();if(thread!=null){long id=thread.getJavaThreadId();Long prev=previous.put(id,at);double seconds=Math.max(0,Math.min(at,to)-Math.max(prev==null?at-5000:prev,from))/1000.0;double load=e.getFloat("user")+e.getFloat("system");if(load>=0)cpuWeights.merge(Jfr467Summary.category(thread),load*seconds,Double::sum);}
   }
   if(!selected)continue;
   if(name.equals("jdk.ExecutionSample")||name.equals("jdk.NativeMethodSample")){
    var trace=e.getStackTrace();if(trace==null||trace.getFrames().isEmpty())continue;
    var frames=new ArrayList<String>();for(var f:trace.getFrames())frames.add(Jfr467Summary.frame(f));String leaf=frames.get(0);
    if(name.equals("jdk.ExecutionSample")){execution++;if(trace.isTruncated())truncated++;add(exec,leaf,1);String key=path(frames);add(paths,key,1);add(categoryLeaf.computeIfAbsent(key,k->new HashMap<>()),leaf,1);}
    else{nativeCount++;add(nativeLeaf,leaf,1);}
   }
   if(name.equals("jdk.GCCPUTime"))for(String f:List.of("userTime","systemTime","realTime")){Double v=millis(e,f);if(v!=null)gc.merge(f,v,Double::sum);}
   if(name.equals("jdk.CompilerStatistics")){
    var v=new LinkedHashMap<String,Object>();v.put("at",at);
    for(String f:List.of("totalTime","peakTime")){Double n=millis(e,f);if(n!=null)v.put(f+"Ms",n);}
    for(String f:List.of("compileCount","bailoutCount","invalidatedCount","osrCompileCount","standardCompileCount"))if(e.hasField(f))v.put(f,e.getLong(f));
    compiler.add(v);
   }
  }}
  var examples=new TreeMap<String,Object>();for(var e:categoryLeaf.entrySet())examples.put(e.getKey(),top(e.getValue(),6));
  var out=new LinkedHashMap<String,Object>();out.put("executionSamples",execution);out.put("nativeSamples",nativeCount);out.put("truncatedExecutionStacks",truncated);out.put("executionPaths",paths);out.put("topExecutionLeaf",top(exec,25));out.put("topNativeLeaf",top(nativeLeaf,10));out.put("executionPathLeaves",examples);out.put("threadLoadWeightFractionSeconds",cpuWeights);out.put("gcReportedCpuTimeMs",gc);out.put("compilerStatisticsFirst",compiler.isEmpty()?Map.of():compiler.get(0));out.put("compilerStatisticsLast",compiler.isEmpty()?Map.of():compiler.get(compiler.size()-1));out.put("compilerStatisticsCount",compiler.size());out.put("limitations",List.of("Execution/native sampling separated; native poll samples are not CPU time.","Disjoint path labels are inferred from retained frames; truncated stacks can miss feature methods.","Thread load integral uses reported fractions and preceding samples; not complete JVM-native CPU accounting.","Compilation/GC durations and process GC CPU reports are not exclusive CPU percentages."));System.out.println(Jfr467Summary.json(out));
 }
}
