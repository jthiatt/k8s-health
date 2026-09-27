# EKS Health Agent

Runs in every EKS cluster. At a fixed interval it checks the core platform services (CoreDNS, Cilium, FluxCD, Argo CD, Traefik, External Secrets, ExternalDNS, Karpenter). It sends one up/down gauge per check to Splunk Observability Cloud through the node-local Splunk OTel Collector, plus selected Prometheus series from each service. This repo also contains the Splunk detector (`detector.tf`) that alerts on those metrics.

The dashboard that reads these metrics is a separate repo: [eks-status-page](https://github.com/jthiatt/eks-status-page).

```
 each EKS cluster
┌──────────────────────┐ :9943  ┌──────────────────────┐      ┌──────────────────────┐
│ eks-health-agent     │ ─────▶ │ Splunk OTel Collector│ ───▶ │ Splunk Observability │ ◀── eks-status-page
│  • workload readiness│        │ agent (same node)    │      │ metrics + detector   │     (reads the API)
│  • DNS lookups       │        └──────────────────────┘      └──────────────────────┘
│  • /metrics scrapes  │
└──────────────────────┘
```

The agent is a single Python script that uses only the standard library. It runs on the stock `python:3.12-slim` image and loads the script from a ConfigMap, so there's no image to build. It needs **no Splunk token**: the collector forwards its data using the ingest token the collector already has.

## How the agent and status page connect

The agent ([eks-health-agent](https://github.com/jthiatt/eks-health-agent)) and the status page ([eks-status-page](https://github.com/jthiatt/eks-status-page)) are separate repos with separate release cycles. They never talk to each other directly; everything goes through Splunk. **Changing any of the following in one repo means changing the other to match:**

| Contract | Agent side | Status page side |
|---|---|---|
| Metric `eks.health.up`, gauge, 1 = up / 0 = down | One datapoint per check per interval | The only metric it reads |
| Dimensions `cluster`, `service`, `check` | `CLUSTER_NAME`, the service keys in `checks.json`, the check label | Groups the page by cluster, then service; lists failing checks by `check` |
| Check interval | `INTERVAL_SECONDS` (default 60) | `INTERVAL_SECONDS` must be the same, or healthy checks show as stale |
| Detector names contain "EKS" | `detector.tf` in the agent repo | `DETECTOR_MATCH=eks` filters which incidents are shown |

The Splunk OTel Collector also adds its own host dimensions. The status page ignores those and merges series by `cluster`/`service`/`check`.

---

## 1. Prerequisites

- `kubectl` 1.21+ (for `kubectl apply -k`), with access to each cluster.
- **The Splunk OTel Collector** (`splunk-otel-collector` Helm chart) running as a DaemonSet on every node, with its `signalfx` receiver on port 9943. The chart enables this receiver by default and serves it on the node IP. The agent finds its node's IP with the Downward API (`status.hostIP`). See [2a](#2a-check-the-collector-receiver).
- Terraform with the `splunk-terraform/signalfx` provider (for the detector only).
- **Service settings the agent's metrics checks depend on.** Change these in each service's Helm values, or remove the matching check from `checks.json`:

| Service | Required setting |
|---|---|
| Cilium | `prometheus.enabled=true` (agent :9962) and `operator.prometheus.enabled=true` (operator :9963). Both are **off by default**. |
| Traefik | Enable the `metrics` entrypoint on :9100 (the chart default) with Prometheus metrics. |
| FluxCD, Argo CD, External Secrets, ExternalDNS, Karpenter | Nothing. Metrics are on by default. |

- **Network access.** The agent scrapes pods directly by IP from the `eks-status` namespace.
  - **Flux and Argo CD:** both ship NetworkPolicies. Flux's `allow-scraping` policy and Argo CD's default policies allow the metrics ports. If you've tightened them, allow ingress from `eks-status` to the ports in the table under [Default checks](#default-checks).
  - **Cilium:** Cilium agents use the host network, so the agent scrapes each **node IP** on port 9962. The default EKS cluster security group allows node-to-node traffic. Check this if your nodes (including Karpenter-launched ones) use custom security groups.

## 2. Install the agent (every cluster)

### 2a. Check the collector receiver

Check that the node's collector accepts datapoints on 9943. Run this from any pod, using a node IP:

```bash
kubectl run sfx-probe --rm -it --restart=Never --image=curlimages/curl -- \
  curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' \
  -d '{"gauge":[]}' http://<node-ip>:9943/v2/datapoint
```

`200` means it's ready. If the connection is refused, the receiver is disabled or not exposed on the node. Check the chart's `agent.config` for the `signalfx` receiver in the metrics pipeline, and the `signalfx` port under `agent.ports`. If you run the collector differently (for example as a gateway Deployment behind a Service), set `INGEST_URL` to its `/v2/datapoint` address instead.

### 2b. Set the cluster name

Each cluster must report a unique `CLUSTER_NAME`, because that's how the status page groups results. Don't edit `agent.yaml` for each cluster. Instead, add a small kustomize overlay per cluster, for example `clusters/prod-use1/kustomization.yaml` in your GitOps repo, pinned to a release of this repo:

```yaml
resources:
  - ssh://git@github.com/jthiatt/eks-health-agent.git?ref=v0.1.0   # or a local path to a checkout
patches:
  - target: {kind: Deployment, name: eks-health-agent}
    patch: |
      - op: replace
        path: /spec/template/spec/containers/0/env/0/value
        value: prod-use1
```

The repo is private, so use the `ssh://` form shown. kustomize fetches remote bases with `git`, and an `https://github.com/...` URL fails with `git fetch ... exit status 128` because it has no credentials. Whatever renders the overlay (you, CI, or Flux/Argo CD) needs an SSH key with read access to this repo.

To use different checks in one cluster, add a `configMapGenerator` to the overlay with `name: eks-health-agent`, `behavior: replace` and that cluster's own `checks.json`.

### 2c. Apply

```bash
kubectl apply -k clusters/prod-use1
```

**Always use `-k` (kustomize), never `kubectl apply -f agent.yaml`.** There's no ConfigMap file in this repo: `kustomization.yaml` generates the `eks-health-agent` ConfigMap from `agent.py` and `checks.json`, and adds a content hash to its name. `-f agent.yaml` creates the Deployment without that ConfigMap, and the pod is stuck in `ContainerCreating` with `configmap "eks-health-agent" not found`. To see exactly what gets applied, run `kubectl kustomize clusters/prod-use1`.

If you use GitOps, point a Flux `Kustomization` or an Argo CD `Application` at the overlay directory. The agent monitors Flux and Argo CD, so it still reports correctly when those tools are the ones deploying it.

### 2d. Verify

```bash
kubectl -n eks-status logs deploy/eks-health-agent
```

A healthy agent logs nothing. Every failed check logs a JSON line with `service`, `check` and `error`. An `ingest failed` line means the agent couldn't reach the collector on its node. Within a minute or two, the metric `eks.health.up` should appear in Splunk's Metric Finder with `cluster`, `service` and `check` dimensions.

The collector also adds its own dimensions (`host.name`, `k8s.node.name`, `k8s.cluster.name`, cloud attributes). So when the agent pod moves to another node, each check starts a new series in Splunk. The status page merges series by `cluster`/`service`/`check` and keeps the newest point, and the detector groups by `cluster` and `service`, so neither is affected. If you build your own charts, group by those dimensions too.

## 3. Create the detector

`detector.tf` is a single `signalfx_detector` resource. Copy it into a Terraform config that has the SignalFx provider:

```hcl
terraform {
  required_providers {
    signalfx = { source = "splunk-terraform/signalfx" }
  }
}

provider "signalfx" {
  auth_token = var.splunk_api_token   # an API token that can manage detectors
  api_url    = "https://api.us1.signalfx.com"
}
```

```bash
terraform init && terraform apply
```

It has no notification recipients, so it only raises incidents, which the status page shows. To be paged too, add `notifications = [...]` to the rules you care about.

| Rule | Severity | Fires when |
|---|---|---|
| EKS service down | Critical | Any check for a service has been 0 for 2 minutes |
| EKS health agent not reporting | Major | A cluster's agent has sent nothing for 5 minutes |
| EKS Cilium nodes unreachable | Critical | Cilium reports unreachable nodes for 5 minutes |
| EKS Traefik 5xx rate high | Major | More than 5% of an entrypoint's requests are 5xx for 5 minutes |
| EKS ExternalDNS registry errors | Major | Registry errors every minute for 10 minutes |
| EKS Flux reconcile errors | Warning | A Flux controller has reconcile errors every minute for 10 minutes |
| EKS Argo CD app degraded | Warning | An app has been `Degraded` for 10 minutes |
| EKS ExternalSecret not syncing | Warning | An ExternalSecret has been not Ready for 15 minutes |
| EKS Karpenter nodepool near limit | Warning | A nodepool has been above 90% of a limit for 10 minutes |

The page only lists incidents from detectors whose name contains `DETECTOR_MATCH` (default in the manifest: `eks`). Keep "EKS" in the names of any detectors you add.

---

## Configuring checks (`checks.json`)

The file maps each **service name** (what the status page shows) to a list of checks. There are three kinds of check:

```jsonc
// 1. Workload readiness: every desired replica is ready. kind is deployment, statefulset or daemonset.
{"kind": "deployment", "namespace": "kube-system", "name": "coredns"}

// 2. DNS lookup. A trailing dot skips search-domain expansion, so failures come back fast.
{"dns": "kubernetes.default.svc.cluster.local."}

// 3. Scrape Prometheus metrics
{
  "metrics": {"namespace": "flux-system", "selector": "app=kustomize-controller", "port": 8080},
  //   or {"url": "http://host:port/metrics"} for a fixed address; optional "path" (default /metrics)
  "forward": ["workqueue_depth", "controller_runtime_reconcile_errors_total"],
  "max":     {"workqueue_depth": 100},
  "min":     {"some_gauge": 1},
  "max_age": {"external_dns_controller_last_sync_timestamp_seconds": 600}
}
```

How metrics checks behave:

- **Which pods are scraped:** with `selector`, the agent scrapes every **running** pod that matches, directly by IP. The check fails if no pods match or any pod can't be reached.
- **Selectors:** `forward`, `max`, `min` and `max_age` entries can be a metric name or `name{label="value",...}` to match only series with those labels. For example, `clustersecretstore_status_condition{condition="Ready",status="False"}`.
- **Thresholds:**
  - `max` fails when any matching series is above the limit.
  - `min` fails when any matching series is below the limit.
  - `max_age` fails when a series holding a Unix timestamp is older than the limit, in seconds.
  - `min` and `max_age` **also fail when no series matches**, because they check that something is present. `max` passes when nothing matches.
- **Forwarded series:**
  - **Name and dimensions:** each is sent to Splunk under its original name, with dimensions `cluster`, `service`, `pod` and the series' own labels. `cluster` and `service` override any labels with the same name.
  - **Counter or gauge:** series declared as counters in the endpoint's `# TYPE` lines, including histogram and summary `_bucket`/`_sum`/`_count` series, are sent as Splunk cumulative counters. Everything else is sent as a gauge.
  - **NaN and Inf values** are dropped.
- **Failed thresholds:** when a threshold fails, the check reports 0, but the forwarded series are still sent.

**Watch the number of series.** Every forwarded series is multiplied by every pod and every label value. Cilium adds about 30 series per node, `argocd_app_info` one per app, and the External Secrets filter one per ExternalSecret. Remove entries from `forward` if Splunk's series usage climbs.

### Default checks

| Service | Workloads | Metrics endpoint | Fails the check when |
|---|---|---|---|
| coredns | `kube-system/coredns` | — (DNS lookups of `kubernetes.default.svc.cluster.local.` and `sts.amazonaws.com.`) | a lookup fails |
| cilium | `kube-system` DaemonSet `cilium`, `cilium-operator` | agent `k8s-app=cilium` :9962, operator `io.cilium/app=operator` :9963 | any eBPF map is over 90% full |
| fluxcd | 4 controllers in `flux-system` | `app in (...)` :8080 | `workqueue_depth` > 100 |
| argocd | server, repo-server, redis, application-controller | application-controller :8082, repo-server :8084 | `workqueue_depth` > 100 |
| traefik | `traefik/traefik` | `app.kubernetes.io/name=traefik` :9100 | pod can't be scraped |
| external-secrets | controller, webhook, cert-controller | `app.kubernetes.io/name=external-secrets` :8080 | any ClusterSecretStore is not Ready |
| external-dns | `external-dns/external-dns` | `app.kubernetes.io/name=external-dns` :7979 | no successful sync in 10 minutes |
| karpenter | `kube-system/karpenter` | `app.kubernetes.io/name=karpenter` :8080 | pod can't be scraped |

These defaults are the upstream Helm chart values. **Check the namespaces, labels and ports against your clusters.** The ones most often different are Karpenter's namespace (`kube-system` vs `karpenter`) and port (8080 on v1, 8000 on older charts), and the ExternalDNS namespace. Remove any service you don't run, or it will always show as down.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `CLUSTER_NAME` | required | The `cluster` dimension. Must be unique per cluster. |
| `INTERVAL_SECONDS` | `60` | How often checks run. Must match the status page. |
| `SPLUNK_OTEL_AGENT` | node IP (Downward API `status.hostIP`) | The host of the node-local collector. The agent sends to `http://<SPLUNK_OTEL_AGENT>:9943/v2/datapoint`. |
| `INGEST_URL` | — | Overrides the full URL. Use it for a collector gateway, or send straight to `https://ingest.<realm>.signalfx.com/v2/datapoint` (which also needs `SPLUNK_ACCESS_TOKEN`). |
| `SPLUNK_ACCESS_TOKEN` | — | Only needed when `INGEST_URL` is Splunk's own ingest endpoint. Sent as `X-SF-Token`. |
| `CHECKS_FILE` | `/app/checks.json` | Location of the checks file. |

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| A whole cluster shows **stale** | The agent pod isn't running, or it can't reach the collector. Check `kubectl -n eks-status logs deploy/eks-health-agent` for `ingest failed`, then run the probe in [2a](#2a-check-the-collector-receiver). Also check that the collector pod on the agent's node is healthy, because the agent only sends to its own node's collector. |
| A cluster is missing from the page | It has never reported. Check `CLUSTER_NAME`, and check that the collector on the agent's node is running and exporting (its own logs). |
| `metrics/...` check is down with `no running pods match selector` | The label selector or namespace doesn't match your install. Compare with `kubectl get pods -n <ns> --show-labels`. |
| `metrics/...` check is down with a timeout or connection refused | Wrong port, metrics disabled in Helm, a NetworkPolicy, or (Cilium) the node security group. |
| `... missing` in the error | A `min` or `max_age` series doesn't exist. The metric name changed in your version, or the component isn't exporting it. |
| A forwarded metric never appears in Splunk | The metric name doesn't exist in your version. Forwarding a nonexistent name does nothing, so it doesn't fail the check. Compare against `curl <pod-ip>:<port>/metrics`. |
| A removed check shows as stale | Splunk keeps its series for a while. It stops showing once the series expires, or you can delete it in Splunk. |
| Pod stuck in `ContainerCreating`: `configmap "eks-health-agent" not found` | It was applied with `kubectl apply -f` instead of `-k`, so the generated ConfigMap was never created. Re-apply with `kubectl apply -k`. See [2c](#2c-apply). |
| `git fetch ... exit status 128` when applying the overlay | The overlay uses an `https://` URL for this private repo. Use `ssh://git@github.com/jthiatt/eks-health-agent.git?ref=...`. See [2b](#2b-set-the-cluster-name). |
| `Forbidden` on pods or deployments in the agent logs | The ClusterRole wasn't applied. Re-run `kubectl apply -k`. |

## Development

```bash
python3 test_agent.py   # self-check: readiness, Prometheus parsing and types, selectors, thresholds
```

There's no build step and no dependencies. Edit `agent.py` or `checks.json` and re-apply the kustomization. The ConfigMap name includes a hash of its contents, so the pod restarts with the new version. Tag a release (`v0.x.y`) so cluster overlays can pin to it.
