{{- define "yeodam-v3-apps.backendCheck" -}}
{{- $c := .config -}}
{{- $contract := .root.Files.Get "files/backend-contract.json" | fromJson -}}
{{- if ne $c.name "backend" }}{{ fail "The backend runtime profile only deploys full BE copies" }}{{ end -}}
{{- if ne (int $c.replicas) 1 }}{{ fail "Initial BE runtime profile uses one replica per Deployment" }}{{ end -}}
{{- if or $c.environment $c.configMapRefs }}{{ fail "BE settings must use the generated ConfigMap and explicit secretRefs" }}{{ end -}}
{{- range $key := concat $contract.requiredConfig $contract.topicConfig -}}
{{- $_ := required (printf "BE ConfigMap requires %s" $key) (index $c.configMapData $key) -}}
{{- end -}}
{{- range $key := $contract.requiredSecrets -}}
{{- $_ := required (printf "BE requires Secret reference %s" $key) (index $c.secretRefs $key) -}}
{{- end -}}
{{- if ne $c.configMapData.SPRING_PROFILES_ACTIVE "prod,internal-metrics" }}{{ fail "Use prod,internal-metrics for BE" }}{{ end -}}
{{- if ne $c.configMapData.MANAGEMENT_SERVER_ADDRESS "0.0.0.0" }}{{ fail "Kubelet requires a reachable management listener" }}{{ end -}}
{{- range $probe := list "startup" "liveness" "readiness" -}}
{{- $p := index $c.probes $probe -}}
{{- $path := "/actuator/health/liveness" -}}
{{- if eq $probe "readiness" }}{{ $path = "/actuator/health/readiness" }}{{ end -}}
{{- if or (ne $p.httpGet.path $path) (ne $p.httpGet.port "management") }}{{ fail "BE Probe must use the dedicated management endpoint" }}{{ end -}}
{{- end -}}
{{- range $key := $contract.topicConfig -}}
{{- if not (hasPrefix $contract.topicPrefix (index $c.configMapData $key)) }}{{ fail "Use V3-specific AI Topic names" }}{{ end -}}
{{- end -}}
{{- $spring := $c.configMapData.SPRING_APPLICATION_JSON | mustFromJson -}}
{{- if ne $c.configMapData.ATTACHMENT_CONVERSION_MODE "DB" -}}
{{- if ne $c.configMapData.ATTACHMENT_CONVERSION_MODE "KAFKA" }}{{ fail "Only explicit DB or KAFKA conversion modes are supported" }}{{ end -}}
{{- range $key := concat $contract.conversionConfig $contract.conversionTopicConfig -}}
{{- $_ := required (printf "KAFKA conversion requires %s" $key) (index $c.configMapData $key) -}}
{{- end -}}
{{- $_ := required "KAFKA conversion requires structured admission settings" (dig "app" "attachment-conversion" "admission" nil $spring) -}}
{{- end -}}
{{- end -}}
