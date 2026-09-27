# Drop into a Terraform config that has the signalfx provider. Name contains "EKS" so the
# status page (DETECTOR_MATCH=eks) picks up its incidents. Add notifications if you want paging.
resource "signalfx_detector" "eks_core_services" {
  name        = "EKS core services"
  description = "An EKS core service is down or unhealthy, or a health agent stopped reporting"

  program_text = <<-EOF
    from signalfx.detectors.not_reporting import not_reporting
    up = data('eks.health.up').min(by=['cluster', 'service'])
    detect(when(up < 1, lasting='2m')).publish('EKS service down')
    not_reporting.detector(stream=data('eks.health.up'), resource_identifier=['cluster'], duration='5m').publish('EKS health agent not reporting')
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
    detect_label = "EKS health agent not reporting"
    severity     = "Major"
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
