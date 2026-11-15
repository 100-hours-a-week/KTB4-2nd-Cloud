import java.nio.file.*;
import java.time.*;
import java.util.*;
import java.util.concurrent.*;
import java.security.*;
import org.apache.kafka.clients.admin.*;
import org.apache.kafka.common.TopicPartition;
/** Describe/ListOffsets only. No consume/commit/produce or group membership. */
public final class ConverterOwnership {
    static final String TOPIC="prod.yeodam.image.initial-light", GROUP="prod.yeodam.image-conversion-initial-light";
    static String q(String s){return "\""+s.replace("\\","\\\\").replace("\"","\\\"")+"\"";}
    static String hash(String s)throws Exception{return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(s.getBytes(java.nio.charset.StandardCharsets.UTF_8)));}
    public static void main(String[] args)throws Exception {
        int seconds=Integer.parseInt(args[0]); if(seconds<1||seconds>1020)throw new IllegalArgumentException("Bounded metadata window required");
        Properties p=new Properties();p.setProperty("bootstrap.servers","yeodam-v3-kafka-kafka-bootstrap.kafka.svc:9093");
        p.setProperty("security.protocol","SASL_SSL");p.setProperty("sasl.mechanism","SCRAM-SHA-512");
        String u=Files.readString(Path.of("/auth/username")).strip(),pw=Files.readString(Path.of("/auth/password")).strip();
        p.setProperty("sasl.jaas.config","org.apache.kafka.common.security.scram.ScramLoginModule required username="+q(u)+" password="+q(pw)+";");
        p.setProperty("ssl.truststore.type","PEM");p.setProperty("ssl.truststore.location","/auth/ca.crt");p.setProperty("ssl.endpoint.identification.algorithm","https");
        p.setProperty("request.timeout.ms","5000");p.setProperty("default.api.timeout.ms","10000");
        AdminClient admin=AdminClient.create(p);boolean initialized=false;long end=System.nanoTime()+TimeUnit.SECONDS.toNanos(seconds);
        try {
            while(System.nanoTime()<end) {
                long start=System.nanoTime();
                int apiMs=initialized?10000:60000;
                long queryEnd=start+TimeUnit.MILLISECONDS.toNanos(apiMs)+TimeUnit.SECONDS.toNanos(2);
                var groupFuture=admin.describeConsumerGroups(List.of(GROUP),new DescribeConsumerGroupsOptions().timeoutMs(apiMs)).all();
                var offsetFuture=admin.listConsumerGroupOffsets(GROUP,new ListConsumerGroupOffsetsOptions().timeoutMs(apiMs)).partitionsToOffsetAndMetadata();
                var g=groupFuture.get(Math.max(1,queryEnd-System.nanoTime()),TimeUnit.NANOSECONDS).get(GROUP);
                var offsets=offsetFuture.get(Math.max(1,queryEnd-System.nanoTime()),TimeUnit.NANOSECONDS);
                initialized=true;
                List<String> members=new ArrayList<>(),committed=new ArrayList<>();
                for(var member:g.members()) {
                    List<Integer> partitions=member.assignment().topicPartitions().stream().filter(tp->tp.topic().equals(TOPIC)).map(TopicPartition::partition).sorted().toList();
                    members.add("{\"memberHash\":"+q(hash(member.consumerId()))+",\"host\":"+q(member.host())+",\"partitions\":"+partitions+"}");
                }
                for(var e:offsets.entrySet())if(e.getKey().topic().equals(TOPIC))committed.add(q(Integer.toString(e.getKey().partition()))+":"+e.getValue().offset());
                System.out.println("WORKER_OWNERSHIP {\"observedAt\":"+q(Instant.now().toString())+",\"group\":"+q(GROUP)+",\"state\":"+q(g.groupState().toString())+",\"querySeconds\":"+((System.nanoTime()-start)/1e9)+",\"members\":["+String.join(",",members)+"],\"committed\":{"+String.join(",",committed)+"}}");
                System.out.flush();Thread.sleep(1000);
            }
        }catch(Exception failure){System.out.println("WORKER_OWNERSHIP {\"unavailable\":true,\"errorType\":"+q((failure.getCause()==null?failure:failure.getCause()).getClass().getSimpleName())+"}");System.out.flush();throw new IllegalStateException("Metadata unavailable; credentials withheld");}
        finally{admin.close(Duration.ofSeconds(2));}
    }
}
