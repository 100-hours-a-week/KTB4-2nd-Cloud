package com.yeodam.yeodambe.trip.service;

import com.yeodam.yeodambe.trip.client.AttachmentConversionKafkaListener;
import com.yeodam.yeodambe.trip.config.*;
import com.yeodam.yeodambe.trip.entity.AttachmentConversionTask.*;
import com.yeodam.yeodambe.trip.service.response.*;
import java.time.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.function.*;
import org.apache.kafka.clients.admin.*;
import org.apache.kafka.clients.consumer.*;
import org.apache.kafka.clients.producer.*;
import org.apache.kafka.common.TopicPartition;
import org.springframework.boot.kafka.autoconfigure.ConcurrentKafkaListenerContainerFactoryConfigurer;
import org.springframework.kafka.config.*;
import org.springframework.kafka.core.*;
import org.springframework.messaging.handler.annotation.support.DefaultMessageHandlerMethodFactory;
import org.springframework.scheduling.concurrent.ThreadPoolTaskScheduler;
import tools.jackson.databind.json.JsonMapper;

/** Actual image Worker/Listener/factory; only persistence and image IO are synthetic. */
public final class ConverterScalingFixture {
    static String bootstrap, topic, group;
    static final Duration BACKOFF=Duration.ofMillis(100);
    static final DerivedPhotoKeys OUTPUT=new DerivedPhotoKeys("synthetic-original","synthetic-analyze","synthetic-preview");
    static void require(boolean ok,String message) { if(!ok)throw new AssertionError(message); }
    static void until(BooleanSupplier check) throws Exception {
        long end=System.nanoTime()+TimeUnit.SECONDS.toNanos(30);
        while(!check.getAsBoolean()&&System.nanoTime()<end)Thread.sleep(50);
        require(check.getAsBoolean(),"Synthetic condition deadline exceeded");
    }
    static final class Task {
        final String id=UUID.randomUUID().toString();
        final Map<Long,DerivedPhotoKeys> saved=new HashMap<>();
        String token; boolean terminal; int applications;
    }
    static final class Store extends AttachmentConversionService {
        final Map<String,Task> tasks=new ConcurrentHashMap<>();
        Store(){super(null,null,null,null);}
        Task add(){Task t=new Task();tasks.put(t.id,t);return t;}
        @Override public synchronized AttachmentConversionClaimResult claim(String id,Kind kind,Lane lane) {
            Task t=tasks.get(id);require(t!=null,"Unknown synthetic task");
            if(t.terminal)return new AttachmentConversionClaimResult(AttachmentConversionClaimResult.Status.ACK_TERMINAL,null,null);
            if(t.token!=null)return new AttachmentConversionClaimResult(AttachmentConversionClaimResult.Status.RETRY_LATER,null,null);
            t.token=UUID.randomUUID().toString();
            var photos=List.of(1L,2L).stream().map(i->new AttachmentConversionClaim.Photo(i,"image/jpeg","synthetic-"+i,10,0,null,t.saved.get(i))).toList();
            var claim=new AttachmentConversionClaim(id,t.token,kind,lane,1L,"upload","execution",1L,1L,"request",photos);
            return new AttachmentConversionClaimResult(AttachmentConversionClaimResult.Status.CLAIMED,claim,null);
        }
        @Override public synchronized boolean renewLease(AttachmentConversionClaim c){return Objects.equals(tasks.get(c.taskId()).token,c.claimToken());}
        @Override public synchronized AttachmentConversionAttemptResult beginPhoto(AttachmentConversionClaim c,Long id){
            return new AttachmentConversionAttemptResult(tasks.get(c.taskId()).saved.containsKey(id)?AttachmentConversionAttemptResult.Status.ALREADY_SAVED:AttachmentConversionAttemptResult.Status.STARTED,new AttachmentConversionClaim.Attempt(id,1,OUTPUT),null);
        }
        @Override public synchronized AttachmentConversionStepResult completePhoto(AttachmentConversionClaim c,Long id,DerivedPhotoKeys output){
            Task t=tasks.get(c.taskId());require(renewLease(c),"Stale result must not apply");
            if(t.saved.putIfAbsent(id,output)==null)t.applications++;
            return new AttachmentConversionStepResult(AttachmentConversionStepResult.Status.CONTINUE,null);
        }
        @Override public synchronized AttachmentConversionStepResult finishSubBatch(AttachmentConversionClaim c){
            Task t=tasks.get(c.taskId());require(t.saved.size()==2,"Incomplete task must not finish");t.terminal=true;t.token=null;
            return new AttachmentConversionStepResult(AttachmentConversionStepResult.Status.ACK_READY,null);
        }
        @Override public synchronized boolean releaseClaim(AttachmentConversionClaim c){Task t=tasks.get(c.taskId());if(Objects.equals(t.token,c.claimToken()))t.token=null;return true;}
        @Override public AttachmentConversionStepResult recordPhotoFailure(AttachmentConversionClaim c,Long id,boolean permanent){throw new AssertionError("Unexpected business failure");}
        synchronized boolean done(Task t){return t.terminal;}
        synchronized int applications(Task t){return t.applications;}
    }
    static final class Photos extends TripAttachmentDerivativeService {
        volatile boolean block; volatile CompletableFuture<DerivedPhotoKeys> pending;
        Photos(){super(null,null,null,null,null,null,null,null,null,null);}
        @Override public CompletableFuture<DerivedPhotoKeys> processConversionPhoto(AttachmentConversionClaim c,AttachmentConversionClaim.Photo p,AttachmentConversionClaim.Attempt a,BooleanSupplier active){
            if(block){block=false;pending=new CompletableFuture<>();return pending;}
            return CompletableFuture.completedFuture(OUTPUT);
        }
    }
    static final class Runner implements AutoCloseable {
        final KafkaListenerEndpointRegistry registry=new KafkaListenerEndpointRegistry();
        final ThreadPoolTaskScheduler scheduler=new ThreadPoolTaskScheduler();
        final AttachmentConversionWorker worker;
        final AttachmentConversionKafkaListener listener;
        Runner(Store store,Photos photos) throws Exception {
            var topics=new AttachmentConversionProperties.Topics(topic,topic+"-ih",topic+"-al",topic+"-ah");
            worker=new AttachmentConversionWorker(store,photos,topics,BACKOFF);
            scheduler.setPoolSize(2);scheduler.initialize();
            var props=new AttachmentConversionProperties(null,null,null,topics,null,true,true,true);
            var producer=new DefaultKafkaProducerFactory<String,String>(Map.of("bootstrap.servers",bootstrap,"key.serializer","org.apache.kafka.common.serialization.StringSerializer","value.serializer","org.apache.kafka.common.serialization.StringSerializer"));
            listener=new AttachmentConversionKafkaListener(worker,JsonMapper.builder().build(),new KafkaTemplate<>(producer),props,topic+"-dlt",BACKOFF,scheduler,registry);
            var consumers=new DefaultKafkaConsumerFactory<Object,Object>(Map.of("bootstrap.servers",bootstrap,"key.deserializer","org.apache.kafka.common.serialization.StringDeserializer","value.deserializer","org.apache.kafka.common.serialization.StringDeserializer","session.timeout.ms",6000,"heartbeat.interval.ms",1000));
            var configurer=new ConcurrentKafkaListenerContainerFactoryConfigurer(){@Override public void configure(ConcurrentKafkaListenerContainerFactory<Object,Object> f,ConsumerFactory<Object,Object> c){f.setConsumerFactory(c);}};
            var factory=new AttachmentConversionKafkaConfiguration(BACKOFF,1048576,524288,262144).attachmentConversionKafkaListenerContainerFactory(configurer,consumers,listener,scheduler,registry);
            factory.getContainerProperties().setPollTimeout(100);
            var handlers=new DefaultMessageHandlerMethodFactory();handlers.afterPropertiesSet();
            var endpoint=new MethodKafkaListenerEndpoint<Object,Object>();endpoint.setId(UUID.randomUUID().toString());endpoint.setGroupId(group);endpoint.setTopics(topic);endpoint.setBean(listener);endpoint.setMethod(AttachmentConversionKafkaListener.class.getMethod("initialLight",ConsumerRecord.class));endpoint.setMessageHandlerMethodFactory(handlers);
            registry.registerListenerContainer(endpoint,factory,false);registry.start();
        }
        Set<TopicPartition> assignments(){var result=new HashSet<TopicPartition>();for(var c:registry.getListenerContainers())if(c.getAssignedPartitions()!=null)result.addAll(c.getAssignedPartitions());return result;}
        @Override public void close(){listener.close();worker.close();registry.stop();scheduler.shutdown();}
    }
    static long offset(AdminClient admin,int partition)throws Exception {
        var offsets=admin.listConsumerGroupOffsets(group).partitionsToOffsetAndMetadata().get(5,TimeUnit.SECONDS);
        var v=offsets.get(new TopicPartition(topic,partition));return v==null?0:v.offset();
    }
    public static void main(String[] args)throws Exception {
        bootstrap=args[0];topic="fixture-conversion-"+UUID.randomUUID();group=topic+"-workers";
        var store=new Store();var firstPhotos=new Photos();var secondPhotos=new Photos();
        var producerProps=Map.<String,Object>of("bootstrap.servers",bootstrap,"key.serializer","org.apache.kafka.common.serialization.StringSerializer","value.serializer","org.apache.kafka.common.serialization.StringSerializer","acks","all");
        try(var admin=AdminClient.create(Map.of("bootstrap.servers",bootstrap));var producer=new KafkaProducer<String,String>(producerProps)) {
            admin.createTopics(List.of(new NewTopic(topic,2,(short)1))).all().get(10,TimeUnit.SECONDS);
            Runner first=new Runner(store,firstPhotos);Runner second=null;
            try {
                until(()->first.assignments().size()==2);
                second=new Runner(store,secondPhotos);Runner other=second;
                until(()->first.assignments().size()==1&&other.assignments().size()==1);
                int victimPartition=first.assignments().iterator().next().partition();int survivorPartition=other.assignments().iterator().next().partition();
                Task paused=store.add(),parallel=store.add();firstPhotos.block=true;
                var sent=producer.send(new ProducerRecord<>(topic,victimPartition,"travel-a","{\"schemaVersion\":1,\"taskId\":\""+paused.id+"\"}")).get(5,TimeUnit.SECONDS);
                producer.send(new ProducerRecord<>(topic,survivorPartition,"travel-b","{\"schemaVersion\":1,\"taskId\":\""+parallel.id+"\"}")).get(5,TimeUnit.SECONDS);
                until(()->firstPhotos.pending!=null&&store.done(parallel));
                require(offset(admin,victimPartition)==sent.offset(),"In-flight work was committed early");
                var stopped=CompletableFuture.runAsync(first::close);
                Thread.sleep(200);require(!stopped.isDone(),"Shutdown did not wait for in-flight photo");
                firstPhotos.pending.complete(OUTPUT);stopped.get(15,TimeUnit.SECONDS);
                until(()->other.assignments().size()==2&&store.done(paused));
                until(()->{try{return offset(admin,victimPartition)==sent.offset()+1;}catch(Exception e){return false;}});
                require(store.applications(paused)==2&&store.applications(parallel)==2,"Duplicate or missing photo result");
                // Broker redelivery of a completed task must ACK without applying the result again.
                var duplicate=producer.send(new ProducerRecord<>(topic,victimPartition,"travel-a","{\"schemaVersion\":1,\"taskId\":\""+paused.id+"\"}")).get(5,TimeUnit.SECONDS);
                until(()->{try{return offset(admin,victimPartition)==duplicate.offset()+1;}catch(Exception e){return false;}});
                require(store.applications(paused)==2,"Duplicate task applied twice");
                System.out.println("CONVERTER_PROOF {\"schemaVersion\":1,\"twoPartitions\":true,\"twoConsumersAssigned\":true,\"parallelProgress\":true,\"noEarlyCommit\":true,\"inflightDrain\":true,\"redeliveryCompleted\":true,\"duplicateResultPrevented\":true,\"syntheticPersistence\":true,\"nativeConversionTested\":false}");
            }finally {if(second!=null)second.close();first.close();}
        }
    }
}
