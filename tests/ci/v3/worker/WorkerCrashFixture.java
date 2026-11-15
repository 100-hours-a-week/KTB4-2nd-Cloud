package com.yeodam.yeodambe.trip.service;

import com.yeodam.yeodambe.trip.client.AttachmentConversionKafkaListener;
import com.yeodam.yeodambe.trip.config.*;
import com.yeodam.yeodambe.trip.entity.AttachmentConversionTask;
import com.yeodam.yeodambe.trip.entity.AttachmentConversionTask.*;
import com.yeodam.yeodambe.trip.service.response.*;
import java.sql.*;
import java.time.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.function.*;
import org.apache.kafka.clients.admin.*;
import org.apache.kafka.clients.consumer.*;
import org.apache.kafka.clients.producer.*;
import org.springframework.boot.kafka.autoconfigure.ConcurrentKafkaListenerContainerFactoryConfigurer;
import org.springframework.kafka.config.*;
import org.springframework.kafka.core.*;
import org.springframework.messaging.handler.annotation.support.DefaultMessageHandlerMethodFactory;
import org.springframework.scheduling.concurrent.ThreadPoolTaskScheduler;
import tools.jackson.databind.json.JsonMapper;

/** Actual image Worker/Listener/Task entity, real Kafka+MySQL, synthetic persistence adapter and photo IO.
 * Deliberate JVM halt skips drain/shutdown. This is NOT production JPA/native conversion validation.
 */
public final class WorkerCrashFixture {
 static final String TASK="00000000-0000-4000-8000-000000007050",TOPIC="worker-crash-fixture",GROUP="worker-crash-fixture";
 static final Duration BACKOFF=Duration.ofMillis(100);
 static final DerivedPhotoKeys OUTPUT=new DerivedPhotoKeys("synthetic-original","synthetic-analyze","synthetic-preview");
 static String url;static boolean crash;static volatile boolean leaseBlocked;
 static void require(boolean ok,String msg){if(!ok)throw new AssertionError(msg);}
 static Connection connection()throws Exception{return DriverManager.getConnection(url,"fixture","fixture-only");}
 static int integer(String sql)throws Exception{try(var c=connection();var s=c.createStatement();var r=s.executeQuery(sql)){r.next();return r.getInt(1);}}
 static final class Store extends AttachmentConversionService {
  Store(){super(null,null,null,null);}
  @Override public synchronized AttachmentConversionClaimResult claim(String id,Kind kind,Lane lane){
   try(var c=connection()){
    c.setAutoCommit(false);String token;LocalDateTime until;boolean done;
    try(var s=c.createStatement();var r=s.executeQuery("SELECT token,lease_until,terminal FROM task WHERE id='"+TASK+"' FOR UPDATE")){r.next();token=r.getString(1);until=r.getTimestamp(2)==null?null:r.getTimestamp(2).toLocalDateTime();done=r.getBoolean(3);}
    if(done){c.commit();return new AttachmentConversionClaimResult(AttachmentConversionClaimResult.Status.ACK_TERMINAL,null,null);}
    LocalDateTime now;try(var s=c.createStatement();var r=s.executeQuery("SELECT UTC_TIMESTAMP(6)")){r.next();now=r.getTimestamp(1).toLocalDateTime();}
    var entity=new AttachmentConversionTask(TASK,Kind.INITIAL,1L,1L,Lane.LIGHT,5,50);
    if(token!=null&&until!=null){entity.claim(token,until);if(entity.isClaimOwner(token,now)){leaseBlocked=true;c.commit();return new AttachmentConversionClaimResult(AttachmentConversionClaimResult.Status.RETRY_LATER,null,null);}}
    token=UUID.randomUUID().toString();entity.claim(token,now.plusMinutes(10));
    try(var s=c.prepareStatement("UPDATE task SET token=?,lease_until=? WHERE id=?")){s.setString(1,token);s.setTimestamp(2,Timestamp.valueOf(entity.getLeaseUntil()));s.setString(3,TASK);s.executeUpdate();}
    List<AttachmentConversionClaim.Photo> photos=new ArrayList<>();
    try(var s=c.createStatement();var r=s.executeQuery("SELECT id,saved,attempt FROM item ORDER BY id")){while(r.next())photos.add(new AttachmentConversionClaim.Photo(r.getLong(1),"image/jpeg","synthetic-"+r.getInt(1),10,0,null,r.getBoolean(2)?OUTPUT:null));}
    c.commit();return new AttachmentConversionClaimResult(AttachmentConversionClaimResult.Status.CLAIMED,new AttachmentConversionClaim(id,token,kind,lane,1L,"upload","execution",1L,1L,"request",photos),null);
   }catch(Exception e){throw new IllegalStateException("Fixture claim failed");}
  }
  @Override public synchronized boolean renewLease(AttachmentConversionClaim claim){
   try(var c=connection();var s=c.prepareStatement("UPDATE task SET lease_until=UTC_TIMESTAMP(6)+INTERVAL 10 MINUTE WHERE id=? AND token=? AND terminal=FALSE AND lease_until>UTC_TIMESTAMP(6)")){s.setString(1,TASK);s.setString(2,claim.claimToken());return s.executeUpdate()==1;}
   catch(Exception e){throw new IllegalStateException("Fixture renew failed");}
  }
  @Override public synchronized AttachmentConversionAttemptResult beginPhoto(AttachmentConversionClaim c,Long id){
   try(var x=connection();var s=x.prepareStatement("UPDATE item SET attempt=attempt+1 WHERE id=? AND saved=FALSE")){s.setLong(1,id);int n=s.executeUpdate();return new AttachmentConversionAttemptResult(n==0?AttachmentConversionAttemptResult.Status.ALREADY_SAVED:AttachmentConversionAttemptResult.Status.STARTED,new AttachmentConversionClaim.Attempt(id,integer("SELECT attempt FROM item WHERE id="+id),OUTPUT),null);}
   catch(Exception e){throw new IllegalStateException("Fixture begin failed");}
  }
  @Override public synchronized AttachmentConversionStepResult completePhoto(AttachmentConversionClaim c,Long id,DerivedPhotoKeys output){
   require(renewLease(c),"Old owner must never apply result");
   try(var x=connection();var s=x.prepareStatement("UPDATE item SET saved=TRUE,applications=applications+1 WHERE id=? AND saved=FALSE")){s.setLong(1,id);s.executeUpdate();return new AttachmentConversionStepResult(AttachmentConversionStepResult.Status.CONTINUE,null);}
   catch(Exception e){throw new IllegalStateException("Fixture save failed");}
  }
  @Override public synchronized AttachmentConversionStepResult finishSubBatch(AttachmentConversionClaim c){
   try(var x=connection();var s=x.createStatement()){require(integer("SELECT COUNT(*) FROM item WHERE saved=TRUE")==5,"Missing persisted photos");s.executeUpdate("UPDATE task SET terminal=TRUE,token=NULL,lease_until=NULL");return new AttachmentConversionStepResult(AttachmentConversionStepResult.Status.ACK_READY,null);}
   catch(Exception e){throw new IllegalStateException("Fixture finish failed");}
  }
  @Override public synchronized boolean releaseClaim(AttachmentConversionClaim c){
   try(var x=connection();var s=x.prepareStatement("UPDATE task SET token=NULL,lease_until=NULL WHERE id=? AND token=?")){s.setString(1,TASK);s.setString(2,c.claimToken());return s.executeUpdate()==1;}
   catch(Exception e){throw new IllegalStateException("Fixture release failed");}
  }
  @Override public AttachmentConversionStepResult recordPhotoFailure(AttachmentConversionClaim c,Long id,boolean permanent){throw new AssertionError("Unexpected photo failure");}
 }
 static final class Photos extends TripAttachmentDerivativeService {
  final AdminClient admin;
  Photos(AdminClient admin){super(null,null,null,null,null,null,null,null,null,null);this.admin=admin;}
  @Override public CompletableFuture<DerivedPhotoKeys> processConversionPhoto(AttachmentConversionClaim c,AttachmentConversionClaim.Photo p,AttachmentConversionClaim.Attempt a,BooleanSupplier active){
   if(crash&&p.itemId()==3){try{var offsets=admin.listConsumerGroupOffsets(GROUP).partitionsToOffsetAndMetadata().get(5,TimeUnit.SECONDS);var v=offsets.get(new org.apache.kafka.common.TopicPartition(TOPIC,0));require(v==null||v.offset()==0,"Early ACK");require(integer("SELECT COUNT(*) FROM item WHERE saved=TRUE")==2,"Partial persistence required");System.out.println("WORKER_CRASH_READY noEarlyCommit=true saved=2 currentItem=3");System.out.flush();Runtime.getRuntime().halt(137);}catch(Exception e){throw new IllegalStateException("Pre-crash fixture check failed");}}
   require(active.getAsBoolean(),"Valid owner required");return CompletableFuture.completedFuture(OUTPUT);
  }
 }
 static final class Runner implements AutoCloseable {
  final KafkaListenerEndpointRegistry registry=new KafkaListenerEndpointRegistry();final ThreadPoolTaskScheduler scheduler=new ThreadPoolTaskScheduler();
  final AttachmentConversionWorker worker;final AttachmentConversionKafkaListener listener;
  Runner(String bootstrap,Store store,Photos photos)throws Exception{
   var topics=new AttachmentConversionProperties.Topics(TOPIC,TOPIC+"-ih",TOPIC+"-al",TOPIC+"-ah");worker=new AttachmentConversionWorker(store,photos,topics,BACKOFF);
   scheduler.setPoolSize(2);scheduler.initialize();var props=new AttachmentConversionProperties(null,null,null,topics,null,true,true,true);
   var producer=new DefaultKafkaProducerFactory<String,String>(Map.of("bootstrap.servers",bootstrap,"key.serializer","org.apache.kafka.common.serialization.StringSerializer","value.serializer","org.apache.kafka.common.serialization.StringSerializer"));
   listener=new AttachmentConversionKafkaListener(worker,JsonMapper.builder().build(),new KafkaTemplate<>(producer),props,TOPIC+"-dlt",BACKOFF,scheduler,registry);
   var consumers=new DefaultKafkaConsumerFactory<Object,Object>(Map.of("bootstrap.servers",bootstrap,"key.deserializer","org.apache.kafka.common.serialization.StringDeserializer","value.deserializer","org.apache.kafka.common.serialization.StringDeserializer","session.timeout.ms",6000,"heartbeat.interval.ms",1000));
   var configurer=new ConcurrentKafkaListenerContainerFactoryConfigurer(){@Override public void configure(ConcurrentKafkaListenerContainerFactory<Object,Object> f,ConsumerFactory<Object,Object> c){f.setConsumerFactory(c);}};
   var factory=new AttachmentConversionKafkaConfiguration(BACKOFF,1048576,524288,262144).attachmentConversionKafkaListenerContainerFactory(configurer,consumers,listener,scheduler,registry);factory.getContainerProperties().setPollTimeout(100);
   var handlers=new DefaultMessageHandlerMethodFactory();handlers.afterPropertiesSet();var endpoint=new MethodKafkaListenerEndpoint<Object,Object>();endpoint.setId("fixture");endpoint.setGroupId(GROUP);endpoint.setTopics(TOPIC);endpoint.setBean(listener);endpoint.setMethod(AttachmentConversionKafkaListener.class.getMethod("initialLight",ConsumerRecord.class));endpoint.setMessageHandlerMethodFactory(handlers);registry.registerListenerContainer(endpoint,factory,false);registry.start();
  }
  @Override public void close(){listener.close();worker.close();registry.stop();scheduler.shutdown();}
 }
 public static void main(String[] args)throws Exception{
  url="jdbc:mysql://db:3306/fixture?sslMode=DISABLED&allowPublicKeyRetrieval=true&serverTimezone=UTC";crash=args[0].equals("crash");String bootstrap="broker:9092";
  try(var admin=AdminClient.create(Map.of("bootstrap.servers",bootstrap))){
   if(crash){admin.createTopics(List.of(new NewTopic(TOPIC,1,(short)1))).all().get(10,TimeUnit.SECONDS);try(var producer=new KafkaProducer<String,String>(Map.of("bootstrap.servers",bootstrap,"key.serializer","org.apache.kafka.common.serialization.StringSerializer","value.serializer","org.apache.kafka.common.serialization.StringSerializer","acks","all"))){producer.send(new ProducerRecord<>(TOPIC,"trip","{\"schemaVersion\":1,\"taskId\":\""+TASK+"\"}")).get(5,TimeUnit.SECONDS);}}
   try(var runner=new Runner(bootstrap,new Store(),new Photos(admin))){
    if(!crash){long blockedDeadline=System.nanoTime()+TimeUnit.SECONDS.toNanos(35);while(!leaseBlocked&&System.nanoTime()<blockedDeadline)Thread.sleep(100);require(leaseBlocked,"No actual lease-rejection attempt observed");require(integer("SELECT COUNT(*) FROM item WHERE saved=TRUE")==2,"Unexpired lease was stolen");System.out.println("WORKER_LEASE_BLOCKED");System.out.flush();}
    long end=System.nanoTime()+TimeUnit.SECONDS.toNanos(70);while(System.nanoTime()<end&&integer("SELECT terminal FROM task")==0)Thread.sleep(100);
    require(integer("SELECT terminal FROM task")==1,"Recovery incomplete");require(integer("SELECT SUM(applications) FROM item")==5,"Duplicate result application");require(integer("SELECT attempt FROM item WHERE id=3")==2,"Interrupted item was not retried");require(integer("SELECT SUM(attempt) FROM item WHERE id IN (1,2)")==2,"Saved items reprocessed");
    System.out.println("WORKER_RECOVERED actualClasses=true actualMySQL=true syntheticPersistenceAdapter=true nativeConversion=false");
   }
  }
 }
}
