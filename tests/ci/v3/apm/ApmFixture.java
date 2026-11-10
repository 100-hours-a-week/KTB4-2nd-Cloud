import com.sun.net.httpserver.HttpServer;
import com.sun.net.httpserver.HttpHandler;
import com.sun.net.httpserver.HttpExchange;
import io.opentelemetry.api.trace.Span;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;

// Runs with the real BE JVM/API jars, without booting Spring or contacting stores.
public class ApmFixture {
  public static void main(String[] args) throws Exception {
    HttpServer server = HttpServer.create(new InetSocketAddress("0.0.0.0", 8080), 0);
    server.createContext("/fixture", new Handler());
    server.start();
    System.out.println("fixture_server_ready");
  }
  static class Handler implements HttpHandler {
    public void handle(HttpExchange exchange) throws java.io.IOException {
      try {
        var context = Span.current().getSpanContext();
        if (!context.isValid()) throw new IllegalStateException("Missing application Span context");
        String downstream = HttpClient.newHttpClient().send(
            HttpRequest.newBuilder(URI.create("http://apm-ai:8000/fixture")).GET().build(),
            HttpResponse.BodyHandlers.ofString()).body();
        String body = "{\"javaTrace\":\"" + context.getTraceId() + "\",\"ai\":" + downstream + "}";
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json");
        exchange.sendResponseHeaders(200, bytes.length);
        exchange.getResponseBody().write(bytes);
        System.out.println("{\"event\":\"apm_fixture\",\"trace_id\":\"" + context.getTraceId() + "\",\"span_id\":\"" + context.getSpanId() + "\"}");
      } catch (Exception error) {
        error.printStackTrace();
        exchange.sendResponseHeaders(500, -1);
      } finally {
        exchange.close();
      }
    }
  }
}
