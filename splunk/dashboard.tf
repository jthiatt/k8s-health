# Cluster health dashboard. Core charts use var.metric_name; the second section charts the series the agent
# forwards from each service (empty until that service runs in a cluster).
# The collector adds host dims (host.name, k8s.node.name, ...) that change when the agent pod moves node,
# so every chart aggregates by cluster/service/check rather than using raw series.

locals {
  # Latest value per check; 1 = up, 0 = down.
  checks = "data('${var.metric_name}', rollup='latest').min(by=['cluster', 'service', 'check'])"
}

resource "signalfx_dashboard_group" "main" {
  name        = var.name_prefix
  description = "Kubernetes cluster health, from the k8s-health agent"
}

# --- Core health ---------------------------------------------------------------------------------

resource "signalfx_single_value_chart" "clusters" {
  name          = "Clusters reporting"
  description   = "Clusters whose agent sent data in the current window"
  program_text  = "data('${var.metric_name}').max(by=['cluster']).count().publish('clusters')"
  max_precision = 3
}

resource "signalfx_single_value_chart" "checks" {
  name          = "Checks reporting"
  program_text  = "${local.checks}.count().publish('checks')"
  max_precision = 4
}

resource "signalfx_single_value_chart" "services_down" {
  name          = "Services down"
  description   = "Services with at least one failing check"
  program_text  = "(1 - data('${var.metric_name}', rollup='latest').min(by=['cluster', 'service'])).sum().publish('services down')"
  max_precision = 3
  color_by      = "Scale"
  color_scale {
    gt    = 0
    color = "red"
  }
  color_scale {
    lte   = 0
    color = "green"
  }
}

resource "signalfx_single_value_chart" "checks_down" {
  name          = "Checks down"
  program_text  = "(1 - ${local.checks}).sum().publish('checks down')"
  max_precision = 4
  color_by      = "Scale"
  color_scale {
    gt    = 0
    color = "red"
  }
  color_scale {
    lte   = 0
    color = "green"
  }
}

resource "signalfx_heatmap_chart" "service_health" {
  name         = "Service health by cluster"
  description  = "Green = every check up, red = at least one check down, empty = not reporting"
  program_text = "data('${var.metric_name}', rollup='latest').min(by=['cluster', 'service']).publish('health')"
  group_by     = ["cluster"]
  color_scale {
    gte   = 1
    color = "green"
  }
  color_scale {
    lt    = 1
    color = "red"
  }
}

resource "signalfx_list_chart" "failing_checks" {
  name          = "Failing checks"
  description   = "Checks whose latest value is 0. Empty = nothing failing."
  program_text  = "${local.checks}.below(1).publish('down')"
  max_precision = 1
}

resource "signalfx_time_chart" "availability" {
  name         = "Service availability"
  description  = "Worst check per service over time (1 = up, 0 = down)"
  program_text = "data('${var.metric_name}').min(by=['cluster', 'service']).publish('up')"
  plot_type    = "LineChart"
  axis_left {
    min_value = 0
    max_value = 1
  }
}

resource "signalfx_time_chart" "checks_per_cluster" {
  name         = "Checks reporting per cluster"
  description  = "A drop to zero means that cluster's agent (or its node's collector) stopped sending"
  program_text = "data('${var.metric_name}').count(by=['cluster']).publish('checks')"
  plot_type    = "LineChart"
}

# --- Forwarded service metrics -------------------------------------------------------------------

resource "signalfx_text_chart" "forwarded_header" {
  name     = "Forwarded service metrics"
  markdown = "Series the agent forwards from each service's `/metrics`. A chart stays empty until that service runs in a cluster and its `metrics` check is enabled in `checks.json`."
}

resource "signalfx_time_chart" "cilium_drops" {
  name         = "Cilium drops/s by reason"
  program_text = "data('cilium_drop_count_total', filter=filter('service', 'cilium'), rollup='rate').sum(by=['cluster', 'reason']).publish('drops/s')"
  plot_type    = "AreaChart"
  stacked      = true
}

resource "signalfx_time_chart" "flux_errors" {
  name         = "Flux reconcile errors/s"
  program_text = "data('controller_runtime_reconcile_errors_total', filter=filter('service', 'fluxcd'), rollup='rate').sum(by=['cluster', 'controller']).publish('errors/s')"
  plot_type    = "LineChart"
}

resource "signalfx_list_chart" "argo_unhealthy" {
  name         = "Argo CD apps not Healthy"
  program_text = "data('argocd_app_info', filter=filter('service', 'argocd') and not filter('health_status', 'Healthy')).count(by=['cluster', 'health_status']).publish('apps')"
}

resource "signalfx_time_chart" "traefik_5xx" {
  name         = "Traefik 5xx % by entrypoint"
  program_text = <<-EOF
    all = data('traefik_entrypoint_requests_total', filter=filter('service', 'traefik'), rollup='rate').sum(by=['cluster', 'entrypoint'])
    err = data('traefik_entrypoint_requests_total', filter=filter('service', 'traefik') and filter('code', '5*'), rollup='rate').sum(by=['cluster', 'entrypoint'])
    (err / all * 100).publish('5xx %')
  EOF
  plot_type    = "LineChart"
}

resource "signalfx_list_chart" "eso_not_ready" {
  name         = "ExternalSecrets not Ready"
  program_text = "data('externalsecret_status_condition', filter=filter('service', 'external-secrets') and filter('status', 'False')).sum(by=['cluster', 'namespace', 'name']).above(0).publish('not ready')"
}

resource "signalfx_time_chart" "karpenter_usage" {
  name         = "Karpenter nodepool usage % of limit"
  program_text = <<-EOF
    usage = data('karpenter_nodepools_usage', filter=filter('service', 'karpenter')).max(by=['cluster', 'nodepool', 'resource_type'])
    limit = data('karpenter_nodepools_limit', filter=filter('service', 'karpenter')).max(by=['cluster', 'nodepool', 'resource_type'])
    (usage / limit * 100).publish('usage %')
  EOF
  plot_type    = "LineChart"
}

resource "signalfx_time_chart" "keda_errors" {
  name         = "KEDA ScaledObject errors/s"
  program_text = "data('keda_scaled_object_errors_total', filter=filter('service', 'keda'), rollup='rate').sum(by=['cluster', 'namespace', 'scaledObject']).publish('errors/s')"
  plot_type    = "LineChart"
}

resource "signalfx_time_chart" "kyverno_results" {
  name         = "Kyverno policy results/s"
  description  = "pass, fail, warn, error and skip, summed across policies"
  program_text = "data('kyverno_policy_results_total', filter=filter('service', 'kyverno'), rollup='rate').sum(by=['cluster', 'rule_result']).publish('results/s')"
  plot_type    = "AreaChart"
  stacked      = true
}

resource "signalfx_time_chart" "fluentd_retries" {
  name         = "fluentd output retries"
  description  = "Retries pending per output plugin; above 0 for long means logs aren't being delivered"
  program_text = "data('fluentd_output_status_retry_count', filter=filter('service', 'fluentd')).max(by=['cluster', 'plugin_id']).publish('retries')"
  plot_type    = "LineChart"
}

resource "signalfx_time_chart" "arc_runners" {
  name         = "ARC runners: busy vs idle"
  description  = "Per runner scale set, from the ARC listeners (needs ARC metrics on)"
  program_text = <<-EOF
    data('gha_busy_runners', filter=filter('service', 'arc')).sum(by=['cluster', 'name']).publish('busy')
    data('gha_idle_runners', filter=filter('service', 'arc')).sum(by=['cluster', 'name']).publish('idle')
  EOF
  plot_type    = "AreaChart"
  stacked      = true
}

resource "signalfx_time_chart" "apiserver_requests" {
  name         = "API server requests/s by code"
  program_text = "data('apiserver_request_total', filter=filter('service', 'kubernetes'), rollup='rate').sum(by=['cluster', 'code']).publish('requests/s')"
  plot_type    = "AreaChart"
  stacked      = true
}

resource "signalfx_time_chart" "pod_scheduling" {
  name         = "Pods scheduled within SLO % (1h)"
  description  = "Share of pods placed on a node within the SLO threshold (default 5 s), over the last hour"
  program_text = <<-EOF
    all = data('k8s.health.pods_scheduled', filter=filter('service', 'kubernetes'), rollup='sum').sum(by=['cluster']).sum(over='1h')
    ok = data('k8s.health.pods_scheduled_within_slo', filter=filter('service', 'kubernetes'), rollup='sum').sum(by=['cluster']).sum(over='1h')
    (ok / all * 100).publish('within SLO %')
  EOF
  plot_type    = "LineChart"
}

resource "signalfx_list_chart" "certs_not_ready" {
  name         = "Certificates not Ready"
  program_text = "data('certmanager_certificate_ready_status', filter=filter('service', 'cert-manager') and filter('condition', 'False')).max(by=['cluster', 'namespace', 'name']).above(0).publish('not ready')"
}

resource "signalfx_time_chart" "cert_days_left" {
  name         = "Days until the next certificate expires"
  description  = "Per cluster, the soonest expiry of any cert-manager Certificate"
  program_text = <<-EOF
    expiry = data('certmanager_certificate_expiration_timestamp_seconds', filter=filter('service', 'cert-manager')).above(0).min(by=['cluster'])
    clock = data('certmanager_clock_time_seconds_gauge', filter=filter('service', 'cert-manager')).max(by=['cluster'])
    ((expiry - clock) / 86400).publish('days left')
  EOF
  plot_type    = "LineChart"
}

resource "signalfx_time_chart" "prometheus_failures" {
  name         = "Prometheus rule failures and Alertmanager notification failures /s"
  program_text = <<-EOF
    data('prometheus_rule_evaluation_failures_total', filter=filter('service', 'prometheus'), rollup='rate').sum(by=['cluster']).publish('rule failures')
    data('alertmanager_notifications_failed_total', filter=filter('service', 'prometheus'), rollup='rate').sum(by=['cluster', 'integration']).publish('notification failures')
  EOF
  plot_type    = "LineChart"
}

# --- Dashboard -----------------------------------------------------------------------------------

resource "signalfx_dashboard" "cluster_health" {
  name            = "Cluster health"
  description     = "Core service health from the k8s-health agent. Use the Cluster filter to focus on one cluster."
  dashboard_group = signalfx_dashboard_group.main.id
  time_range      = "-3h"

  variable {
    property               = "cluster"
    alias                  = "Cluster"
    description            = "Filter every chart to one or more clusters"
    values                 = []
    value_required         = false
    restricted_suggestions = false
  }

  chart {
    chart_id = signalfx_single_value_chart.clusters.id
    row      = 0
    column   = 0
    width    = 3
    height   = 1
  }
  chart {
    chart_id = signalfx_single_value_chart.checks.id
    row      = 0
    column   = 3
    width    = 3
    height   = 1
  }
  chart {
    chart_id = signalfx_single_value_chart.services_down.id
    row      = 0
    column   = 6
    width    = 3
    height   = 1
  }
  chart {
    chart_id = signalfx_single_value_chart.checks_down.id
    row      = 0
    column   = 9
    width    = 3
    height   = 1
  }
  chart {
    chart_id = signalfx_heatmap_chart.service_health.id
    row      = 1
    column   = 0
    width    = 8
    height   = 2
  }
  chart {
    chart_id = signalfx_list_chart.failing_checks.id
    row      = 1
    column   = 8
    width    = 4
    height   = 2
  }
  chart {
    chart_id = signalfx_time_chart.availability.id
    row      = 3
    column   = 0
    width    = 6
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.checks_per_cluster.id
    row      = 3
    column   = 6
    width    = 6
    height   = 1
  }
  chart {
    chart_id = signalfx_text_chart.forwarded_header.id
    row      = 4
    column   = 0
    width    = 12
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.cilium_drops.id
    row      = 5
    column   = 0
    width    = 4
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.flux_errors.id
    row      = 5
    column   = 4
    width    = 4
    height   = 1
  }
  chart {
    chart_id = signalfx_list_chart.argo_unhealthy.id
    row      = 5
    column   = 8
    width    = 4
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.traefik_5xx.id
    row      = 6
    column   = 0
    width    = 4
    height   = 1
  }
  chart {
    chart_id = signalfx_list_chart.eso_not_ready.id
    row      = 6
    column   = 4
    width    = 4
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.karpenter_usage.id
    row      = 6
    column   = 8
    width    = 4
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.keda_errors.id
    row      = 7
    column   = 0
    width    = 4
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.kyverno_results.id
    row      = 7
    column   = 4
    width    = 4
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.fluentd_retries.id
    row      = 7
    column   = 8
    width    = 4
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.arc_runners.id
    row      = 8
    column   = 0
    width    = 4
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.apiserver_requests.id
    row      = 8
    column   = 4
    width    = 4
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.pod_scheduling.id
    row      = 8
    column   = 8
    width    = 4
    height   = 1
  }
  chart {
    chart_id = signalfx_list_chart.certs_not_ready.id
    row      = 9
    column   = 0
    width    = 4
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.cert_days_left.id
    row      = 9
    column   = 4
    width    = 4
    height   = 1
  }
  chart {
    chart_id = signalfx_time_chart.prometheus_failures.id
    row      = 9
    column   = 8
    width    = 4
    height   = 1
  }
}
