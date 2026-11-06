{{- define "yeodam-v3-apps.labels" -}}
app.kubernetes.io/name: {{ .config.name | quote }}
app.kubernetes.io/instance: {{ .instance | quote }}
app.kubernetes.io/component: {{ .config.component | quote }}
app.kubernetes.io/part-of: yeodam
yeodam.io/stage: v3
yeodam.io/deployment-purpose: {{ .config.purpose | quote }}
{{- end -}}

{{- define "yeodam-v3-apps.selector" -}}
app.kubernetes.io/name: {{ .config.name | quote }}
app.kubernetes.io/instance: {{ .instance | quote }}
{{- end -}}

{{- define "yeodam-v3-apps.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: [ALL]
{{- end -}}

{{- define "yeodam-v3-apps.check" -}}
{{- $c := .config -}}
{{- $prefix := printf "%s: " .instance -}}
{{- $roles := .root.Files.Get "files/placement-contract.json" | fromJson -}}
{{- $role := required "Unknown app placement role" (index $roles .instance) -}}
{{- if ne (toJson $c.nodeSelector) (toJson (dict "yeodam.io/role" $role "yeodam.io/stage" "v3")) }}{{ fail "App nodeSelector must select its own V3 CPU pool" }}{{ end -}}
{{- if ne (toJson $c.tolerations) (toJson (list (dict "key" "yeodam.io/role" "operator" "Equal" "value" $role "effect" "NoSchedule"))) }}{{ fail "App must tolerate only its own role taint" }}{{ end -}}
{{- $_ := required (cat $prefix "set a published digest-pinned image") (index .root.Values.images $c.imageRef) -}}
{{- $_ := required (cat $prefix "set CPU request") $c.resources.requests.cpu -}}
{{- $_ := required (cat $prefix "set memory request") $c.resources.requests.memory -}}
{{- $_ := required (cat $prefix "set memory limit") $c.resources.limits.memory -}}
{{- $_ := required (cat $prefix "set ephemeral-storage request") (index $c.resources.requests "ephemeral-storage") -}}
{{- $_ := required (cat $prefix "set ephemeral-storage limit") (index $c.resources.limits "ephemeral-storage") -}}
{{- $_ := required (cat $prefix "set scratch volume size") $c.scratchSizeLimit -}}
{{- $_ := required (cat $prefix "set configurationRevision") $c.configurationRevision -}}
{{- $_ := required (cat $prefix "set non-root runAsUser") $c.security.runAsUser -}}
{{- $_ := required (cat $prefix "set non-root runAsGroup") $c.security.runAsGroup -}}
{{- $_ := required (cat $prefix "set non-root fsGroup") $c.security.fsGroup -}}
{{- if empty $c.imagePullSecrets -}}{{ fail (cat $prefix "set the existing registry pull Secret reference") }}{{- end -}}
{{- if le (int $c.terminationGracePeriodSeconds) (int $c.preStopDelaySeconds) -}}
{{- fail (cat $prefix "termination grace must exceed preStop delay") -}}
{{- end -}}
{{- if eq (add (int $c.rollout.maxSurge) (int $c.rollout.maxUnavailable)) 0 -}}
{{- fail (cat $prefix "explicitly budget a RollingUpdate strategy") -}}
{{- end -}}
{{- range $probe := list "startup" "liveness" "readiness" -}}
{{- $p := index $c.probes $probe -}}
{{- $_ := required (cat $prefix "set" $probe "Probe") $p -}}
{{- if not (hasKey $c.ports $p.httpGet.port) -}}{{ fail (cat $prefix $probe "Probe references an unknown named port") }}{{- end -}}
{{- end -}}
{{- if and $c.service.enabled (not (hasKey $c.ports "http")) -}}{{ fail (cat $prefix "Service requires a named http port") }}{{- end -}}
{{- if and $c.service.enabled (not $c.service.port) -}}{{ fail (cat $prefix "set Service port") }}{{- end -}}
{{- if and $c.service.enabled (has $c.purpose (list "conversion-worker" "result-worker")) -}}
{{- fail (cat $prefix "Worker-purpose copies must not expose an HTTP Service") -}}
{{- end -}}
{{- if eq $c.name "backend" -}}{{- $_ := required (cat $prefix "set writable backend catalog volume size") $c.catalogSizeLimit -}}{{- end -}}
{{- range $key, $_ := $c.environment -}}
{{- if or (regexMatch "(?i)(password|secret|token|api_?key|private_?key)$" $key) (has $key (list "KAKAO_CLIENT_ID" "MONGODB_URI" "SPRING_MONGODB_URI")) -}}
{{- fail (cat $prefix "use secretRefs for" $key) -}}
{{- end -}}
{{- if hasKey $c.secretRefs $key -}}{{ fail (cat $prefix "duplicate environment and Secret key" $key) }}{{- end -}}
{{- end -}}
{{- range $key, $_ := $c.configMapData -}}
{{- if or (regexMatch "(?i)(password|secret|token|api_?key|private_?key)$" $key) (has $key (list "KAKAO_CLIENT_ID" "SPRING_MONGODB_URI" "MONGODB_URI")) -}}
{{- fail (cat $prefix "use secretRefs for" $key) -}}
{{- end -}}
{{- if or (hasKey $c.environment $key) (hasKey $c.secretRefs $key) -}}{{ fail (cat $prefix "duplicate ConfigMap/environment/Secret key" $key) }}{{- end -}}
{{- end -}}
{{- if has (printf "%s-runtime" .instance) $c.configMapRefs -}}{{ fail (cat $prefix "generated ConfigMap must not be imported twice") }}{{- end -}}
{{- if .root.Values.frontendContract -}}
{{- include "yeodam-v3-apps.frontendCheck" . -}}
{{- end -}}
{{- if .root.Values.websocketContract -}}
{{- include "yeodam-v3-apps.websocketCheck" . -}}
{{- end -}}
{{- if .root.Values.backendContract -}}
{{- include "yeodam-v3-apps.backendCheck" . -}}
{{- end -}}
{{- end -}}
