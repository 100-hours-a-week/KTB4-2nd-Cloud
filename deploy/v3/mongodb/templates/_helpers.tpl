{{- define "mongodb.labels" -}}
app.kubernetes.io/name: mongodb
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: yeodam
app.kubernetes.io/managed-by: {{ .Release.Service }}
yeodam.io/stage: v3
{{- end }}
{{- define "mongodb.selector" -}}
app.kubernetes.io/name: mongodb
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
