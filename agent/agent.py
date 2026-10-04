"""k8s-health agent: every INTERVAL_SECONDS runs the checks in CHECKS_FILE and pushes one `k8s.health.up`
gauge (1/0) per check, dims: cluster, service, check, over OTLP/HTTP (JSON) to the node-local Splunk OTel
Collector. `metrics` checks also forward selected Prometheus series.

Runs as several replicas; a Kubernetes Lease elects the one that collects. The others stand by and
take over if the leader's node breaks (it stops renewing) or can't reach its collector (it steps down)."""
import json
import math
import os
import re
import signal
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
METRIC = os.environ.get("METRIC_NAME", "k8s.health.up")
PATHS = {"deployment": "deployments", "statefulset": "statefulsets", "daemonset": "daemonsets"}
PROM_LINE = re.compile(r"^([a-zA-Z_:][\w:]*)(?:\{(.*)\})?\s+(\S+)")
PROM_LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')
LEASE = os.environ.get("LEASE_NAME", "k8s-health-agent")  # one per install; the Helm chart sets it
LEASE_SECONDS = int(os.environ.get("LEASE_DURATION_SECONDS", "30"))  # a dead leader is replaced after this
RENEW_SECONDS = 10  # how often every replica tries to acquire/renew
SEND_FAILURES_TO_STEP_DOWN = 3  # consecutive failed sends to this node's collector


def ready(kind, obj):
    s = obj.get("status", {})
    if kind == "daemonset":
        want, have = s.get("desiredNumberScheduled", 0), s.get("numberReady", 0)
    else:
        want, have = obj.get("spec", {}).get("replicas", 1), s.get("readyReplicas", 0)
    return want > 0 and have >= want


def bracket(host):
    return f"[{host}]" if ":" in host else host  # IPv6


def k8s(path, method="GET", body=None, raw=False):
    # Talk to the API by IP, not kubernetes.default.svc, so a CoreDNS outage doesn't fail every check.
    url = f"https://{bracket(os.environ['KUBERNETES_SERVICE_HOST'])}:{os.environ['KUBERNETES_SERVICE_PORT']}{path}"
    with open(f"{SA}/token") as f:  # re-read: projected tokens rotate
        req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + f.read(), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, context=ssl.create_default_context(cafile=f"{SA}/ca.crt"), timeout=5) as r:
        return r.read().decode() if raw else json.load(r)


def parse_prom(text):
    """Prometheus text format -> [(name, {labels}, value, splunk_type)]."""
    types, out = {}, []
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            _, _, family, typ = line.split(maxsplit=3)
            types[family] = typ.strip()
        elif m := PROM_LINE.match(line):
            out.append((m[1], dict(PROM_LABEL.findall(m[2] or "")), float(m[3]), splunk_type(m[1], types)))
    return out


def splunk_type(name, types):
    """Use the endpoint's declared # TYPE; name suffixes lie (e.g. gauges called *_total)."""
    if types.get(name) == "counter":
        return "cumulative_counter"
    for suffix in ("_total", "_bucket", "_sum", "_count"):
        if name.endswith(suffix) and types.get(name[: -len(suffix)]) in ("counter", "histogram", "summary"):
            return "cumulative_counter"
    return "gauge"


def matches(selector, name, labels):
    """selector is `name` or `name{label="value",...}`; the series must have all listed labels."""
    m = PROM_LINE.match(selector + " 0")
    return m[1] == name and dict(PROM_LABEL.findall(m[2] or "")).items() <= labels.items()


def breach(op, value, limit, now):
    if op == "max":
        return value > limit
    if op == "min":
        return value < limit
    return now - value > limit  # max_age: value is a unix timestamp


def metrics_targets(t):
    """{"url": ...} or {"namespace", "selector", "port"} (scrapes every running pod by IP) -> [(url, dims)]."""
    if "url" in t:
        return [(t["url"], {})]
    q = urllib.parse.urlencode({"labelSelector": t["selector"], "fieldSelector": "status.phase=Running"})
    pods = [p for p in k8s(f"/api/v1/namespaces/{t['namespace']}/pods?{q}")["items"] if p["status"].get("podIP")]
    if not pods:
        raise RuntimeError("no running pods match selector")
    return [(f"http://{bracket(p['status']['podIP'])}:{t['port']}{t.get('path', '/metrics')}", {"pod": p["metadata"]["name"]})
            for p in pods]


def scrape(c, out):
    """Appends `forward`ed series to out as (name, value, dims, splunk_type); raises if a threshold is breached.
    Thresholds: max / min / max_age (seconds since a unix-timestamp gauge). min and max_age also fail
    when no series matches, since they assert something must be present; max passes on absence."""
    rules = [(op, sel, limit) for op in ("max", "min", "max_age") for sel, limit in c.get(op, {}).items()]
    bad = []
    for url, dims in metrics_targets(c["metrics"]):
        where = dims.get("pod", url)
        with urllib.request.urlopen(url, timeout=5) as r:
            series = parse_prom(r.read().decode())
        now, seen = time.time(), set()
        for name, labels, value, typ in series:
            if any(matches(sel, name, labels) for sel in c.get("forward", [])) and math.isfinite(value):  # NaN isn't JSON
                out.append((name, value, {**labels, **dims}, typ))
            for op, sel, limit in rules:
                if matches(sel, name, labels):
                    seen.add(sel)
                    if breach(op, value, limit, now):
                        bad.append(f"{where} {name}{labels}={value} breaches {op} {limit}")
        bad += [f"{where} {sel} missing" for op, sel, _ in rules if op != "max" and sel not in seen]
    if bad:
        raise RuntimeError("; ".join(bad))


def not_ready_nodes(nodes, now, ignore_younger_than):
    """Names of nodes whose Ready condition isn't True, skipping nodes still joining (autoscaler churn)."""
    bad = []
    for n in nodes:
        created = datetime.strptime(n["metadata"]["creationTimestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if now - created.timestamp() < ignore_younger_than:
            continue
        ready = next((x.get("status") for x in n.get("status", {}).get("conditions", []) if x.get("type") == "Ready"), "Unknown")
        if ready != "True":
            bad.append(n["metadata"]["name"])
    return bad


def run_check(c, out):
    """Raises if the check fails. Metrics checks append forwarded series to out."""
    if "metrics" in c:
        return scrape(c, out)
    if "dns" in c:
        socket.getaddrinfo(c["dns"], 443)
        return
    if "apiserver" in c:
        k8s(c["apiserver"], raw=True)  # e.g. /readyz: 200 "ok", or an HTTPError
        return
    if "nodes" in c:
        opts = c["nodes"] or {}
        bad = not_ready_nodes(k8s("/api/v1/nodes")["items"], time.time(), opts.get("ignore_younger_than_seconds", 300))
        if len(bad) > opts.get("max_not_ready", 0):
            raise RuntimeError(f"{len(bad)} node(s) not Ready: {', '.join(bad[:10])}")
        return
    obj = k8s(f"/apis/apps/v1/namespaces/{c['namespace']}/{PATHS[c['kind']]}/{c['name']}")
    if not ready(c["kind"], obj):
        raise RuntimeError(f"not ready: {obj.get('status')}")


def label_of(c):
    if "apiserver" in c:
        return f"apiserver{c['apiserver']}"
    if "nodes" in c:
        return "nodes/ready"
    if "metrics" in c:
        t = c["metrics"]
        return f"metrics/{t['url']}" if "url" in t else f"metrics/{t['namespace']}/{t['selector']}"
    return f"dns/{c['dns']}" if "dns" in c else f"{c['kind']}/{c['namespace']}/{c['name']}"


def otlp(points, ts_ns):
    """[(name, value, dims, splunk_type)] -> OTLP/HTTP JSON export request. The collector's signalfx exporter
    turns gauges into gauges and monotonic cumulative sums into cumulative counters."""
    metrics = {}
    for name, value, dims, typ in points:
        if name not in metrics:
            data = ({"sum": {"aggregationTemporality": 2, "isMonotonic": True, "dataPoints": []}}  # 2 = CUMULATIVE
                    if typ == "cumulative_counter" else {"gauge": {"dataPoints": []}})
            metrics[name] = {"name": name, **data}
        points_list = (metrics[name].get("sum") or metrics[name]["gauge"])["dataPoints"]
        points_list.append({"timeUnixNano": str(ts_ns), "asDouble": float(value),
                            "attributes": [{"key": k, "value": {"stringValue": str(v)}} for k, v in dims.items()]})
    return {"resourceMetrics": [{"resource": {}, "scopeMetrics": [
        {"scope": {"name": "k8s-health-agent"}, "metrics": list(metrics.values())}]}]}


def log(level, msg, **kw):
    print(json.dumps({"level": level, "msg": msg, **kw}), flush=True)


def micro(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")  # k8s MicroTime


def parse_micro(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()


class Elector:
    """Leader election on a coordination.k8s.io Lease, like client-go's leaderelection. Updates use the
    Lease's resourceVersion, so two replicas can't both win a takeover: the loser gets 409 Conflict."""

    def __init__(self, namespace, identity):
        self.collection = f"/apis/coordination.k8s.io/v1/namespaces/{namespace}/leases"
        self.path = f"{self.collection}/{LEASE}"
        self.identity = identity
        self.renewed = 0.0  # when we last renewed as holder
        self.api_ok = 0.0  # when the API server last answered us (readiness)
        self.ticked = time.time()  # when the election loop last ran (liveness)
        self.hold_off_until = 0.0  # after stepping down, don't try to re-acquire before this
        self.lock = threading.RLock()

    def is_leader(self, now=None):
        # Stop acting one renew period before the lease can expire, so we never overlap a successor.
        return (now or time.time()) - self.renewed < LEASE_SECONDS - RENEW_SECONDS

    def tick(self, now=None):
        now = now or time.time()
        with self.lock:
            self.ticked = time.time()
            spec = {"holderIdentity": self.identity, "leaseDurationSeconds": LEASE_SECONDS, "renewTime": micro(now)}
            try:
                lease = k8s(self.path)
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
                self.api_ok = now
                if now >= self.hold_off_until:
                    k8s(self.collection, "POST", {"apiVersion": "coordination.k8s.io/v1", "kind": "Lease",
                                                  "metadata": {"name": LEASE},
                                                  "spec": {**spec, "acquireTime": micro(now), "leaseTransitions": 0}})
                    self._won(now)
                return
            self.api_ok = now
            cur = lease.get("spec", {})
            mine = cur.get("holderIdentity") == self.identity
            expired = (not cur.get("holderIdentity") or not cur.get("renewTime")
                       or parse_micro(cur["renewTime"]) + cur.get("leaseDurationSeconds", LEASE_SECONDS) < now)
            if not mine and (not expired or now < self.hold_off_until):
                self._lost()
                return
            if not mine:
                spec.update(acquireTime=micro(now), leaseTransitions=cur.get("leaseTransitions", 0) + 1)
            lease["spec"] = {**cur, **spec}
            try:
                k8s(self.path, "PUT", lease)  # metadata.resourceVersion makes this compare-and-swap
            except urllib.error.HTTPError as e:
                if e.code == 409:  # someone else updated it first
                    self._lost()
                    return
                raise
            self._won(now)

    def release(self, hold_off=0.0, now=None):
        """Give up the lease now (shutdown, or this node can't send) so another replica takes over at its next tick."""
        now = now or time.time()
        with self.lock:
            self.hold_off_until = now + hold_off
            if not self.renewed:
                return
            self.renewed = 0.0
            try:
                lease = k8s(self.path)
                if lease.get("spec", {}).get("holderIdentity") == self.identity:
                    lease["spec"].update(holderIdentity=None, leaseDurationSeconds=1, renewTime=micro(now))
                    k8s(self.path, "PUT", lease)
                log("info", "released leadership", identity=self.identity, hold_off_seconds=hold_off)
            except Exception as e:  # it will expire on its own
                log("warn", "lease release failed", error=str(e))

    def _won(self, now):
        if not self.is_leader(now):
            log("info", "became leader", identity=self.identity)
        self.renewed = now

    def _lost(self):
        if self.renewed:
            log("info", "lost leadership", identity=self.identity)
        self.renewed = 0.0

    def run(self):
        while True:
            try:
                self.tick()
            except Exception as e:
                log("warn", "leader election failed", error=str(e))
            time.sleep(RENEW_SECONDS)


def is_missing(err):
    return isinstance(err, urllib.error.HTTPError) and err.code == 404


def collect(services, cluster, absent):
    """Run every service's checks -> datapoints. A service in mode "auto" whose workload checks all 404
    isn't installed in this cluster: it's skipped (no datapoints) until one of its workloads appears.
    `absent` carries that state between cycles so the change is logged once."""
    points = []
    for service, spec in services.items():
        results = []
        for c in spec["checks"]:
            extra = []
            try:
                run_check(c, extra)
                results.append((c, None, extra))
            except Exception as e:
                results.append((c, e, extra))
        workloads = [err for c, err, _ in results if "kind" in c]
        if spec.get("mode") == "auto" and workloads and all(is_missing(err) for err in workloads):
            if service not in absent:
                log("info", "not installed, skipping (mode auto)", service=service)
                absent.add(service)
            continue
        if service in absent:
            log("info", "now installed, reporting", service=service)
            absent.discard(service)
        base = {"cluster": cluster, "service": service}
        for c, err, extra in results:
            if err:
                log("warn", "check failed", service=service, check=label_of(c), error=str(err))
            points.append((METRIC, 0 if err else 1, {**base, "check": label_of(c)}, "gauge"))
            points += [(name, value, {**dims, **base}, typ) for name, value, dims, typ in extra]
    return points


def send(endpoint, points, start):
    """True if the collector took the batch."""
    try:
        req = urllib.request.Request(f"{endpoint}/v1/metrics", data=json.dumps(otlp(points, int(start * 1e9))).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            rejected = json.loads(r.read() or b"{}").get("partialSuccess", {})
        if rejected.get("rejectedDataPoints"):
            log("error", "collector rejected datapoints", **rejected)
        return True
    except Exception as e:
        log("error", "ingest failed", error=str(e))
        return False


def health_server(elector, state, interval, port):
    """/readyz: the API server answers us (a replica that can't reach it can't lead or check anything).
    /healthz: the election loop is running and no check cycle is stuck; failing it gets the pod restarted."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            now = time.time()
            role = "leader" if elector.is_leader(now) else "standby"
            if self.path == "/readyz":
                ok = now - elector.api_ok < 3 * RENEW_SECONDS
            elif self.path == "/healthz":
                stuck = state["cycle_since"] and now - state["cycle_since"] > 3 * interval
                ok = now - elector.ticked < 3 * RENEW_SECONDS + 20 and not stuck  # +20: a tick's own API timeouts
            else:
                ok = None
            body = json.dumps({"ok": ok, "role": role}).encode()
            self.send_response(404 if ok is None else 200 if ok else 503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # no access log for probes
            pass

    server = ThreadingHTTPServer(("", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    cluster = os.environ["CLUSTER_NAME"]
    interval = int(os.environ.get("INTERVAL_SECONDS", "60"))
    # Default: OTLP/HTTP on the Splunk OTel Collector agent on this node (hostNetwork, :4318); it adds its own token.
    endpoint = os.environ.get("OTLP_ENDPOINT") or f"http://{bracket(os.environ['SPLUNK_OTEL_AGENT'])}:4318"
    with open(os.environ.get("CHECKS_FILE", "/etc/k8s-health/checks.json")) as f:
        services = json.load(f)["services"]
    with open(f"{SA}/namespace") as f:
        namespace = f.read().strip()

    elector = Elector(namespace, os.environ.get("POD_NAME") or socket.gethostname())
    state = {"cycle_since": 0.0, "next_run": 0.0, "failures": 0}
    absent = set()
    log("info", "starting", cluster=cluster, identity=elector.identity, lease=LEASE, metric=METRIC,
        services={name: spec.get("mode", "always") for name, spec in services.items()})

    def shutdown(*_):
        # As PID 1, Python ignores SIGTERM without a handler. Hand the lease over so a standby takes over
        # within one renew period instead of waiting for it to expire, then exit.
        elector.release()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    health_server(elector, state, interval, int(os.environ.get("HEALTH_PORT", "8080")))
    threading.Thread(target=elector.run, daemon=True).start()

    while True:
        step(elector, state, services, cluster, endpoint, interval, absent)
        time.sleep(1)


def step(elector, state, services, cluster, endpoint, interval, absent):
    """One pass of the main loop: if this replica leads and a cycle is due, run the checks and send them."""
    if not (elector.is_leader() and time.time() >= state["next_run"]):
        return
    start = time.time()
    state["next_run"] = start + interval
    state["cycle_since"] = start
    ok = send(endpoint, collect(services, cluster, absent), start)
    state["cycle_since"] = 0.0
    state["failures"] = 0 if ok else state["failures"] + 1
    if state["failures"] >= SEND_FAILURES_TO_STEP_DOWN:
        # This node's collector is unreachable: let a replica on another node take over.
        log("warn", "stepping down: cannot reach this node's collector", failures=state["failures"])
        elector.release(hold_off=2 * LEASE_SECONDS)
        state["failures"] = 0


if __name__ == "__main__":  # pragma: no cover
    main()
