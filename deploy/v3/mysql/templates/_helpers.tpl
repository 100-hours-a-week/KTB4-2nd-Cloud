{{- define "mysql.labels" -}}
app.kubernetes.io/name: mysql
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: database
app.kubernetes.io/part-of: yeodam
app.kubernetes.io/managed-by: {{ .Release.Service }}
yeodam.io/stage: v3
{{- end }}
{{- define "mysql.selector" -}}
app.kubernetes.io/name: mysql
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
