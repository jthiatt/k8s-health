---
name: minikube-setup
description: Guide a new user through running k8s-health end to end on a local minikube cluster - prerequisites, minikube, Splunk Observability tokens, the Splunk OTel Collector, the k8s-health agent, the status page, and the Terraform alerts and dashboard - verifying each step before moving on. Use when someone wants to try, develop, or demo k8s-health locally, or asks how to set the project up from scratch.
---

# Set up k8s-health on minikube

Walk the user through the phases below **in order**. Each phase ends with a **Verify** step: run it, and don't
move on until it passes. When something fails, check **Troubleshooting** at the bottom first: every entry there
is a problem someone actually hit setting this project up.

Run commands yourself when you can. Only the user can do the Splunk UI steps, and the user always supplies the
secrets. Paths are relative to the repo root.

## Ground rules

- **Never ask the user to paste a token into the chat**, and never echo one: it ends up in the transcript.
  - Have them save each token to a file outside the repo (phase 2), then read it with `tr -d '\n\r' < file`.
    `kubectl create secret --from-file` keeps the trailing newline an editor adds, and a token with a newline
    breaks auth with confusing errors.
  - Never pass a token on the command line in a way that gets printed, and never write one into a file in the repo.
- **Ask, don't assume,** for things only the user knows:
  - their Splunk **realm** (e.g. `us1`, `eu0`; under Settings → Organization);
  - which email or notification target the dead man's switch should alert.
- **Before replacing anything, look first.** Ask before:
  - deleting or recreating a minikube profile they already have;
  - overwriting an existing `splunk/terraform.tfvars`.
- The **Terraform phase creates real detectors and a dashboard** in the user's Splunk org. Confirm before
  `terraform apply`, and show them the plan summary first.

## Phase 0: Prerequisites

Check each one, and report what's missing with the install link. Don't install system software without asking.

| Tool | Check | Needed for |
|---|---|---|
| Docker (Desktop, Colima, or Engine), running | `docker info` | the minikube driver |
| minikube | `minikube version` | the cluster |
| kubectl | `kubectl version --client` | everything |
| Helm **3.14 or newer** | `helm version --short` | `--reset-then-reuse-values`; some add-on charts fail to render on older Helm |
| Terraform **1.5 or newer** | `terraform version` | phase 6 only (alerts and dashboard) |
| A Splunk Observability Cloud account | ask | metrics, the status page, alerts |

**Resources:** give minikube at least **4 CPUs and 8 GB**. The base setup needs about 3 GB, but trying several
add-on profiles at once (Prometheus, Istio, ...) needs the headroom. Docker Desktop's own memory limit must be
higher than what minikube asks for.

**macOS:** if the Mac sleeps, minikube freezes and every check goes stale. For long sessions, suggest running
`caffeinate -ims` in another terminal.

## Phase 1: minikube

```bash
minikube start --driver=docker --cpus=4 --memory=8g
kubectl get nodes
```

If a `minikube` profile already exists, ask whether to reuse it. If it's broken, they may prefer to
`minikube delete` it, but that's their call: it destroys everything in it.

**Verify:** the node is `Ready`, and `kubectl get pods -n kube-system` shows CoreDNS running.

## Phase 2: Splunk tokens (the user does this in the Splunk UI)

Ask the user to create these under **Settings → Access Tokens**, and to save each one to a file outside the repo,
e.g. in `~/.k8s-health/`.

| File | Token type | Used by |
|---|---|---|
| `~/.k8s-health/ingest` | **INGEST** | The Splunk OTel Collector, which sends all metrics. The k8s-health agent needs no token. |
| `~/.k8s-health/api` | **API** (read) | The status page, which reads metrics and incidents |
| `~/.k8s-health/admin` | A token that can create detectors and dashboards (e.g. a user API token with admin rights) | Terraform, phase 6 only |

Also ask for their **realm**.

**Verify (without printing the tokens):**

```bash
for f in ingest api; do printf '%s: ' $f; [ -s ~/.k8s-health/$f ] && echo present || echo MISSING; done
curl -s -o /dev/null -w 'api token: HTTP %{http_code}\n' -H "X-SF-Token: $(tr -d '\n\r' < ~/.k8s-health/api)" \
  "https://api.<realm>.signalfx.com/v2/metrictimeseries?query=sf_metric:cpu.utilization&limit=1"
```

`200` means the API token and realm are right. `401` means a wrong token or realm.

## Phase 3: Splunk OTel Collector

The agent sends OTLP to the collector on its own node (`<node IP>:4318`), exactly as on a real cluster.
`dev/minikube/otel-values.yaml` has the chart settings plus two minikube-only workarounds, explained in its
comments.

```bash
kubectl create namespace splunk-otel
kubectl -n splunk-otel create secret generic splunk-otel-collector \
  --from-literal=splunk_observability_access_token="$(tr -d '\n\r' < ~/.k8s-health/ingest)"
helm repo add splunk-otel-collector-chart https://signalfx.github.io/splunk-otel-collector-chart
helm repo update splunk-otel-collector-chart
helm install otel splunk-otel-collector-chart/splunk-otel-collector -n splunk-otel \
  -f dev/minikube/otel-values.yaml --set splunkObservability.realm=<realm> --wait
```

Chart **0.161.0** is the tested version. Add `--version 0.161.0` if a newer one misbehaves.

**Verify:**
1. The collector pods are Running: `kubectl -n splunk-otel get pods`.
2. Their logs show no 401 or 403: `kubectl -n splunk-otel logs ds/otel-splunk-otel-collector-agent --tail=50`.
3. The probe returns `200`:

```bash
NODE_IP=$(kubectl get node minikube -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')
kubectl run otlp-probe --rm -i --restart=Never --image=curlimages/curl -- \
  curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' \
  -d '{"resourceMetrics":[]}' "http://$NODE_IP:4318/v1/metrics"
```

## Phase 4: The k8s-health agent

From the latest release (what a user would run):

```bash
helm install k8s-health oci://ghcr.io/jthiatt/charts/k8s-health \
  -n k8s-health --create-namespace --set clusterName=minikube --wait
```

To install from this checkout instead (to test local chart changes), use `charts/k8s-health` in place of the
`oci://` reference.

**Verify:**

```bash
kubectl -n k8s-health get pods                         # 3 agent replicas, all Ready
LEADER=$(kubectl -n k8s-health get lease k8s-health-agent -o jsonpath='{.spec.holderIdentity}')
kubectl -n k8s-health logs "$LEADER"
```

- The leader logs `became leader` and a `starting` line listing the services it checks.
- On a bare minikube, add-ons log `not installed, skipping`. That's expected: profiles in `auto` mode report
  only what's installed.
- Within about 2 minutes `k8s.health.up` with `cluster=minikube` should be in Splunk. Check with the API token:

```bash
curl -s -H "X-SF-Token: $(tr -d '\n\r' < ~/.k8s-health/api)" \
  "https://api.<realm>.signalfx.com/v2/metrictimeseries?query=sf_metric:k8s.health.up%20AND%20cluster:minikube" \
  | python3 -c "import json,sys; print(sorted({x['dimensions']['service'] for x in json.load(sys.stdin)['results']}))"
```

Expect `['coredns', 'kube-proxy', 'kubernetes']` on a bare minikube.

## Phase 5: The status page

```bash
kubectl -n k8s-health create secret generic k8s-health-splunk-api \
  --from-literal=token="$(tr -d '\n\r' < ~/.k8s-health/api)"
helm upgrade k8s-health oci://ghcr.io/jthiatt/charts/k8s-health -n k8s-health --reset-then-reuse-values \
  --set statusPage.enabled=true --set statusPage.splunk.realm=<realm> \
  --set statusPage.splunk.existingSecret=k8s-health-splunk-api --wait
kubectl -n k8s-health port-forward svc/k8s-health-status-page 8080:80
```

**Verify:**
- `curl -s localhost:8080/api/status` returns `"overall": "up"` and a `minikube` cluster.
- Point the user at http://localhost:8080. The page has no login, so don't expose it beyond the laptop without SSO
  in front of it.

## Phase 6: Alerts and dashboard (Terraform, creates real objects in Splunk)

```bash
cd splunk
cp terraform.tfvars.example terraform.tfvars
```

In `terraform.tfvars`, set:
- `splunk_realm`;
- `deadman_notifications`, e.g. `["Email,<their address>"]`. Ask them for it.

The file is git-ignored. Then:

```bash
export TF_VAR_splunk_token="$(tr -d '\n\r' < ~/.k8s-health/admin)"
terraform init
terraform plan          # summarize for the user and confirm before applying
terraform apply
```

**Verify:**
- The apply prints `dashboard_url`. Open it: the **Cluster health** dashboard should show `minikube`.
- Two detectors now exist: `k8s-health core services` and `k8s-health agent deadman`.

## Phase 7: Prove it works (offer this; about 10 minutes)

Run the core test tier. It reads every result from the status page, so it exercises the whole path:
agent → collector → Splunk → page, plus the alerts if phase 6 was done.

```bash
python3 dev/minikube/test.py core     # set HELM=/path/to/helm if the default helm is older than 3.14
```

It checks:
- the agents and their leader;
- a clean check cycle, and all core services up on the page;
- leader failover, with no gap in the data;
- an outage end to end. It adds a small `k8s-health-canary` service to the release (auto mode, so it's harmless
  when absent), breaks it, and expects "down" on the page plus a **Service down** incident, then recovery.

`--deadman` also stops every agent to test the dead man's switch. That takes about 10 minutes more and **sends a
real alert** to the `deadman_notifications` recipients, so tell the user first.

Don't demo an outage by scaling CoreDNS to 0. The collector also needs DNS to reach Splunk, so the page goes
stale instead of showing CoreDNS down.

To try **add-on profiles**, use the add-on tier. It tests one add-on at a time: install it from its official
chart, check it's detected, break one of its workloads, restore it, and uninstall it.

```bash
python3 dev/minikube/test.py list               # the add-ons it knows
python3 dev/minikube/test.py addons keda istio  # or no names for all of them (about 1.5 hours)
python3 dev/minikube/test.py cleanup            # remove every add-on and its CRDs
```

Don't install many add-ons at once on minikube. The API server and etcd slow down enough that checks time out.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Collector logs `401`/`403`, or the status page says `Splunk API error: HTTP Error 401` | A wrong token or realm, or a **trailing newline in the token**. Recreate the secret with `--from-literal=...="$(tr -d '\n\r' < file)"`, then restart the pod. |
| `helm install oci://...` or `helm pull` hangs forever | The Docker credential helper (`docker-credential-desktop`) is hanging. Run Helm with an empty config: `DOCKER_CONFIG=$(mktemp -d) HELM_REGISTRY_CONFIG=$(mktemp) helm ...`. The charts are public, so no login is needed. |
| `unknown flag: --reset-then-reuse-values` | Helm is older than 3.14; upgrade it. Plain `--reuse-values` is not a substitute: it keeps the old chart's defaults. |
| An add-on chart fails with `incompatible types for comparison` | Same cause: Helm too old. |
| `eval $(minikube docker-env)` then `docker build` hangs | minikube uses containerd. Build images into the cluster with `minikube image build -t <tag> <dir>` and install with `--set agent.image.pullPolicy=Never`. |
| Agent pods `OOMKilled` | You're running an image older than 0.2.0. Use 0.2.0 or later, which streams metrics. |
| Everything goes `stale` at once, and the deadman fires | minikube is frozen (the Mac slept) or the agents aren't running. Run `minikube status` and check the leader's logs. |
| No collector data at all; `kubelet_stats` TLS errors | The values file wasn't applied: install with `-f dev/minikube/otel-values.yaml`. |
| An add-on is missing from the page | It's not installed, or it uses non-default names or namespaces. The leader logs `not installed, skipping`. See the README's Troubleshooting section. |
| Many checks fail at once with `timed out`, and etcd logs `apply request took too long` | minikube is overloaded: too many add-ons installed at once, or Docker Desktop has more CPUs than the host has physical cores. Run `test.py cleanup`. In Docker Desktop → Resources, set CPUs to at most the physical core count, and memory at least 2 GB above minikube's. |
| Falco crashes with `BPF_TRACE_RAW_TP is not supported` | Docker Desktop's kernel can't run Falco's syscall driver. Fine for real clusters; skip Falco on minikube. |

For anything else, see the README's **Troubleshooting** section.

## Teardown

Ask before running any of these:
- `helm uninstall k8s-health -n k8s-health` and `helm uninstall otel -n splunk-otel` remove the workloads.
- `cd splunk && terraform destroy` removes the detectors and dashboard from Splunk.
- `minikube delete` removes the whole cluster.
- Tokens can be revoked in the Splunk UI. Delete `~/.k8s-health/` when done.
