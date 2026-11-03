{{- define "yeodam-v3-platform.namespaceLabels" -}}
app.kubernetes.io/part-of: yeodam
app.kubernetes.io/managed-by: {{ .root.Release.Service | quote }}
yeodam.io/stage: v3
yeodam.io/domain: {{ .namespace | quote }}
{{- end -}}

{{/* Reuse the same stable labels on future Deployment pod templates. */}}
{{- define "yeodam-v3-platform.workloadLabels" -}}
app.kubernetes.io/name: {{ .workload.name | quote }}
app.kubernetes.io/instance: {{ .instance | quote }}
app.kubernetes.io/component: {{ .workload.component | quote }}
app.kubernetes.io/part-of: yeodam
yeodam.io/stage: v3
yeodam.io/deployment-purpose: {{ .workload.purpose | quote }}
{{- end -}}
