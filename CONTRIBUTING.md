# Contributing to k8s-health

Thanks for helping. The most useful contributions are usually **add-on profiles** (checks for a component this project doesn't cover yet) and **"tested on" reports** for the existing ones.

## Repository layout

| Path | What |
|---|---|
| `agent/` | The in-cluster agent: one Python file, **standard library only**, plus its tests and Dockerfile |
| `status-page/` | The status page: a small Flask app (`status_page.py`, `templates/`, `static/`), its tests and Dockerfile |
| `charts/k8s-health/` | The Helm chart. Add-on profiles live in its `values.yaml`. |
| `splunk/` | Terraform for the detectors and dashboard |
| `dev/minikube/` | Files for a local test environment |
| `.github/workflows/ci.yaml` | Tests, chart lint, Terraform validation, and image and chart publishing |

## Tests

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/coverage run --source=agent -m pytest agent && .venv/bin/coverage report
.venv/bin/coverage run --source=status-page -m pytest status-page && .venv/bin/coverage report
helm plugin install https://github.com/helm-unittest/helm-unittest --version v1.2.1 --verify=false   # once
helm unittest charts/k8s-health
helm lint charts/k8s-health --set clusterName=dev
(cd splunk && terraform fmt -check -recursive && terraform init -backend=false && terraform validate && tflint --init --config=../.tflint.hcl && tflint --config=../.tflint.hcl)
```

**Code coverage:** the agent and the status page must each keep **at least 90% line coverage**. Current numbers are in the README badges, and the line-by-line reports are at https://jthiatt.github.io/k8s-health/. The bar is `fail_under` in `pyproject.toml`, and CI fails below that. Add or extend a test with every change. The tests use pytest and stub the Kubernetes API, Splunk API and HTTP calls, so they run offline in about a second.

**Chart tests:** the Helm chart's templates have [helm-unittest](https://github.com/helm-unittest/helm-unittest) suites in `charts/k8s-health/tests/`. Cover any template change there too.

## The contract between the parts

The agent, status page, detectors and dashboard never talk to each other directly; they meet in Splunk. Changing any of these means changing all the parts that use it, in the same pull request:

| Contract | Defined in |
|---|---|
| Metric `k8s.health.up`, a gauge: 1 = check passed, 0 = failed | `metricName` (chart), `METRIC_NAME` (agent and page), `metric_name` (Terraform) |
| Dimensions `cluster`, `service`, `check` | `agent/agent.py`; read by `status-page/status_page.py` and the Terraform charts and detectors |
| One datapoint per check per `intervalSeconds` | Chart. The page's stale threshold is 3 intervals. |
| Detector names contain `k8s-health` | Terraform `name_prefix`; the status page's `detectorMatch` |

## Local environment (minikube)

These are the steps used to test the project.

```bash
minikube start
# 1. Splunk OTel Collector (requirement 3), with your INGEST token
kubectl create namespace splunk-otel
kubectl -n splunk-otel create secret generic splunk-otel-collector --from-literal=splunk_observability_access_token=<INGEST token>
helm repo add splunk-otel-collector-chart https://signalfx.github.io/splunk-otel-collector-chart
helm install otel splunk-otel-collector-chart/splunk-otel-collector -n splunk-otel -f dev/minikube/otel-values.yaml --wait

# 2. Build images straight into minikube, tagged as the chart expects
minikube image build -t ghcr.io/jthiatt/k8s-health-agent:0.1.0 agent
minikube image build -t ghcr.io/jthiatt/k8s-health-status-page:0.1.0 status-page

# 3. Install from your checkout
kubectl create namespace k8s-health
kubectl -n k8s-health create secret generic k8s-health-splunk-api --from-literal=token=<API token>
helm install k8s-health charts/k8s-health -n k8s-health --set clusterName=minikube \
  --set statusPage.enabled=true --set statusPage.splunk.realm=<realm> --set statusPage.splunk.existingSecret=k8s-health-splunk-api
```

**After changing code:** rebuild with a **new tag** (e.g. `:dev2`), then `helm upgrade ... --reset-then-reuse-values --set agent.image.tag=dev2`. After changing only `values.yaml`, `helm upgrade --reset-then-reuse-values` is enough; plain `--reuse-values` would keep the old defaults. With `IfNotPresent`, reusing a tag keeps the old image.

**minikube quirks:**
- `dev/minikube/otel-values.yaml` works around two minikube-only collector issues, both commented there.
- If your machine sleeps, minikube's API and node take a minute or two to recover after it wakes.

**Exercising an add-on:**
- **Install the component:** for example Argo CD from its upstream manifests. Then give it an app with `kubectl apply -f dev/minikube/argocd-app.yaml`, so its app metrics exist.
- **Check detection:** the leader logs `now installed, reporting` for the service.
- **Test the down path:** scale one of the component's workloads to 0.

**Failover tests** (delete the leader; freeze it from the node with `kill -STOP`): see the git history of `agent/` for the exact commands used. A signal sent from *inside* the container doesn't work, because PID 1 ignores SIGSTOP from its own PID namespace.

## Adding an add-on profile

1. **Write the profile** under `agent.profiles` in `charts/k8s-health/values.yaml`, following the existing ones:
   - `enabled: auto`
   - `checks`: the component's core workloads, with the **namespace and names its official Helm chart or manifests use by default**.
   - `metricsChecks`: optional. Set `metrics: true` only if the component exposes metrics without extra configuration.
2. **Keep forwarded series few.** Only forward series that something charts or alerts on, and avoid labels with unbounded values.
3. **Optionally, alert on it:** add a rule to `splunk/detector.tf` and a chart to `splunk/dashboard.tf`.
4. **Add it to the README's** "What gets checked" table. Put where you tested it under **Tested on**, or `not yet`.
5. **Test it:** install the component somewhere (minikube or kind is fine). Show the leader detecting it and all checks passing, then a failure after scaling a workload to 0. Paste the logs in the pull request.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Report unacceptable behavior to the address given there.

## Reporting security problems

Please don't open public issues for those. See [SECURITY.md](SECURITY.md).

## Pull requests

- Keep the agent standard-library only, so its image stays tiny and has no dependencies to patch.
- Add or extend tests with every change, and keep both components at 90%+ coverage. The existing tests show the style: pytest, small fakes for the Kubernetes and Splunk APIs, no network.
- Update the README in the same pull request when behavior or values change.

## Releasing

Push a tag `vX.Y.Z`. CI then:
- builds and pushes `ghcr.io/<owner>/k8s-health-agent:X.Y.Z` and `ghcr.io/<owner>/k8s-health-status-page:X.Y.Z` (amd64 and arm64);
- publishes the chart, versioned `X.Y.Z` with `appVersion` `X.Y.Z`, to `oci://ghcr.io/<owner>/charts/k8s-health`.

**After the first release,** make the two image packages and the chart package **public** in GitHub's package settings. Packages pushed from a private repository start out private.

Pushes to `main` also publish `edge` and `sha-<commit>` image tags.
