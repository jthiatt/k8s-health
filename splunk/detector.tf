resource "signalfx_detector" "core_services" {
  name        = "${var.name_prefix} core services"
  description = "A core service or add-on in a cluster is down or unhealthy. (A silent agent is the deadman detector's job.)"

  program_text = <<-EOF
    up = data('${var.metric_name}').min(by=['cluster', 'service'])
    detect(when(up < 1, lasting='2m')).publish('Service down')
    flux_errors = data('controller_runtime_reconcile_errors_total', filter=filter('service', 'fluxcd'), rollup='delta').sum(by=['cluster', 'controller'])
    detect(when(flux_errors > 0, lasting='10m')).publish('Flux reconcile errors')
    argo_degraded = data('argocd_app_info', filter=filter('service', 'argocd') and filter('health_status', 'Degraded')).count(by=['cluster', 'name'])
    detect(when(argo_degraded > 0, lasting='10m')).publish('Argo CD app degraded')
    cilium_unreachable = data('cilium_node_health_connectivity_status', filter=filter('service', 'cilium') and filter('status', 'unreachable')).max(by=['cluster'])
    detect(when(cilium_unreachable > 0, lasting='5m')).publish('Cilium nodes unreachable')
    traefik_all = data('traefik_entrypoint_requests_total', filter=filter('service', 'traefik'), rollup='rate').sum(by=['cluster', 'entrypoint'])
    traefik_5xx = data('traefik_entrypoint_requests_total', filter=filter('service', 'traefik') and filter('code', '5*'), rollup='rate').sum(by=['cluster', 'entrypoint'])
    detect(when(traefik_5xx / traefik_all > 0.05, lasting='5m')).publish('Traefik 5xx rate high')
    eso_not_ready = data('externalsecret_status_condition', filter=filter('service', 'external-secrets') and filter('status', 'False')).sum(by=['cluster', 'namespace', 'name'])
    detect(when(eso_not_ready > 0, lasting='15m')).publish('ExternalSecret not syncing')
    dns_errors = data('external_dns_registry_errors_total', filter=filter('service', 'external-dns'), rollup='delta').sum(by=['cluster'])
    detect(when(dns_errors > 0, lasting='10m')).publish('ExternalDNS registry errors')
    k_usage = data('karpenter_nodepools_usage', filter=filter('service', 'karpenter')).max(by=['cluster', 'nodepool', 'resource_type'])
    k_limit = data('karpenter_nodepools_limit', filter=filter('service', 'karpenter')).max(by=['cluster', 'nodepool', 'resource_type'])
    detect(when(k_usage / k_limit > 0.9, lasting='10m')).publish('Karpenter nodepool near limit')
    keda_errors = data('keda_scaled_object_errors_total', filter=filter('service', 'keda'), rollup='delta').sum(by=['cluster', 'namespace', 'scaledObject'])
    detect(when(keda_errors > 0, lasting='10m')).publish('KEDA scaling errors')
    kyverno_errors = data('kyverno_policy_results_total', filter=filter('service', 'kyverno') and filter('rule_result', 'error'), rollup='delta').sum(by=['cluster'])
    detect(when(kyverno_errors > 0, lasting='10m')).publish('Kyverno policy errors')
  EOF

  rule {
    detect_label = "Service down"
    severity     = "Critical"
  }

  rule {
    detect_label = "Flux reconcile errors"
    severity     = "Warning"
  }

  rule {
    detect_label = "Argo CD app degraded"
    severity     = "Warning"
  }

  rule {
    detect_label = "Cilium nodes unreachable"
    severity     = "Critical"
  }

  rule {
    detect_label = "Traefik 5xx rate high"
    severity     = "Major"
  }

  rule {
    detect_label = "ExternalSecret not syncing"
    severity     = "Warning"
  }

  rule {
    detect_label = "ExternalDNS registry errors"
    severity     = "Major"
  }

  rule {
    detect_label = "Karpenter nodepool near limit"
    severity     = "Warning"
  }

  rule {
    detect_label = "KEDA scaling errors"
    severity     = "Warning"
  }

  rule {
    detect_label = "Kyverno policy errors"
    severity     = "Warning"
  }
}

# Dead man's switch: every cluster's agent sends var.metric_name every intervalSeconds (60). If a cluster that
# was reporting goes silent for 5 minutes, page someone. Without this, a dead agent looks like "no news" on
# every other detector and the dashboard, and only shows up as "stale" on the status page.
resource "signalfx_detector" "agent_deadman" {
  name        = "${var.name_prefix} agent deadman"
  description = "A cluster's k8s-health agent has stopped sending ${var.metric_name} to Splunk"

  program_text = <<-EOF
    from signalfx.detectors.not_reporting import not_reporting
    not_reporting.detector(stream=data('${var.metric_name}'), resource_identifier=['cluster'], duration='5m').publish('Agent not reporting')
  EOF

  rule {
    detect_label  = "Agent not reporting"
    severity      = "Major"
    notifications = var.deadman_notifications
    runbook_url   = var.runbook_url
    tip           = "Find the leader: kubectl -n ${var.agent_namespace} get lease ${var.agent_lease}. Then check its logs for 'ingest failed', and that the Splunk OTel Collector pod on its node is running."

    parameterized_subject = "{{#if anomalous}}k8s-health agent not reporting: {{dimensions.cluster}}{{else}}Resolved: k8s-health agent reporting again: {{dimensions.cluster}}{{/if}}"
    parameterized_body    = <<-EOF
      {{#if anomalous}}
      The k8s-health agent in cluster {{dimensions.cluster}} has not sent ${var.metric_name} to Splunk for 5 minutes.
      Until it is fixed, every check for this cluster is blind: the status page shows it as stale and no other k8s-health alert can fire for it.

      What to check:
      1. Agent pods: kubectl -n ${var.agent_namespace} get pods -l app.kubernetes.io/component=agent   (all READY 1/1)
      2. Leader:     kubectl -n ${var.agent_namespace} get lease ${var.agent_lease}   (HOLDER should be a running pod)
         Logs:       kubectl -n ${var.agent_namespace} logs <holder>                      ('ingest failed' = can't reach the collector)
      3. Collector:  the Splunk OTel Collector agent pod on the leader's node, and its own logs
      {{else}}
      The k8s-health agent in cluster {{dimensions.cluster}} is sending ${var.metric_name} again.
      {{/if}}
      Detector: {{detectorName}}  |  Rule: {{ruleName}}  |  Time: {{timestamp}}
    EOF
  }
}
