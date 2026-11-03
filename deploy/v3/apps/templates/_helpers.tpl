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
{{- if or (regexMatch "(?i)(password|secret|token|api_?key|private_?key)$" $key) (eq $key "KAKAO_CLIENT_ID") -}}
{{- fail (cat $prefix "use secretRefs for" $key) -}}
{{- end -}}
{{- if hasKey $c.secretRefs $key -}}{{ fail (cat $prefix "duplicate environment and Secret key" $key) }}{{- end -}}
{{- end -}}
{{- end -}}
