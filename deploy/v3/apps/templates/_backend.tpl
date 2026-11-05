{{- define "yeodam-v3-apps.resolveConfig" -}}
{{- $c := mergeOverwrite (deepCopy .root.Values.defaults) .workload -}}
{{- if and $c.enabled (not .root.Values.backendContract) (not (empty $c.springProperties)) }}{{ fail "springProperties requires the BE runtime contract" }}{{ end -}}
{{- if and .root.Values.backendContract $c.enabled -}}
{{- if hasKey $c.configMapData "SPRING_APPLICATION_JSON" }}{{ fail "Use structured springProperties; raw SPRING_APPLICATION_JSON can shadow role settings" }}{{ end -}}
{{- if not (kindIs "map" $c.springProperties) }}{{ fail "springProperties must be a structured mapping" }}{{ end -}}
{{- $_ := set $c.configMapData "SPRING_APPLICATION_JSON" (mustToJson $c.springProperties) -}}
{{- end -}}
{{- toYaml $c -}}
{{- end -}}

{{- define "yeodam-v3-apps.backendCheck" -}}
{{- $c := .config -}}
{{- $contract := .root.Files.Get "files/backend-contract.json" | fromJson -}}
{{- if ne $c.name "backend" }}{{ fail "The backend runtime profile only deploys full BE copies" }}{{ end -}}
{{- if ne (int $c.replicas) 1 }}{{ fail "Initial BE runtime profile uses one replica per Deployment" }}{{ end -}}
{{- if or $c.environment $c.configMapRefs }}{{ fail "BE settings must use the generated ConfigMap and explicit secretRefs" }}{{ end -}}
{{- range $key := concat $contract.requiredConfig $contract.topicConfig $contract.conversionConfig $contract.conversionTopicConfig -}}
{{- $_ := required (printf "BE ConfigMap requires %s" $key) (index $c.configMapData $key) -}}
{{- end -}}
{{- $allowed := concat $contract.requiredConfig $contract.topicConfig $contract.conversionConfig $contract.conversionTopicConfig $contract.optionalConfig (list "SPRING_APPLICATION_JSON") -}}
{{- range $key, $_ := $c.configMapData -}}
{{- if not (has $key $allowed) }}{{ fail (printf "Unreviewed BE ConfigMap key %s" $key) }}{{ end -}}
{{- end -}}
{{- if ne $c.configMapData.JAVA_TOOL_OPTIONS $contract.javaToolOptions }}{{ fail "Use reviewed heap settings without JVM role-property overrides" }}{{ end -}}
{{- if ne (len $c.secretRefs) (len $contract.requiredSecrets) }}{{ fail "Only reviewed BE Secret bindings may inject environment values" }}{{ end -}}
{{- range $key := $contract.requiredSecrets -}}
{{- $ref := required (printf "BE requires Secret reference %s" $key) (index $c.secretRefs $key) -}}
{{- if or (ne $ref.name "backend-credentials") (ne $ref.key $key) }}{{ fail "BE Secret references must match backend-credentials keys" }}{{ end -}}
{{- end -}}
{{- if hasKey $c.secretRefs "SPRING_MONGODB_URI" }}{{ fail "Use MONGODB_URI once; do not shadow it with a second Mongo binding" }}{{ end -}}
{{- if ne $c.configMapData.SPRING_PROFILES_ACTIVE "prod,internal-metrics" }}{{ fail "Use prod,internal-metrics for BE" }}{{ end -}}
{{- if ne $c.configMapData.MANAGEMENT_SERVER_ADDRESS "0.0.0.0" }}{{ fail "Kubelet requires a reachable management listener" }}{{ end -}}
{{- range $probe := list "startup" "liveness" "readiness" -}}
{{- $p := index $c.probes $probe -}}
{{- $path := "/actuator/health/liveness" -}}
{{- if eq $probe "readiness" }}{{ $path = "/actuator/health/readiness" }}{{ end -}}
{{- if or (ne $p.httpGet.path $path) (ne $p.httpGet.port "management") }}{{ fail "BE Probe must use the dedicated management endpoint" }}{{ end -}}
{{- end -}}
{{- if lt (int $c.terminationGracePeriodSeconds) (int $contract.minimumTerminationGraceSeconds) }}{{ fail "Keep at least 180 seconds for BE worker drain and shutdown" }}{{ end -}}
{{- $kafka := .root.Files.Get "files/business-contract.json" | fromJson -}}
{{- range $key, $expected := $kafka.backendEnvironment -}}
{{- if ne (index $c.configMapData $key) $expected }}{{ fail (printf "BE Topic/Group %s must match the shared Kafka contract" $key) }}{{ end -}}
{{- end -}}
{{- range $key, $expected := $kafka.backendSpringGroups -}}
{{- $property := "result-consumer-group" -}}
{{- if eq $key "outbox" }}{{ $property = "receipt-consumer-group" }}{{ end -}}
{{- if ne (dig "app" $key $property "" $c.springProperties) $expected }}{{ fail "BE structured consumer group must match the shared Kafka contract" }}{{ end -}}
{{- end -}}
{{- if ne $c.configMapData.ATTACHMENT_CONVERSION_MODE "KAFKA" }}{{ fail "Every V3 BE role must keep KAFKA mode, including non-consumers" }}{{ end -}}
{{- $roleName := required "Unknown BE workload role" (index $contract.workloadRoles .instance) -}}
{{- $role := index $contract.roleProfiles $roleName -}}
{{- range $key, $expected := $role.environment -}}
{{- if ne (index $c.configMapData $key) $expected }}{{ fail (printf "%s: invalid role flag %s" $.instance $key) }}{{ end -}}
{{- end -}}
{{- $spring := $c.springProperties -}}
{{- if ne (dig "app" "trip" "result-listener-enabled" nil $spring) $role.tripResultListener }}{{ fail "Trip result Listener does not match workload role" }}{{ end -}}
{{- if ne (dig "app" "outbox" "receipt-listener-enabled" nil $spring) $role.outboxReceiptListener }}{{ fail "Outbox receipt Listener does not match workload role" }}{{ end -}}
{{- if ne (dig "app" "attachment-conversion" "operations" "enabled" nil $spring) false }}{{ fail "Operations runner must stay disabled" }}{{ end -}}
{{- $_ := required "KAFKA mode requires admission even when consumers are disabled" (dig "app" "attachment-conversion" "admission" nil $spring) -}}
{{- end -}}
