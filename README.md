# EKS Health Agent

Runs in every EKS cluster. At a fixed interval it checks the core platform services (CoreDNS, Cilium, FluxCD, Argo CD, Traefik, External Secrets, ExternalDNS, Karpenter). It sends one up/down gauge per check to Splunk Observability Cloud through the node-local Splunk OTel Collector, plus selected Prometheus series from each service. This repo also contains the Splunk detector and dashboard for those metrics (Terraform, in `splunk/`).

The dashboard that reads these metrics is a separate repo: [eks-status-page](https://github.com/jthiatt/eks-status-page).

```
 each EKS cluster
┌──────────────────────┐ :4318  ┌──────────────────────┐      ┌──────────────────────┐
│ eks-health-agent     │ OTLP ▶ │ Splunk OTel Collector│ ───▶ │ Splunk Observability │ ◀── eks-status-page
│  • workload readiness│        │ agent (same node)    │      │ metrics + detector   │     (reads the API)
│  • DNS lookups       │        └──────────────────────┘      └──────────────────────┘
│  • /metrics scrapes  │
└──────────────────────┘
```

The agent is a single Python script that uses only the standard library. It runs on the stock `python:3.12-slim` image and loads the script from a ConfigMap, so there's no image to build. It sends **OTLP/HTTP (JSON)** to the collector on its own node, the collector's standard input, so it works with a default chart install. It needs **no Splunk token**: the collector forwards the data with the ingest token it already has.

It runs as **3 replicas, with one leader** chosen through a Kubernetes Lease, so a single bad node doesn't stop the checks. See [High availability](#high-availability).

## How the agent and status page connect

The agent ([eks-health-agent](https://github.com/jthiatt/eks-health-agent)) and the status page ([eks-status-page](https://github.com/jthiatt/eks-status-page)) are separate repos with separate release cycles. They never talk to each other directly; everything goes through Splunk. **Changing any of the following in one repo means changing the other to match:**

| Contract | Agent side | Status page side |
|---|---|---|
| Metric `eks.health.up`, gauge, 1 = up / 0 = down | One datapoint per check per interval | The only metric it reads |
| Dimensions `cluster`, `service`, `check` | `CLUSTER_NAME`, the service keys in `checks.json`, the check label | Groups the page by cluster, then service; lists failing checks by `check` |
| Check interval | `INTERVAL_SECONDS` (default 60) | `INTERVAL_SECONDS` must be the same, or healthy checks show as stale |
| Detector names contain "EKS" | `splunk/detector.tf` in the agent repo | `DETECTOR_MATCH=eks` filters which incidents are shown |

The Splunk OTel Collector also adds its own host dimensions. The status page ignores those and merges series by `cluster`/`service`/`check`.

---

## 1. Prerequisites

- `kubectl` 1.21+ (for `kubectl apply -k`), with access to each cluster.
- **The Splunk OTel Collector** (`splunk-otel-collector` Helm chart, tested with 0.161.0) running as a DaemonSet on every node. The chart's node agent listens for OTLP/HTTP on port 4318, with host networking, on the node IP. That's on by default. The agent finds its node's IP with the Downward API (`status.hostIP`). See [2a](#2a-check-the-collector-receiver).
  - Don't rely on the collector's `signalfx` receiver (port 9943). Current chart versions only run it on the optional gateway, not on the node agent.
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

Check that the node's collector accepts OTLP on 4318. Run this from any pod, using a node IP (`kubectl get nodes -o wide`):

```bash
kubectl run otlp-probe --rm -it --restart=Never --image=curlimages/curl -- \
  curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' \
  -d '{"resourceMetrics":[]}' http://<node-ip>:4318/v1/metrics
```

`200` means it's ready. If the connection is refused, check that the chart's `agent.ports.otlp-http` is still set (with `hostPort: 4318`) and that `otlp` is a receiver in the agent's `metrics` pipeline (`kubectl -n <ns> get cm -l component=otel-collector-agent -o yaml`). If you run the collector differently (for example as a gateway Deployment behind a Service), set `OTLP_ENDPOINT` to its OTLP/HTTP base URL instead.

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
kubectl -n eks-status get pods -l app=eks-health-agent          # 3 pods, all READY 1/1
kubectl -n eks-status get lease eks-health-agent                # HOLDER = the leader pod
kubectl -n eks-status logs "$(kubectl -n eks-status get lease eks-health-agent -o jsonpath='{.spec.holderIdentity}')"
```

Only the leader runs checks, so read the **leader's** logs. `kubectl logs deploy/eks-health-agent` picks an arbitrary pod, usually a standby with nothing to say. A healthy leader logs only `became leader`. Every failed check logs a JSON line with `service`, `check` and `error`. An `ingest failed` line means the leader couldn't reach the collector on its node; after 3 in a row it hands over to another replica. Within a minute or two, the metric `eks.health.up` should appear in Splunk's Metric Finder with `cluster`, `service` and `check` dimensions.

The collector also adds its own dimensions (`host.name`, `k8s.node.name`, `k8s.cluster.name`, cloud attributes). So when the agent pod moves to another node, each check starts a new series in Splunk. The status page merges series by `cluster`/`service`/`check` and keeps the newest point, and the detector groups by `cluster` and `service`, so neither is affected. If you build your own charts, group by those dimensions too.

## 3. Create the dashboard and detector

`splunk/` is a self-contained Terraform config:

| File | Contents |
|---|---|
| `main.tf` | The provider and variables (`splunk_token`, `splunk_realm`), plus the `dashboard_url` output. |
| `dashboard.tf` | Dashboard group **EKS** and dashboard **EKS health**. |
| `detector.tf` | Detector **EKS core services**. |

Both need a Splunk API token that can manage dashboards and detectors:

```bash
cd splunk
export TF_VAR_splunk_token=<API_TOKEN>
terraform init
terraform apply                                          # dashboard + detector
terraform apply -target=signalfx_dashboard.eks_health    # or the dashboard only
```

State is kept locally in `splunk/terraform.tfstate`, which is git-ignored. Only that machine can update or destroy these resources. For a team, add a remote backend (for example S3) to `main.tf`.

### Dashboard

**EKS health**, in dashboard group **EKS**. A **Cluster** filter at the top narrows every chart.
- **Headline numbers:** clusters reporting, checks reporting, services down, checks down. The "down" numbers turn red above 0.
- **Service health by cluster:** a heatmap, green or red per service.
- **Failing checks:** each check whose latest value is 0, with its cluster and service.
- **Service availability** over time, and **checks reporting per cluster**. A drop to 0 there means that cluster's agent, or its node's collector, stopped sending.
- **Forwarded service metrics:** Cilium drops by reason, Flux reconcile errors, Argo CD apps that aren't Healthy, Traefik 5xx %, ExternalSecrets not Ready, and Karpenter nodepool usage as a % of its limit. Each stays empty until that service runs and its `metrics` check is enabled.

The collector adds host dimensions that change when the agent pod moves to another node, so every chart aggregates by `cluster`/`service`/`check` rather than showing raw series.

### Detector

The detector has no notification recipients, so it only raises incidents, which the status page shows. To be paged too, add `notifications = [...]` to the rules you care about.

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

## High availability

The Deployment runs **3 replicas**. They elect a leader with a Kubernetes **Lease** named `eks-health-agent` in the agent's namespace, the same mechanism client-go's `leaderelection` and kube-controller-manager use. Only the leader runs checks and sends data; the others stand by.

- **Election:** every replica tries to acquire or renew the Lease every 10 seconds. Updates use the Lease's `resourceVersion`, so when two replicas race for an expired Lease, exactly one wins and the other gets `409 Conflict`.
- **Leader's node dies or loses the API server:** the leader stops renewing. It stops collecting 20 seconds after its last successful renewal, and a standby takes over once the Lease expires (`LEASE_DURATION_SECONDS`, 30), so two leaders never overlap. On minikube a frozen leader was replaced after 23 seconds.
- **Leader can't reach its node's collector:** after 3 failed sends in a row, it releases the Lease and holds off for 60 seconds, so a replica on another node, with a working collector, takes over. Failing *checks* never cause a handover; those are results, not node problems.
- **Rollouts and deletes:** on SIGTERM the leader releases the Lease, so a standby takes over at its next attempt. On minikube that took under 1 second.
- **Status page:** a handover costs at most one check interval, well within the page's 3-interval stale threshold. On minikube, Splunk showed no gap longer than the normal 60 seconds across both tests.

**Probes** (port 8080):

| Endpoint | Probe | 200 when | Why |
|---|---|---|---|
| `/readyz` | readiness | The Kubernetes API answered within the last 30 s | A replica that can't reach the API can neither lead nor run checks. |
| `/healthz` | liveness | The election loop is running, and no check cycle has been stuck for 3 × `INTERVAL_SECONDS` | Restarts a hung process. It deliberately stays healthy when the API or collector is unreachable, because restarting on the same node wouldn't help; handing over leadership does. |

Both return `{"ok": ..., "role": "leader" | "standby"}`.

**Placement:**
- **Pod anti-affinity:** prefers one replica per node (`kubernetes.io/hostname`). It's *preferred*, not required, so all 3 still run on clusters with fewer than 3 nodes (like minikube). Switch it to `requiredDuringSchedulingIgnoredDuringExecution` if you'd rather leave extra replicas Pending than share a node.
- **Topology spread:** a `topologySpreadConstraints` rule spreads replicas across availability zones (`ScheduleAnyway`).
- **PodDisruptionBudget:** `maxUnavailable: 1`, so node drains and upgrades take at most one replica at a time.

**RBAC:** a namespaced Role, `eks-health-agent-leader-election`, allows `get`, `create` and `update` on Leases in the agent's namespace. The existing ClusterRole is unchanged.

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
| `SPLUNK_OTEL_AGENT` | node IP (Downward API `status.hostIP`) | The host of the node-local collector. The agent sends OTLP/HTTP JSON to `http://<SPLUNK_OTEL_AGENT>:4318/v1/metrics`. |
| `OTLP_ENDPOINT` | — | Overrides the base URL (the agent appends `/v1/metrics`), for example a collector gateway Service: `http://splunk-otel-collector.<ns>:4318`. |
| `CHECKS_FILE` | `/app/checks.json` | Location of the checks file. |
| `POD_NAME` | pod hostname (the manifest sets it from `metadata.name`) | This replica's identity in the Lease. |
| `LEASE_DURATION_SECONDS` | `30` | How long a Lease stays valid without renewal, so roughly how long a dead leader takes to be replaced. Must be more than 10 (the renew period); the leader stops acting 10 s before it expires. |
| `HEALTH_PORT` | `8080` | Port for `/healthz` and `/readyz`. |

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `collector rejected datapoints` in the agent logs | The collector accepted the request but dropped some points (the log line includes its reason). Check the collector's own logs. |
| A whole cluster shows **stale** | The agent pod isn't running, or it can't reach the collector. Check the leader's logs for `ingest failed` (see [2d](#2d-verify)), then run the probe in [2a](#2a-check-the-collector-receiver). Also check that the collector pod on the agent's node is healthy, because the agent only sends to its own node's collector. |
| A cluster is missing from the page | It has never reported. Check `CLUSTER_NAME`, and check that the collector on the agent's node is running and exporting (its own logs). |
| `metrics/...` check is down with `no running pods match selector` | The label selector or namespace doesn't match your install. Compare with `kubectl get pods -n <ns> --show-labels`. |
| `metrics/...` check is down with a timeout or connection refused | Wrong port, metrics disabled in Helm, a NetworkPolicy, or (Cilium) the node security group. |
| `... missing` in the error | A `min` or `max_age` series doesn't exist. The metric name changed in your version, or the component isn't exporting it. |
| A forwarded metric never appears in Splunk | The metric name doesn't exist in your version. Forwarding a nonexistent name does nothing, so it doesn't fail the check. Compare against `curl <pod-ip>:<port>/metrics`. |
| A removed check still shows on the status page | Expected for a while. It shows its last state, then stale after `STALE_AFTER_SECONDS`. The page drops it about 15 minutes after its last point, once it has no data in the lookback window while the rest of the cluster keeps reporting. In Splunk charts it simply stops reporting. |
| Pod stuck in `ContainerCreating`: `configmap "eks-health-agent" not found` | It was applied with `kubectl apply -f` instead of `-k`, so the generated ConfigMap was never created. Re-apply with `kubectl apply -k`. See [2c](#2c-apply). |
| `git fetch ... exit status 128` when applying the overlay | The overlay uses an `https://` URL for this private repo. Use `ssh://git@github.com/jthiatt/eks-health-agent.git?ref=...`. See [2b](#2b-set-the-cluster-name). |
| No pod holds the Lease, or the holder keeps changing | Check the logs of every replica for `leader election failed`. `Forbidden` on `leases` means the Role or RoleBinding wasn't applied. Frequent changes with `stepping down: cannot reach this node's collector` mean several nodes' collectors are unhealthy. |
| Pods not Ready | `/readyz` fails when the pod can't reach the Kubernetes API. Check the node's networking. Check the probe response with `kubectl -n eks-status exec <pod> -- python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8080/readyz').read())"`. |
| `Forbidden` on pods or deployments in the agent logs | The ClusterRole wasn't applied. Re-run `kubectl apply -k`. |

## Development

```bash
python3 test_agent.py   # self-check: readiness, Prometheus parsing and types, selectors, thresholds, OTLP payload
```

There's no build step and no dependencies. Edit `agent.py` or `checks.json` and re-apply the kustomization. The ConfigMap name includes a hash of its contents, so the pod restarts with the new version. Tag a release (`v0.x.y`) so cluster overlays can pin to it.

## Testing on minikube

`test/minikube/` runs the agent on a local minikube with the same collector chart used on EKS. minikube has none of the EKS add-ons, so it uses its own checks (`test/minikube/checks.json`):
- **coredns:** the Deployment, internal and external DNS lookups, and a metrics check against CoreDNS's own Prometheus port (9153). This exercises pod discovery, scraping, forwarding and thresholds.
- **kube-proxy:** a DaemonSet check.
- **argocd:** the same Argo CD checks as `checks.json` (workloads plus metrics scrapes on :8082 and :8084, through Argo CD's own NetworkPolicies). Install Argo CD first (step 1b).

To exercise the down path, scale something down, for example `kubectl -n argocd scale deploy/argocd-redis --replicas=0`. Then scale it back up.

**1. Install the collector** (once). Use a dedicated INGEST-only token, stored as a Secret so it never lands in git:

```bash
kubectl create namespace splunk-otel
kubectl -n splunk-otel create secret generic splunk-otel-collector \
  --from-literal=splunk_observability_access_token=<INGEST_TOKEN>
helm repo add splunk-otel-collector-chart https://signalfx.github.io/splunk-otel-collector-chart
helm upgrade --install otel splunk-otel-collector-chart/splunk-otel-collector --version 0.161.0 \
  -n splunk-otel -f test/minikube/otel-values.yaml --wait
```

Use **Helm 3.8 or newer**. Older Helm (for example the 3.7 bundled with Rancher Desktop) fails to render this chart with `len of nil pointer`.

`otel-values.yaml` differs from what you'd use on EKS in only two minikube workarounds, both commented:
- **Kubelet TLS:** it skips TLS verification to the kubelet, because minikube's kubelet certificate has no IP address in it.
- **Control-plane metrics:** it turns off controller-manager and scheduler metrics, because minikube binds them to localhost. On EKS the control plane isn't visible anyway.

**1b. Install Argo CD** (once), the upstream manifests pinned to a release:

```bash
kubectl create namespace argocd
kubectl apply -n argocd --server-side -f https://raw.githubusercontent.com/argoproj/argo-cd/v3.5.3/manifests/install.yaml
```

**1c. Give Argo CD an app** (once), so it has something to report on. `test/minikube/argocd-app.yaml` is Argo CD's public guestbook example (no git credentials needed), synced automatically into the `guestbook` namespace:

```bash
kubectl apply -f test/minikube/argocd-app.yaml
kubectl -n argocd get app guestbook   # SYNC STATUS Synced, HEALTH STATUS Healthy
```

Without an app, the `argocd_app_info`, `argocd_app_sync_total` and `argocd_git_request_total` series don't exist.

**2. Deploy the agent** from your working copy. This overlay sits inside the base's directory, so it references the base files directly and needs the relaxed load restrictor:

```bash
kubectl kustomize --load-restrictor LoadRestrictionsNone test/minikube | kubectl apply -f -
```

**3. Check:**
- **Agent:** 3 pods Ready, exactly one holding the Lease. The **leader's** logs (`kubectl -n eks-status logs "$(kubectl -n eks-status get lease eks-health-agent -o jsonpath='{.spec.holderIdentity}')"`) should show only `became leader` once Argo CD is ready. Every failing check logs one line per cycle.
- **Splunk:** within a minute or two, `eks.health.up` with `cluster:minikube` should show 11 series (4 coredns, 1 kube-proxy, 6 argocd), all at 1. `coredns_dns_requests_total` should arrive as a cumulative counter, and Argo CD's `workqueue_depth` as a gauge. With the guestbook app, `argocd_app_info` should also appear, with `name=guestbook`, `health_status=Healthy` and `sync_status=Synced`. So should the counters `argocd_app_sync_total` (`phase=Succeeded`) and `argocd_git_request_total` (`fetch` and `ls-remote`).

**Testing an unhealthy Argo CD app.** Point the guestbook at an image tag that doesn't exist:

```bash
kubectl -n argocd patch app guestbook --type merge \
  -p '{"spec":{"source":{"kustomize":{"images":["gcr.io/google-samples/gb-frontend:does-not-exist"]}}}}'
```

What happens:
- **At once:** the new pod can't pull its image. The app turns **Progressing**, and the dashboard's **Argo CD apps not Healthy** chart shows it within about a minute.
- **After about 10 minutes** (the Deployment's progress deadline): Argo CD marks the app **Degraded**. The detector's **EKS Argo CD app degraded** rule would fire 10 minutes after that, if the detector is applied.
- **The `argocd` service stays up.** Argo CD itself is healthy; only an app it manages is broken.

Restore it:

```bash
kubectl -n argocd patch app guestbook --type merge -p '{"spec":{"source":{"kustomize":null}}}'
```
- **Status page:** run it locally against the same org. It should show a `minikube` cluster with argocd, coredns and kube-proxy up. The 30-day history bars stay grey for about the first hour, until Splunk has hourly rollups.

**Testing failover.** These are the two scenarios verified on minikube:

```bash
# Graceful: delete the leader. It releases the Lease and a standby takes over within ~1-10 s.
kubectl -n eks-status delete pod "$(kubectl -n eks-status get lease eks-health-agent -o jsonpath='{.spec.holderIdentity}')"

# Frozen leader (like a hung node): stop its process from the node, so it can neither renew nor release.
# A signal from inside the container won't work: PID 1 ignores SIGSTOP sent from its own PID namespace.
L=$(kubectl -n eks-status get lease eks-health-agent -o jsonpath='{.spec.holderIdentity}')
CID=$(kubectl -n eks-status get pod $L -o jsonpath='{.status.containerStatuses[0].containerID}' | sed 's|containerd://||')
minikube ssh -- sudo kill -STOP "$(minikube ssh -- sudo crictl inspect --output go-template --template '{{.info.pid}}' $CID | tr -d '\r')"
kubectl -n eks-status get lease eks-health-agent -w    # new HOLDER after ~20-30 s
kubectl -n eks-status get pod $L -w                    # RESTARTS 1 after ~1 min (liveness), then back as standby
```

To iterate, edit `agent.py` or the checks and re-run step 2. The ConfigMap hash changes, so the pod restarts with the new code.
