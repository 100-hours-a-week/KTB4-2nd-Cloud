{{- define "redis.labels" -}}
app.kubernetes.io/name: redis
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: yeodam
app.kubernetes.io/managed-by: {{ .Release.Service }}
yeodam.io/stage: v3
{{- end }}
{{- define "redis.selector" -}}
app.kubernetes.io/name: redis
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
