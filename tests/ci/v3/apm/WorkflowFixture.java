import com.sun.net.httpserver.HttpServer;
import com.sun.net.httpserver.HttpExchange;
import com.yeodam.yeodambe.integration.service.WorkflowTraceService;
import com.yeodam.yeodambe.integration.service.response.WorkflowTraceCarrier;
import io.opentelemetry.api.trace.Span;
import org.apache.kafka.common.header.internals.RecordHeaders;
import java.net.*;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.Executors;

// Exact production WorkflowTraceService; DB/native image work is synthetic.
public class WorkflowFixture {
  static final WorkflowTraceService traces = new WorkflowTraceService();
  static final HttpClient client = HttpClient.newBuilder().connectTimeout(java.time.Duration.ofSeconds(2)).build();
  public static void main(String[] args) throws Exception {
    var server=HttpServer.create(new InetSocketAddress("0.0.0.0",8080),0);
    server.setExecutor(Executors.newFixedThreadPool(4));
    String role=args[0];
    server.createContext("/ready", e->respond(e,200,"ready"));
    server.createContext("/workflow", e->handle(e,role));
    server.createContext("/result", e->{
      try {var span=traces.startResultApplication(traces.capture());
        try(var scope=traces.makeCurrent(span)){respond(e,200,"{\"applied\":true}");}
        finally{traces.finish(span,"success");}
      }catch(Exception error){respond(e,500,"result_failed");}
    });
    server.createContext("/jdbc", e->{
      try(var connection=java.sql.DriverManager.getConnection(System.getenv("FIXTURE_MYSQL_URL"),"root","synthetic-only");var statement=connection.createStatement();var result=statement.executeQuery("SELECT 1")){
        if(!result.next()||result.getInt(1)!=1)throw new IllegalStateException("Synthetic query failed");
        respond(e,200,"{\"jdbc\":true}");
      }catch(Exception failure){System.err.println("Synthetic JDBC: "+failure.getClass().getSimpleName()+": "+failure.getMessage());respond(e,503,"jdbc_not_ready");}
    });
    server.start();
  }
  static void handle(HttpExchange e,String role)throws java.io.IOException {
    try {
      WorkflowTraceCarrier parent=traces.capture();
      if(!Span.current().getSpanContext().isValid())throw new IllegalStateException("Missing automatic HTTP Context");
      var headers=new RecordHeaders();headers.add("traceparent",parent.traceparent().getBytes(StandardCharsets.UTF_8));
      headers.add("tracestate",null); // Actual Debezium optional-column wire shape.
      Span span=role.equals("convert")?traces.startConversion(traces.fromHeaders(headers)):traces.startProcessDispatch();
      if(!span.isRecording())throw new IllegalStateException("Business span is not recording");
      try(var scope=traces.makeCurrent(span)) {
        String url=role.equals("convert")?"http://workflow-worker:8080/workflow":"http://workflow-ai:8000/workflow";
        var response=client.send(HttpRequest.newBuilder(URI.create(url)).timeout(java.time.Duration.ofSeconds(15)).GET().build(),HttpResponse.BodyHandlers.ofString());
        if(response.statusCode()!=200)throw new IllegalStateException("Synthetic downstream failed");
        respond(e,200,"{\"completed\":true}");
      } finally {traces.finish(span,"success");}
    }catch(Exception error){respond(e,500,"workflow_failed");}
  }
  static void respond(HttpExchange e,int code,String value)throws java.io.IOException {
    byte[] b=value.getBytes(StandardCharsets.UTF_8);e.getResponseHeaders().set("Content-Type","application/json");e.sendResponseHeaders(code,b.length);e.getResponseBody().write(b);e.close();
  }
}
