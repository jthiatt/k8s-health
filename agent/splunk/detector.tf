resource "signalfx_detector" "eks_core_services" {
  name        = "EKS core services"
  description = "An EKS core service is down or unhealthy. (A silent agent is the deadman detector's job.)"

  program_text = <<-EOF
    up = data('eks.health.up').min(by=['cluster', 'service'])
    detect(when(up < 1, lasting='2m')).publish('EKS service down')
    flux_errors = data('controller_runtime_reconcile_errors_total', filter=filter('service', 'fluxcd'), rollup='delta').sum(by=['cluster', 'controller'])
    detect(when(flux_errors > 0, lasting='10m')).publish('EKS Flux reconcile errors')
    argo_degraded = data('argocd_app_info', filter=filter('service', 'argocd') and filter('health_status', 'Degraded')).count(by=['cluster', 'name'])
    detect(when(argo_degraded > 0, lasting='10m')).publish('EKS Argo CD app degraded')
    cilium_unreachable = data('cilium_node_health_connectivity_status', filter=filter('service', 'cilium') and filter('status', 'unreachable')).max(by=['cluster'])
    detect(when(cilium_unreachable > 0, lasting='5m')).publish('EKS Cilium nodes unreachable')
    traefik_all = data('traefik_entrypoint_requests_total', filter=filter('service', 'traefik'), rollup='rate').sum(by=['cluster', 'entrypoint'])
    traefik_5xx = data('traefik_entrypoint_requests_total', filter=filter('service', 'traefik') and filter('code', '5*'), rollup='rate').sum(by=['cluster', 'entrypoint'])
    detect(when(traefik_5xx / traefik_all > 0.05, lasting='5m')).publish('EKS Traefik 5xx rate high')
    eso_not_ready = data('externalsecret_status_condition', filter=filter('service', 'external-secrets') and filter('status', 'False')).sum(by=['cluster', 'namespace', 'name'])
    detect(when(eso_not_ready > 0, lasting='15m')).publish('EKS ExternalSecret not syncing')
    dns_errors = data('external_dns_registry_errors_total', filter=filter('service', 'external-dns'), rollup='delta').sum(by=['cluster'])
    detect(when(dns_errors > 0, lasting='10m')).publish('EKS ExternalDNS registry errors')
    k_usage = data('karpenter_nodepools_usage', filter=filter('service', 'karpenter')).max(by=['cluster', 'nodepool', 'resource_type'])
    k_limit = data('karpenter_nodepools_limit', filter=filter('service', 'karpenter')).max(by=['cluster', 'nodepool', 'resource_type'])
    detect(when(k_usage / k_limit > 0.9, lasting='10m')).publish('EKS Karpenter nodepool near limit')
  EOF

  rule {
    detect_label = "EKS service down"
    severity     = "Critical"
  }

  rule {
    detect_label = "EKS Flux reconcile errors"
    severity     = "Warning"
  }

  rule {
    detect_label = "EKS Argo CD app degraded"
    severity     = "Warning"
  }

  rule {
    detect_label = "EKS Cilium nodes unreachable"
    severity     = "Critical"
  }

  rule {
    detect_label = "EKS Traefik 5xx rate high"
    severity     = "Major"
  }

  rule {
    detect_label = "EKS ExternalSecret not syncing"
    severity     = "Warning"
  }

  rule {
    detect_label = "EKS ExternalDNS registry errors"
    severity     = "Major"
  }

  rule {
    detect_label = "EKS Karpenter nodepool near limit"
    severity     = "Warning"
  }
}

# Dead man's switch: every cluster's agent sends eks.health.up every INTERVAL_SECONDS (60). If a cluster that
# was reporting goes silent for 5 minutes, page someone. Without this, a dead agent looks like "no news" on
# every other detector and the dashboard, and only shows up as "stale" on the status page.
resource "signalfx_detector" "eks_agent_deadman" {
  name        = "EKS health agent deadman"
  description = "A cluster's eks-health-agent has stopped sending eks.health.up to Splunk"

  program_text = <<-EOF
    from signalfx.detectors.not_reporting import not_reporting
    not_reporting.detector(stream=data('eks.health.up'), resource_identifier=['cluster'], duration='5m').publish('EKS health agent not reporting')
  EOF

  rule {
    detect_label  = "EKS health agent not reporting"
    severity      = "Major"
    notifications = var.deadman_notifications
    runbook_url   = "https://github.com/jthiatt/eks-health-agent#troubleshooting"
    tip           = "Find the leader: kubectl -n eks-status get lease eks-health-agent. Then check its logs for 'ingest failed', and that the Splunk OTel Collector pod on its node is running."

    parameterized_subject = "{{#if anomalous}}EKS health agent not reporting: {{dimensions.cluster}}{{else}}Resolved: EKS health agent reporting again: {{dimensions.cluster}}{{/if}}"
    parameterized_body    = <<-EOF
      {{#if anomalous}}
      The eks-health-agent in cluster {{dimensions.cluster}} has not sent eks.health.up to Splunk for 5 minutes.
      Until it is fixed, every check for this cluster is blind: the status page shows it as stale and no other EKS alert can fire for it.

      What to check:
      1. Agent pods: kubectl -n eks-status get pods -l app=eks-health-agent   (3 replicas, READY 1/1)
      2. Leader:     kubectl -n eks-status get lease eks-health-agent        (HOLDER should be a running pod)
         Logs:       kubectl -n eks-status logs <holder>                      ('ingest failed' = can't reach the collector)
      3. Collector:  the Splunk OTel Collector agent pod on the leader's node, and its own logs
      {{else}}
      The eks-health-agent in cluster {{dimensions.cluster}} is sending eks.health.up again.
      {{/if}}
      Detector: {{detectorName}}  |  Rule: {{ruleName}}  |  Time: {{timestamp}}
    EOF
  }
}
