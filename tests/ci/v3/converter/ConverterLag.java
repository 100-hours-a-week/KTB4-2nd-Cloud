import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;
import org.apache.kafka.clients.admin.*;
import org.apache.kafka.clients.consumer.OffsetAndMetadata;
import org.apache.kafka.common.TopicPartition;
/** Metadata only: never create topics/groups, consume records, commit/reset offsets or publish. */
public final class ConverterLag {
    static final String[] LANES={"initial-light","initial-heic","additional-light","additional-heic"};
    static String quote(String s){return "\""+s.replace("\\","\\\\").replace("\"","\\\"")+"\"";}
    public static void main(String[] args) {
        try {
            Properties p=new Properties();p.setProperty("bootstrap.servers","yeodam-v3-kafka-kafka-bootstrap.kafka.svc:9093");
            p.setProperty("security.protocol","SASL_SSL");p.setProperty("sasl.mechanism","SCRAM-SHA-512");
            String user=Files.readString(Path.of("/auth/username")).strip(),pass=Files.readString(Path.of("/auth/password")).strip();
            p.setProperty("sasl.jaas.config","org.apache.kafka.common.security.scram.ScramLoginModule required username="+quote(user)+" password="+quote(pass)+";");
            p.setProperty("ssl.truststore.type","PEM");p.setProperty("ssl.truststore.location","/auth/ca.crt");p.setProperty("ssl.endpoint.identification.algorithm","https");p.setProperty("request.timeout.ms","5000");p.setProperty("default.api.timeout.ms","10000");
            List<String> rows=new ArrayList<>();
            try(AdminClient admin=AdminClient.create(p)) {
                for(String lane:LANES) {
                    String topic="prod.yeodam.image."+lane,group="prod.yeodam.image-conversion-"+lane;
                    var description=admin.describeTopics(List.of(topic)).allTopicNames().get(12,TimeUnit.SECONDS).get(topic);
                    var consumers=admin.describeConsumerGroups(List.of(group)).all().get(12,TimeUnit.SECONDS).get(group);
                    var offsets=admin.listConsumerGroupOffsets(group).partitionsToOffsetAndMetadata().get(12,TimeUnit.SECONDS);
                    Map<TopicPartition,OffsetSpec> ends=new HashMap<>(),starts=new HashMap<>();
                    for(var part:description.partitions()){var tp=new TopicPartition(topic,part.partition());ends.put(tp,OffsetSpec.latest());starts.put(tp,OffsetSpec.earliest());}
                    var end=admin.listOffsets(ends).all().get(12,TimeUnit.SECONDS);var begin=admin.listOffsets(starts).all().get(12,TimeUnit.SECONDS);
                    long lag=0;boolean known=true;List<String> partitions=new ArrayList<>();
                    for(var part:description.partitions()) {
                        var tp=new TopicPartition(topic,part.partition());long last=end.get(tp).offset(),first=begin.get(tp).offset();var committed=offsets.get(tp);
                        long at=committed==null?-1:committed.offset();
                        if(at<0&&first==0&&last==0)at=0; // Proven empty new partition only.
                        if(at<first||at>last)known=false;else lag+=last-at;
                        partitions.add("{\"partition\":"+part.partition()+",\"replicas\":"+part.replicas().size()+",\"isr\":"+part.isr().size()+",\"begin\":"+first+",\"end\":"+last+",\"committed\":"+at+"}");
                    }
                    var assigned=new HashSet<TopicPartition>();boolean unique=true;
                    for(var member:consumers.members())for(var tp:member.assignment().topicPartitions())if(!assigned.add(tp))unique=false;
                    rows.add("{\"lane\":"+quote(lane)+",\"state\":"+quote(consumers.groupState().toString())+",\"members\":"+consumers.members().size()+",\"assigned\":"+assigned.size()+",\"uniqueAssignments\":"+unique+",\"lagKnown\":"+known+",\"lag\":"+lag+",\"partitions\":["+String.join(",",partitions)+"]}");
                }
            }
            System.out.println("CONVERTER_LAG {\"schemaVersion\":1,\"groups\":["+String.join(",",rows)+"]}");
        }catch(Exception failure){System.err.println("Read-only Kafka metadata probe failed; credential details withheld");System.exit(1);}
    }
}
