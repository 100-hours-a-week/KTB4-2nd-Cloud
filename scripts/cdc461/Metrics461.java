import com.sun.tools.attach.VirtualMachine;
import java.lang.management.*;
import java.util.*;
import java.nio.file.*;
import javax.management.*;
import javax.management.remote.*;

/** Numeric-only local JMX sampling. No records, credentials or connector config. */
public class Metrics461 {
    static String json(Object value) {
        if (value == null) return "null";
        if (value instanceof Number) {
            double n = ((Number)value).doubleValue();
            return Double.isFinite(n) ? value.toString() : "null";
        }
        if (value instanceof Boolean) return value.toString();
        if (value instanceof Map) {
            List<String> parts = new ArrayList<>();
            ((Map<?,?>)value).forEach((k,v) -> parts.add(json(k.toString())+":"+json(v)));
            return "{"+String.join(",",parts)+"}";
        }
        if (value instanceof Iterable) {
            List<String> parts = new ArrayList<>();
            for (Object v:(Iterable<?>)value) parts.add(json(v));
            return "["+String.join(",",parts)+"]";
        }
        return "\""+value.toString().replace("\\","\\\\").replace("\"","\\\"").replace("\n","\\n").replace("\r","\\r")+"\"";
    }
    static Map<String,Object> attributes(MBeanServerConnection server, ObjectName name) throws Exception {
        List<String> keys = new ArrayList<>();
        // These bean types contain task/consumer measurements, not connector config.
        for (MBeanAttributeInfo a:server.getMBeanInfo(name).getAttributes()) {
            if (a.isReadable() && (a.getType().equals("double") || a.getType().equals("long") ||
                    a.getType().equals("int") || a.getType().equals("java.lang.Double") || a.getType().equals("java.lang.Long"))) keys.add(a.getName());
        }
        Map<String,Object> values = new TreeMap<>();
        for (Object item:server.getAttributes(name,keys.toArray(new String[0]))) {
            Attribute a=(Attribute)item;
            if (a.getValue() instanceof Number) values.put(a.getName(),a.getValue());
        }
        return values;
    }
    public static void main(String[] args) throws Exception {
        if (args.length!=3 || !args[0].matches("yeodam-diag461-[a-f0-9]{8}")) throw new IllegalArgumentException("Scoped connector prefix required");
        String prefix=args[0]; int seconds=Integer.parseInt(args[1]), interval=Integer.parseInt(args[2]);
        if (seconds<1 || seconds>780 || interval<1 || interval>10) throw new IllegalArgumentException("Bounded sampling required");
        List<String> candidates=new ArrayList<>();
        VirtualMachine.list().forEach(v -> {if (v.displayName().contains("ConnectDistributed")) candidates.add(v.id());});
        if (candidates.size()!=1) throw new IllegalStateException("Expected one Connect JVM");
        VirtualMachine vm=VirtualMachine.attach(candidates.get(0));
        String address;
        try {
            address=vm.getAgentProperties().getProperty("com.sun.management.jmxremote.localConnectorAddress");
            if (address==null) address=vm.startLocalManagementAgent();
        } finally { vm.detach(); }
        // The local agent's address is deliberately never printed.
        try (JMXConnector connection=JMXConnectorFactory.connect(new JMXServiceURL(address))) {
            MBeanServerConnection server=connection.getMBeanServerConnection();
            ThreadMXBean threads=ManagementFactory.newPlatformMXBeanProxy(server,ManagementFactory.THREAD_MXBEAN_NAME,ThreadMXBean.class);
            MemoryMXBean memory=ManagementFactory.newPlatformMXBeanProxy(server,ManagementFactory.MEMORY_MXBEAN_NAME,MemoryMXBean.class);
            com.sun.management.OperatingSystemMXBean self=(com.sun.management.OperatingSystemMXBean)ManagementFactory.getOperatingSystemMXBean();
            long until=System.nanoTime()+seconds*1000000000L; int frame=0;
            while (System.nanoTime()<until) {
                if (Files.exists(Path.of("/tmp/stop-"+prefix))) break;
                long began=System.nanoTime(); Map<String,Object> row=new LinkedHashMap<>();
                row.put("at",System.currentTimeMillis()/1000.0);row.put("observerCpuNanos",self.getProcessCpuTime());
                Map<String,Object> beans=new TreeMap<>();
                for (String pattern:new String[]{"kafka.connect:type=sink-task-metrics,*","kafka.connect:type=task-metrics,*",
                        "kafka.consumer:type=consumer-fetch-manager-metrics,*","kafka.consumer:type=consumer-coordinator-metrics,*"}) {
                    for (ObjectName name:server.queryNames(new ObjectName(pattern),null)) {
                        String connector=name.getKeyProperty("connector"), client=name.getKeyProperty("client-id");
                        if (connector!=null && connector.startsWith("\"")) connector=ObjectName.unquote(connector);
                        if (client!=null && client.startsWith("\"")) client=ObjectName.unquote(client);
                        if ((connector!=null && connector.startsWith(prefix+"-sink")) || (client!=null && client.contains(prefix+"-sink"))) {
                            try {beans.put(name.toString(),attributes(server,name));}
                            catch (InstanceNotFoundException ignored) { /* Task may be starting/stopping. */ }
                        }
                    }
                }
                row.put("beans",beans);row.put("heapUsed",memory.getHeapMemoryUsage().getUsed());
                row.put("workerCpuNanos",server.getAttribute(new ObjectName("java.lang:type=OperatingSystem"),"ProcessCpuTime"));
                List<Object> gc=new ArrayList<>();
                for (ObjectName name:server.queryNames(new ObjectName("java.lang:type=GarbageCollector,*"),null)) {
                    gc.add(Map.of("name",name.getKeyProperty("name"),"count",server.getAttribute(name,"CollectionCount"),"millis",server.getAttribute(name,"CollectionTime")));
                }
                row.put("gc",gc);
                if (frame%5==0) {
                    List<Object> stacks=new ArrayList<>();
                    for (ThreadInfo t:threads.dumpAllThreads(false,false,24)) {
                        if (t!=null && t.getThreadName().contains(prefix+"-sink")) {
                            List<String> stack=new ArrayList<>();
                            for (StackTraceElement e:t.getStackTrace()) stack.add(e.toString());
                            stacks.add(Map.of("thread",t.getThreadName(),"state",t.getThreadState().toString(),"cpuNanos",threads.getThreadCpuTime(t.getThreadId()),"stack",stack));
                        }
                    }
                    row.put("taskThreads",stacks);
                }
                row.put("sampleMillis",(System.nanoTime()-began)/1000000.0);
                System.out.println(json(row));System.out.flush();frame++;
                long sleep=interval*1000000000L-(System.nanoTime()-began);
                if (sleep>0) Thread.sleep(sleep/1000000L);
            }
        }
    }
}
