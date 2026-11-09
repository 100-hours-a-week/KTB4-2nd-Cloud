import java.util.*;
import java.io.*;
import java.time.*;
import java.nio.file.*;
import java.util.concurrent.TimeUnit;
import org.apache.kafka.clients.admin.*;
import org.apache.kafka.common.acl.*;
import org.apache.kafka.common.resource.*;
import org.apache.kafka.clients.consumer.*;
import org.apache.kafka.common.*;
import org.apache.kafka.common.header.*;
public class OutboxWire {
  static String q(String s) { return "\""+s.replace("\\","\\\\").replace("\"","\\\"")+"\""; }
  static String b(byte[] v) { return Base64.getEncoder().encodeToString(v==null ? new byte[0] : v); }
  static void setup(String properties) throws Exception {
    Properties p=new Properties();try(var r=Files.newInputStream(Path.of(properties))){p.load(r);}p.put("bootstrap.servers","broker1:9093");
    String[] topics={"prod.yeodam.trip-create.request","prod.yeodam.trip-update.request","prod.yeodam.story-create.request","prod.yeodam.image.initial-light","prod.yeodam.image.initial-heic","prod.yeodam.image.additional-light","prod.yeodam.image.additional-heic","prod.yeodam.schema-history","__debezium-heartbeat.v3.yeodam.outbox"};
    var created=new ArrayList<NewTopic>();var acls=new ArrayList<AclBinding>();
    for(String t:topics) {
      boolean history=t.endsWith("schema-history"), heartbeat=t.startsWith("__debezium-heartbeat.");
      created.add(new NewTopic(t,1,(short)3).configs(heartbeat ? Map.of("min.insync.replicas","2","cleanup.policy","compact,delete","retention.ms","86400000","retention.bytes","1048576","segment.bytes","1048576","segment.ms","3600000") : history ? Map.of("min.insync.replicas","2","cleanup.policy","delete","retention.ms","-1","retention.bytes","-1","segment.bytes","1048576") : Map.of("min.insync.replicas","2","message.timestamp.type","LogAppendTime","max.message.bytes","262144")));
      var operations=history ? List.of(AclOperation.READ,AclOperation.WRITE,AclOperation.DESCRIBE,AclOperation.DESCRIBE_CONFIGS) : List.of(AclOperation.WRITE,AclOperation.DESCRIBE);
      for(var op:operations)acls.add(new AclBinding(new ResourcePattern(ResourceType.TOPIC,t,PatternType.LITERAL),new AccessControlEntry("User:v3-connect-outbox","*",op,AclPermissionType.ALLOW)));
    }
    acls.add(new AclBinding(new ResourcePattern(ResourceType.GROUP,"v3.yeodam.outbox-schemahistory",PatternType.LITERAL),new AccessControlEntry("User:v3-connect-outbox","*",AclOperation.READ,AclPermissionType.ALLOW)));
    try(var admin=Admin.create(p)) {admin.createTopics(created).all().get(60,TimeUnit.SECONDS);admin.createAcls(acls).all().get(60,TimeUnit.SECONDS);}
    System.out.println("PASS: synthetic business7/history1 RF3/minISR2 and literal Source ACLs");
  }
  public static void main(String[] args) throws Exception {
    if(args[0].equals("--setup")){setup(args[1]);return;}
    Properties p=new Properties();try(var r=Files.newInputStream(Path.of(args[0]))){p.load(r);}
    p.put("bootstrap.servers","broker1:9093");p.put("enable.auto.commit","false");p.put("auto.offset.reset","earliest");
    p.put("key.deserializer","org.apache.kafka.common.serialization.ByteArrayDeserializer");
    p.put("value.deserializer","org.apache.kafka.common.serialization.ByteArrayDeserializer");
    try(var c=new KafkaConsumer<byte[],byte[]>(p)) {
      var parts=new ArrayList<TopicPartition>();for(int i=1;i<args.length;i++)parts.add(new TopicPartition(args[i],0));
      c.assign(parts);c.seekToBeginning(parts);var ends=c.endOffsets(parts);long deadline=System.currentTimeMillis()+20000;
      while(System.currentTimeMillis()<deadline) {
        boolean done=true;for(var t:parts)if(c.position(t)<ends.get(t))done=false;if(done)break;
        for(var r:c.poll(Duration.ofMillis(250))) {
          var h=new LinkedHashMap<String,String>();for(Header x:r.headers())h.put(x.key(),b(x.value()));
          var fields=new ArrayList<String>();for(var x:h.entrySet())fields.add(q(x.getKey())+":"+q(x.getValue()));
          System.out.println("OUTBOX_RECORD {\"topic\":"+q(r.topic())+",\"key\":"+q(b(r.key()))+",\"value\":"+q(b(r.value()))+",\"headers\":{"+String.join(",",fields)+"}}");
        }
      }
      for(var t:parts)if(c.position(t)<ends.get(t))throw new IllegalStateException("Consumer did not reach observed end offset");
    }
  }
}
