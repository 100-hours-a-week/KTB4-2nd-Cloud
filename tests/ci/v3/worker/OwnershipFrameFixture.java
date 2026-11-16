import java.nio.file.*;
import java.util.concurrent.atomic.AtomicReference;
/** Local file transport only: no Kafka/AdminClient/model/production connection. */
public final class OwnershipFrameFixture {
    public static void main(String[] args)throws Exception {
        Path dir=Files.createTempDirectory("ownership-frame-");Path file=dir.resolve("frame.json");
        var output=System.out;System.setOut(new java.io.PrintStream(java.io.OutputStream.nullOutputStream()));
        String healthy="{\"observedAt\":\"2026-11-16T00:00:00Z\",\"state\":\"Stable\"}";
        String failed="{\"unavailable\":true,\"errorType\":\"TimeoutException\"}";
        AtomicReference<Throwable> error=new AtomicReference<>();
        try {
            ConverterOwnership.publish(file,healthy);
            Thread writer=new Thread(()->{
                try {for(int i=0;i<100;i++)ConverterOwnership.publish(file,i%2==0?failed:healthy);}
                catch(Throwable t){error.set(t);}
            });writer.start();int reads=0;
            do {
                String frame=Files.readString(file);
                if(!frame.equals(healthy+"\n")&&!frame.equals(failed+"\n"))throw new AssertionError("Torn frame");
                reads++;
            }while(writer.isAlive());writer.join();
            if(error.get()!=null)throw new AssertionError("Writer failed",error.get());
            ConverterOwnership.publish(file,failed);
            if(!Files.readString(file).equals(failed+"\n"))throw new AssertionError("Stale healthy frame");
            if(!Files.getPosixFilePermissions(file).equals(java.nio.file.attribute.PosixFilePermissions.fromString("rw-------")))throw new AssertionError("Wrong frame permissions");
            output.println("{\"atomicFrameVerified\":true,\"unavailableReplacedHealthy\":true,\"reads\":"+reads+"}");
        }finally{System.setOut(output);Files.deleteIfExists(file);Files.deleteIfExists(dir.resolve("frame.json.pending"));Files.delete(dir);}
    }
}
