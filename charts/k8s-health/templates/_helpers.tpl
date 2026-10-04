{{- define "k8s-health.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 52 | trimSuffix "-" -}}
{{- else if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 52 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 52 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "k8s-health.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* selectorLabels: pass (list . "agent") or (list . "status-page") */}}
{{- define "k8s-health.selectorLabels" -}}
{{- $ := index . 0 -}}
app.kubernetes.io/name: {{ $.Chart.Name }}
app.kubernetes.io/instance: {{ $.Release.Name }}
app.kubernetes.io/component: {{ index . 1 }}
{{- end -}}

{{- define "k8s-health.image" -}}
{{- $img := index . 0 -}}{{- $ := index . 1 -}}
{{- printf "%s:%s" $img.repository ($img.tag | default $.Chart.AppVersion) -}}
{{- end -}}

{{- define "k8s-health.agentName" -}}{{ include "k8s-health.fullname" . }}-agent{{- end -}}
{{- define "k8s-health.statusPageName" -}}{{ include "k8s-health.fullname" . }}-status-page{{- end -}}
