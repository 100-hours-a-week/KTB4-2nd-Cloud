{{- define "yeodam-v3-apps.frontendCheck" -}}
{{- $c := .config }}{{- $contract := .root.Files.Get "files/frontend-contract.json" | fromJson }}
{{- if or (ne .instance "frontend") (ne (int $c.replicas) 1) }}{{ fail "FE profile deploys only frontend1" }}{{ end }}
{{- if not (regexMatch "^ghcr.io/100-hours-a-week/yeodam-frontend@sha256:[a-f0-9]{64}$" .root.Values.images.frontend) }}{{ fail "Use reviewed immutable FE image" }}{{ end }}
{{- if or $c.environment $c.configMapRefs $c.secretRefs $c.secretVolumes $c.springProperties }}{{ fail "FE uses public runtime config and registry reference only" }}{{ end }}
{{- if ne (len $c.configMapData) (int (add (len $contract.config) 1)) }}{{ fail "Unreviewed FE environment key" }}{{ end }}
{{- range $key, $v := $contract.config }}
{{- if eq $key "SERVER_API_BASE_URL" }}
{{- if not (has (index $c.configMapData $key) (list "https://v3.yeodam-2gether.com/api" "https://yeodam-2gether.com/api")) }}{{ fail "Only reviewed QA or operating FE API origin allowed" }}{{ end }}
{{- else }}{{- if ne (index $c.configMapData $key) $v }}{{ fail (printf "Invalid FE runtime setting %s" $key) }}{{ end }}{{ end }}
{{- end }}
{{- if or (not (regexMatch "^[a-f0-9]{40}$" $c.configMapData.RELEASE)) (not (regexMatch "^[a-f0-9]{64}$" $c.configurationRevision)) }}{{ fail "FE source and configuration revision required" }}{{ end }}
{{- $res := deepCopy $contract.resources }}{{- $_ := set $res.limits "cpu" "" }}
{{- if ne (toJson $c.resources) (toJson $res) }}{{ fail "Keep minimum FE resource budget" }}{{ end }}
{{- if or (ne (toJson $c.ports) (toJson (dict "http" 3000))) (ne (toJson $c.service) (toJson (dict "enabled" true "port" 3000))) }}{{ fail "FE uses internal HTTP3000 Service" }}{{ end }}
{{- if or (ne $c.scratchSizeLimit "64Mi") (ne (int $c.rollout.maxSurge) 0) (ne (int $c.rollout.maxUnavailable) 1) (ne (int $c.terminationGracePeriodSeconds) 60) (ne (int $c.preStopDelaySeconds) 5) }}{{ fail "Keep FE scratch, rollout and shutdown budget" }}{{ end }}
{{- range $name := list "startup" "liveness" "readiness" }}{{- $p := index $c.probes $name }}
{{- if or (ne $p.httpGet.path "/login") (ne $p.httpGet.port "http") (ne $p.httpGet.scheme "HTTP") }}{{ fail "Use actual public FE login route for local health" }}{{ end }}
{{- end }}
{{- end -}}
