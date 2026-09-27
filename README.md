# EKS Status Page

A status page for EKS clusters, built on Splunk Observability Cloud. It reads the health metrics that the [eks-health-agent](https://github.com/jthiatt/eks-health-agent) sends from every cluster, plus detector incidents, from the Splunk API. It shows them as a single page: overall status, 30-day daily uptime per service, and recent incidents.

It's a small Flask app served by gunicorn, packaged as a container image. It can run in a cluster or anywhere else that can reach `api.<realm>.signalfx.com`. Run **one** for all clusters.

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

- The agent deployed to your clusters and its detector applied (see the [eks-health-agent README](https://github.com/jthiatt/eks-health-agent)). Without them the page says "No data from any cluster agent".
- Docker and a container registry the cluster can pull from, such as ECR.
- `kubectl` 1.21+ if you run the page in a cluster.

## 2. Create the Splunk API token

In Splunk Observability: **Settings → Access Tokens → Create Token**, with the **API** scope (read). A user API token also works. Store it as Secret `splunk-api`, or pass it as an env var if the page runs outside a cluster.

## 3. Deploy

### Build and push the image

```bash
docker build -t <your-registry>/eks-status-page:0.1.0 .
docker push <your-registry>/eks-status-page:0.1.0
```

For ECR, first run `aws ecr create-repository --repository-name eks-status-page`, then log in with `aws ecr get-login-password | docker login --username AWS --password-stdin <account>.dkr.ecr.<region>.amazonaws.com`.

Then set `newName` and `newTag` under `images:` in `deploy/kustomization.yaml` to match.

The container runs gunicorn with **one worker and 8 threads**. Splunk responses are cached inside the process, so a single worker means Splunk is polled once no matter how many people have the page open. Keep `replicas: 1` for the same reason; a second replica would double the API calls.

### Option A: in a cluster

```bash
kubectl create namespace eks-status   # skip if the agent is already installed here
kubectl -n eks-status create secret generic splunk-api --from-literal=token=<API_TOKEN>
kubectl apply -k deploy/
kubectl -n eks-status port-forward svc/eks-status-page 8080:80   # then open http://localhost:8080
```

To expose it permanently, put a Traefik `IngressRoute` (or an Ingress) in front of `svc/eks-status-page`. **The page has no authentication of its own.** It shows cluster names and alert details, so put it behind SSO (for example Traefik ForwardAuth with oauth2-proxy) or keep it internal.

### Option B: outside the cluster

Run the same image anywhere with Docker:

```bash
docker run -d -p 8080:8080 -e SPLUNK_API_TOKEN=<API_TOKEN> -e SPLUNK_REALM=us1 -e DETECTOR_MATCH=eks \
  <your-registry>/eks-status-page:0.1.0
```

Or without Docker:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
SPLUNK_API_TOKEN=<API_TOKEN> DETECTOR_MATCH=eks \
  .venv/bin/gunicorn --bind 0.0.0.0:8080 --workers 1 --threads 8 status_page:app
```

### Endpoints

| Path | Returns |
|---|---|
| `/` | HTML status page. It refreshes itself every `INTERVAL_SECONDS`. |
| `/api/status` | The same data as JSON, for other tools. |
| `/healthz` | `ok`. Used as the readiness and liveness probe. It doesn't call Splunk. |

Current status and incidents are cached for 30 seconds. The 30-day history is cached for 10 minutes, since it changes slowly and is the largest query.

---

## How to read the page

The page has three parts.

**Header.** The overall state of every cluster:

| Header | When |
|---|---|
| ✓ All systems operational | Every service is up and no detector is firing. |
| ✓ (purple) Systems operational, N active alerts | Every service is up, but a detector is firing. |
| ! Some checks are not reporting | Nothing is down, but at least one check is stale. |
| ✕ N services down | At least one service is down. |
| ! No data from any cluster agent | No agent has ever reported. |

**Services, grouped by cluster.** Every check is a separate `eks.health.up` series, and a service's pill shows its worst check:

| Pill | Meaning |
|---|---|
| ✓ green (**up**) | Every check's latest value is 1. |
| ✕ red (**down**) | At least one check's latest value is 0. The failing checks are listed under the bar. |
| ! amber (**stale**) | At least one check hasn't reported within `STALE_AFTER_SECONDS` (default 3 × `INTERVAL_SECONDS`). The agent is down, can't reach the collector, or has fallen behind. |

The bar shows one block per UTC day for the last `HISTORY_DAYS`, oldest on the left. Hover a block for the date and exact figure.

| Block | Uptime that day |
|---|---|
| Green | ≥ 99.9% |
| Amber | ≥ 99% |
| Red | < 99% |
| Grey | No data (the agent wasn't installed yet, or didn't report all day) |

"Operation in last N days" is the average of the days that have data. Both it and the daily figures come from Splunk's hourly averages of the 0/1 gauge; Splunk rejects daily resolution, so the page adds up 24 hours per day itself.
- A service's day is its **worst** check that day.
- Figures are **rounded down**, so 99.995% shows as 99.99%, never 100%.
- **Hours with no data don't count as downtime.** An agent that stops reporting shows as stale right away, but doesn't lower the history.

**Recent incidents.** Detector incidents from the last `INCIDENT_DAYS`, ongoing ones first. A purple ✕ is ongoing; a ✓ is resolved. Each entry shows the rule name, detector, severity and timing, and links to the detector in Splunk.

The page and the agents must use the same `INTERVAL_SECONDS`, or the page will mark healthy checks as stale.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `SPLUNK_API_TOKEN` | — | API token (from Secret `splunk-api`). |
| `SPLUNK_REALM` | `us1` | API and detector-link realm. |
| `INTERVAL_SECONDS` | `60` | Must match the agents. Also the page refresh interval. |
| `STALE_AFTER_SECONDS` | 3 × interval | How long a check can go without reporting before it shows as stale. |
| `DETECTOR_MATCH` | `""` (all). The manifest sets `eks`. | Only incidents from detectors whose name contains this text (case-insensitive) are shown. |
| `HISTORY_DAYS` | `30` | Number of daily uptime blocks per service. |
| `INCIDENT_DAYS` | `7` | How far back resolved incidents are shown. Ongoing incidents are always shown. |
| `PORT` | `8080` | Listen port for `python status_page.py` (local development only). The image's gunicorn always listens on 8080. |

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| A whole cluster shows **stale**, or is missing | The agents or the collector are the problem, not the page. See the troubleshooting table in the [eks-health-agent README](https://github.com/jthiatt/eks-health-agent). |
| Page shows `Splunk API error: HTTP Error 401` | The API token is wrong, expired, or lacks the API scope. |
| Status page pod is `OOMKilled` | The 30-day history query grew too big (many clusters × checks). Raise the memory limit in `deploy/status-page.yaml` or lower `HISTORY_DAYS`. |
| History bars are all grey for a new install | Expected. History only starts once the agents start reporting. |
| Healthy checks flip to **stale** | `INTERVAL_SECONDS` here is lower than the agents' interval. |
| No incidents listed although detectors fired | The detector names don't contain `DETECTOR_MATCH` (`eks` in the manifest). |

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python test_status_page.py   # self-check: status merging, stale logic, daily history, page rendering
SPLUNK_API_TOKEN=<API_TOKEN> .venv/bin/python status_page.py   # dev server on :8080
```

- **Layout:** the Flask app is `status_page.py`, the page is `templates/index.html` (Jinja2), styles are in `static/style.css`, and the Kubernetes manifests are in `deploy/`.
- **Themes:** light and dark, following the viewer's OS setting. It works down to phone width.
- **Changing the page:** rebuild the image, bump the tag in `deploy/kustomization.yaml`, and re-apply.
