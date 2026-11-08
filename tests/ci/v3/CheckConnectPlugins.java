import javax.script.*;
import java.util.*;
public class CheckConnectPlugins {
  public static void main(String[] args) throws Exception {
    Class.forName("io.debezium.connector.mysql.MySqlConnector").getConstructor().newInstance();
    Class.forName("io.debezium.transforms.outbox.EventRouter").getConstructor().newInstance();
    Class.forName("io.debezium.transforms.Filter").getConstructor().newInstance();
    ScriptEngine engine = new ScriptEngineManager().getEngineByName("groovy");
    if (engine == null) throw new IllegalStateException("Groovy JSR223 missing");
    String expression = "(value.op == 'c' || value.op == 'r') && (value.after.cdc_managed == true || value.after.cdc_managed == 1) && value.after.status == 'PENDING'";
    for (String op : List.of("c", "r", "u", "d")) {
      for (boolean managed : List.of(true, false)) {
        for (String status : List.of("PENDING", "PUBLISHED")) {
          engine.put("value", Map.of("op",op,"after",Map.of("cdc_managed",managed,"status",status)));
          boolean expected = (op.equals("c") || op.equals("r")) && managed && status.equals("PENDING");
          if (!Objects.equals(engine.eval(expression), expected)) throw new AssertionError("Filter behavior differs");
        }
      }
    }
    System.out.println("PASS: MySQL Connector, Outbox EventRouter, scripting Filter and Groovy engine/filter conditions");
  }
}
