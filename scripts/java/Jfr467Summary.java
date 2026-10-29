import java.nio.file.Path;
import java.time.Instant;
import java.util.*;
import jdk.jfr.consumer.*;

/** Payload-free JFR reader; never serializes arbitrary event fields. */
public final class Jfr467Summary {
    static final Set<String> DENIED=Set.of("jdk.InitialEnvironmentVariable","jdk.InitialSystemProperty","jdk.JVMInformation","jdk.SystemProcess","jdk.ProcessStart","jdk.JavaExceptionThrow","jdk.JavaErrorThrow","jdk.SecurityProperty","jdk.SecurityPropertyModification");
    static String category(RecordedThread t) {
        String n=t==null?"":t.getJavaName();
        if(n==null)n="";
        if(n.contains("CompilerThread")||n.contains("Sweeper"))return "compiler";
        if(n.startsWith("GC ")||n.contains("G1 ")||n.contains("ZGC"))return "gc";
        if(n.contains("kafka")||n.contains("migrationTrip")||n.contains("migrationAuth"))return "kafka";
        if(n.contains("http-nio")||n.contains("tomcat"))return "http";
        if(n.contains("Flight Recorder")||n.contains("JFR"))return "recorder";
        return "other";
    }
    static void inc(Map<String,Long> m,String key,long n){m.merge(key,n,Long::sum);}
    static String frame(RecordedFrame f){var m=f.getMethod();return m==null||m.getType()==null?"<unresolved>":m.getType().getName()+"."+m.getName();}
    static Map<String,Long> top(Map<String,Long> m,int limit){var out=new LinkedHashMap<String,Long>();m.entrySet().stream().sorted(Map.Entry.<String,Long>comparingByValue().reversed()).limit(limit).forEach(e->out.put(e.getKey(),e.getValue()));return out;}
    static String json(Object x){
        if(x==null)return "null";
        if(x instanceof String s)return "\""+s.replace("\\","\\\\").replace("\"","\\\"").replace("\n","\\n").replace("\r","\\r")+"\"";
        if(x instanceof Number||x instanceof Boolean)return x.toString();
        if(x instanceof Map<?,?> m){var a=new ArrayList<String>();for(var e:m.entrySet())a.add(json(e.getKey().toString())+":"+json(e.getValue()));return "{"+String.join(",",a)+"}";}
        if(x instanceof Collection<?> c){var a=new ArrayList<String>();for(Object v:c)a.add(json(v));return "["+String.join(",",a)+"]";}
        throw new IllegalArgumentException("Unsupported safe output type");
    }
    public static void main(String[] args)throws Exception {
        Path file=Path.of(args[0]);long from=args.length>1?Long.parseLong(args[1]):0,to=args.length>2?Long.parseLong(args[2]):Long.MAX_VALUE;
        var all=new TreeMap<String,Long>();var counts=new TreeMap<String,Long>();var leaf=new HashMap<String,Long>();var inclusive=new HashMap<String,Long>();var threads=new TreeMap<String,Long>();var durations=new TreeMap<String,Double>();var cpu=new TreeMap<String,double[]>();var allocations=new HashMap<String,Long>();double[] jvmCpu=new double[4];long first=Long.MAX_VALUE,last=0,execution=0;var denied=new TreeMap<String,Long>();
        try(var r=new RecordingFile(file)){
            while(r.hasMoreEvents()){
                var e=r.readEvent();String name=e.getEventType().getName();inc(all,name,1);if(DENIED.contains(name))inc(denied,name,1);
                long at=e.getStartTime().toEpochMilli();if(at<from||at>=to)continue;
                first=Math.min(first,at);last=Math.max(last,at);inc(counts,name,1);
                if(name.equals("jdk.ExecutionSample")||name.equals("jdk.NativeMethodSample")){
                    execution++;RecordedThread thread=e.hasField("sampledThread")?e.getThread("sampledThread"):e.getThread();inc(threads,category(thread),1);
                    var trace=e.getStackTrace();if(trace!=null&&!trace.getFrames().isEmpty()){
                        inc(leaf,frame(trace.getFrames().get(0)),1);var seen=new HashSet<String>();
                        for(var f:trace.getFrames())if(seen.add(frame(f)))inc(inclusive,frame(f),1);
                    }
                }
                if(Set.of("jdk.GarbageCollection","jdk.GCPhasePause","jdk.Compilation","jdk.SocketRead","jdk.SocketWrite","jdk.ThreadPark","jdk.JavaMonitorEnter","jdk.JavaMonitorWait","jdk.FileRead","jdk.FileWrite").contains(name))durations.merge(name,e.getDuration().toNanos()/1e6,Double::sum);
                if(name.equals("jdk.CPULoad")){jvmCpu[0]++;jvmCpu[1]+=e.getFloat("jvmUser");jvmCpu[2]+=e.getFloat("jvmSystem");jvmCpu[3]+=e.getFloat("machineTotal");}
                if(name.equals("jdk.ObjectAllocationSample")&&e.hasField("weight"))inc(allocations,e.getClass("objectClass").getName(),e.getLong("weight"));
                if(name.equals("jdk.ThreadCPULoad")){
                    var v=cpu.computeIfAbsent(category(e.getThread()),k->new double[3]);v[0]++;v[1]+=e.getFloat("user");v[2]+=e.getFloat("system");
                }
            }
        }
        var cpuOut=new TreeMap<String,Object>();for(var e:cpu.entrySet()){var v=e.getValue();cpuOut.put(e.getKey(),Map.of("events",(long)v[0],"reportedUserMean",v[1]/v[0],"reportedSystemMean",v[2]/v[0]));}
        var out=new LinkedHashMap<String,Object>();out.put("file",file.getFileName().toString());out.put("fromEpochMs",from);out.put("toEpochMs",to);out.put("firstSelectedEpochMs",first==Long.MAX_VALUE?null:first);out.put("lastSelectedEpochMs",last);out.put("deniedEventCounts",denied);out.put("allEventCounts",all);out.put("selectedEventCounts",counts);out.put("jvmCpuReportedMeans",jvmCpu[0]==0?Map.of():Map.of("events",(long)jvmCpu[0],"jvmUser",jvmCpu[1]/jvmCpu[0],"jvmSystem",jvmCpu[2]/jvmCpu[0],"machineTotal",jvmCpu[3]/jvmCpu[0]));out.put("sampledAllocationWeightsByClass",top(allocations,20));out.put("executionSamples",execution);out.put("sampleThreadCategories",threads);out.put("topLeafSamples",top(leaf,25));out.put("topInclusiveSamples",top(inclusive,35));out.put("recordedDurationSumMs",durations);out.put("threadCpuLoadReportedMeans",cpuOut);out.put("limitations",List.of("Default JFR thresholds/sampling; absent events do not prove zero cost.","Duration sums can overlap and include waits; not CPU time or complete JDBC timing.","Inclusive stack counts can exceed sample total; thread categories are name-based.","CPU fractions retain JFR reported units; not normalized host capacity percentages."));
        System.out.println(json(out));if(!denied.isEmpty())System.exit(2);
    }
}
