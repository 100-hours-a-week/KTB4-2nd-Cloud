{{- define "yeodam-v3-apps.websocketCheck" -}}
{{- $c := .config -}}
{{- if not (regexMatch "^ghcr.io/100-hours-a-week/yeodam-websocket@sha256:[a-f0-9]{64}$" .root.Values.images.websocket) }}{{ fail "Use the separate immutable WebSocket image" }}{{ end -}}
{{- $contract := .root.Files.Get "files/websocket-contract.json" | fromJson -}}
{{- if or (ne .instance "trip-websocket") (ne $c.name "websocket") (ne (int $c.replicas) 1) }}{{ fail "WebSocket profile deploys only one trip-websocket" }}{{ end -}}
{{- if or (ne (toJson $c.ports) (toJson (dict "http" 8081))) (ne (toJson $c.service) (toJson (dict "enabled" true "port" 8081))) }}{{ fail "WebSocket exposes only its internal HTTP8081 Service" }}{{ end -}}
{{- if or $c.environment $c.configMapRefs }}{{ fail "WebSocket uses explicit ConfigMap and Secret references" }}{{ end -}}
{{- range $key, $v := $contract.config }}
{{- if ne (index $c.configMapData $key) $v }}{{ fail (printf "WebSocket runtime mismatch: %s" $key) }}{{ end -}}
{{- end }}
{{- if ne (len $c.configMapData) (int (add (len $contract.config) 3)) }}{{ fail "Unreviewed WebSocket environment key" }}{{ end -}}
{{- if not (regexMatch "^[a-f0-9]{40}$" $c.configMapData.RELEASE) }}{{ fail "WebSocket source commit required" }}{{ end -}}
{{- if not (regexMatch "^https://[A-Za-z0-9.-]+(:[0-9]+)?$" $c.configMapData.WEBSOCKET_ALLOWED_ORIGINS) }}{{ fail "Explicit HTTPS WebSocket Origin required" }}{{ end -}}
{{- if not (regexMatch "^[a-f0-9]{64}$" $c.configurationRevision) }}{{ fail "WebSocket configuration metadata revision required" }}{{ end -}}
{{- if ne (toJson $c.springProperties) (toJson $contract.spring) }}{{ fail "WebSocket requires reviewed Sentinel/TLS properties" }}{{ end -}}
{{- if ne (toJson $c.secretRefs) (toJson $contract.secrets) }}{{ fail "WebSocket uses three selected Secret keys only" }}{{ end -}}
{{- if ne (toJson $c.secretVolumes) (toJson $contract.secretVolumes) }}{{ fail "WebSocket mounts only public Redis trust" }}{{ end -}}
{{- $resources := deepCopy $contract.resources }}{{- $_ := set $resources.limits "cpu" "" }}
{{- if ne (toJson $c.resources) (toJson $resources) }}{{ fail "Keep the initial WebSocket resource budget" }}{{ end -}}
{{- if or (ne (int $c.terminationGracePeriodSeconds) 60) (ne (int $c.preStopDelaySeconds) 5) (ne $c.scratchSizeLimit "64Mi") }}{{ fail "Keep WebSocket shutdown and scratch budget" }}{{ end -}}
{{- if or (ne (int $c.rollout.maxSurge) 0) (ne (int $c.rollout.maxUnavailable) 1) }}{{ fail "Initial WebSocket update replaces one Pod without surge" }}{{ end -}}
{{- range $probe := list "startup" "liveness" "readiness" }}
{{- $p := index $c.probes $probe }}{{- $path := "/api/actuator/health/liveness" }}
{{- if eq $probe "readiness" }}{{ $path = "/api/actuator/health/readiness" }}{{ end }}
{{- if or (ne $p.httpGet.path $path) (ne $p.httpGet.port "http") (ne $p.httpGet.scheme "HTTP") }}{{ fail "Use actual WebSocket health endpoints" }}{{ end -}}
{{- end }}
{{- end -}}
