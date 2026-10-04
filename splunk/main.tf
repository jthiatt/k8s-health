terraform {
  required_providers {
    signalfx = {
      source  = "splunk-terraform/signalfx"
      version = "~> 9.0"
    }
  }
}

variable "splunk_token" {
  description = "Splunk Observability API token that can manage detectors and dashboards (TF_VAR_splunk_token)"
  type        = string
  sensitive   = true
}

variable "deadman_notifications" {
  description = "Who the deadman detector alerts when an agent stops reporting, as Splunk notification strings: \"Email,<address>\", \"Team,<teamId>\", \"PagerDuty,<integrationId>\", \"Slack,<integrationId>,<channel>\". Set in terraform.tfvars."
  type        = list(string)
  validation {
    condition     = length(var.deadman_notifications) > 0
    error_message = "The deadman detector needs at least one recipient, or nobody hears about a silent agent."
  }
}

variable "splunk_realm" {
  description = "Your Splunk Observability realm, e.g. us0, us1, eu0, jp0 (Settings > Organization)"
  type        = string
}

variable "name_prefix" {
  description = "Prefix for detector and dashboard-group names. The status page lists incidents from detectors whose name contains its detectorMatch value, which defaults to this."
  type        = string
  default     = "k8s-health"
}

variable "metric_name" {
  description = "Metric the agents send (Helm value metricName)"
  type        = string
  default     = "k8s.health.up"
}

variable "agent_namespace" {
  description = "Namespace the agent's Helm release is in (used in the deadman alert's instructions)"
  type        = string
  default     = "k8s-health"
}

variable "agent_lease" {
  description = "The agent's Lease name: <Helm fullname>-agent, i.e. k8s-health-agent for a release named k8s-health"
  type        = string
  default     = "k8s-health-agent"
}

variable "runbook_url" {
  description = "Linked from the deadman alert"
  type        = string
  default     = "https://github.com/jthiatt/k8s-health#troubleshooting"
}

provider "signalfx" {
  auth_token = var.splunk_token
  api_url    = "https://api.${var.splunk_realm}.signalfx.com"
}

output "dashboard_url" {
  value = signalfx_dashboard.cluster_health.url
}
