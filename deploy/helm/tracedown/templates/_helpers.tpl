{{- define "tdn.name" -}}{{ .Chart.Name | trunc 63 | trimSuffix "-" }}{{- end -}}

{{- define "tdn.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}{{ .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else -}}{{ printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}{{- end -}}
{{- end -}}

{{- define "tdn.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
app.kubernetes.io/name: {{ include "tdn.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: tracedown
{{- end -}}

{{/* selector labels for one component: include "tdn.selector" (list . "server") */}}
{{- define "tdn.selector" -}}
app.kubernetes.io/name: {{ include "tdn.name" (index . 0) }}
app.kubernetes.io/instance: {{ (index . 0).Release.Name }}
app.kubernetes.io/component: {{ index . 1 }}
{{- end -}}

{{- define "tdn.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end -}}

{{- define "tdn.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}{{ default (include "tdn.fullname" .) .Values.serviceAccount.name }}
{{- else -}}{{ default "default" .Values.serviceAccount.name }}{{- end -}}
{{- end -}}

{{- define "tdn.secretName" -}}
{{- default (printf "%s-env" (include "tdn.fullname" .)) .Values.secrets.existingSecret -}}
{{- end -}}

{{- define "tdn.kafkaEnabled" -}}
{{- if or .Values.redpanda.enabled .Values.external.kafkaBootstrap -}}true{{- end -}}
{{- end -}}

{{- define "tdn.hasDatabase" -}}
{{- if or .Values.postgresql.enabled .Values.external.databaseUrl .Values.secrets.existingSecret -}}true{{- end -}}
{{- end -}}

{{/* Keep a generated secret value stable across upgrades: (list . "secret-name" "KEY" length) */}}
{{- define "tdn.stableRandom" -}}
{{- $ctx := index . 0 -}}{{- $name := index . 1 -}}{{- $key := index . 2 -}}{{- $len := index . 3 -}}
{{- $existing := lookup "v1" "Secret" $ctx.Release.Namespace $name -}}
{{- if and $existing (hasKey $existing.data $key) -}}{{ index $existing.data $key | b64dec }}
{{- else -}}{{ randAlphaNum $len }}{{- end -}}
{{- end -}}

{{- define "tdn.pgPassword" -}}
{{- if .Values.postgresql.password -}}{{ .Values.postgresql.password }}
{{- else -}}{{ include "tdn.stableRandom" (list . (printf "%s-postgresql" (include "tdn.fullname" .)) "POSTGRES_PASSWORD" 32) }}{{- end -}}
{{- end -}}

{{/* Pod-level pieces shared by every Tracedown container */}}
{{- define "tdn.envFrom" -}}
- configMapRef: { name: {{ include "tdn.fullname" . }}-config }
- secretRef: { name: {{ include "tdn.secretName" . }} }
{{- end -}}

{{- define "tdn.podCommon" -}}
serviceAccountName: {{ include "tdn.serviceAccountName" . }}
securityContext: {{- toYaml .Values.podSecurityContext | nindent 2 }}
{{- with .Values.imagePullSecrets }}
imagePullSecrets: {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}
