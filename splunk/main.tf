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

variable "splunk_realm" {
  type    = string
  default = "us1"
}

provider "signalfx" {
  auth_token = var.splunk_token
  api_url    = "https://api.${var.splunk_realm}.signalfx.com"
}

output "dashboard_url" {
  value = signalfx_dashboard.eks_health.url
}
