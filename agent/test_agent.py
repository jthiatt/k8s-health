"""Unit tests for agent.py. Run: pytest agent (CI also enforces >= 90% line coverage)."""
import copy
import io
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import agent
from agent import (LEASE_SECONDS, Elector, bracket, breach, collect, label_of, matches, micro, not_ready_nodes, otlp,
                   parse_micro, parse_prom, ready, run_check, scrape, send, splunk_type)

PROM = """# HELP workqueue_depth Current depth
# TYPE workqueue_depth gauge
workqueue_depth{name="kustomization"} 150
workqueue_depth{name="gitrepository"} 0
# TYPE controller_runtime_reconcile_errors_total counter
controller_runtime_reconcile_errors_total{controller="kustomization"} 7
# TYPE externalsecret_sync_calls_error counter
externalsecret_sync_calls_error{name="db"} 2
# TYPE external_dns_registry_endpoints_total gauge
external_dns_registry_endpoints_total 40
# TYPE rest_client_request_duration_seconds histogram
rest_client_request_duration_seconds_bucket{le="0.1"} 5
clustersecretstore_status_condition{name="aws",condition="Ready",status="True"} 1
clustersecretstore_status_condition{name="aws",condition="Ready",status="False"} 0
external_dns_controller_last_sync_timestamp_seconds 1.0e+09
go_gc_duration_seconds{quantile="0.5",note="a \\"q\\" b"} NaN
"""


def not_found(path):
    return urllib.error.HTTPError(path, 404, "not found", {}, None)


# --- Prometheus parsing and selectors -------------------------------------------------------------

def test_parse_prom_types_come_from_type_lines_not_suffixes():
    types = {n: t for n, _, _, t in parse_prom(PROM)}
    assert types["controller_runtime_reconcile_errors_total"] == "cumulative_counter"
    assert types["externalsecret_sync_calls_error"] == "cumulative_counter"  # counter without _total
    assert types["external_dns_registry_endpoints_total"] == "gauge"  # gauge with _total
    assert types["rest_client_request_duration_seconds_bucket"] == "cumulative_counter"
    assert types["go_gc_duration_seconds"] == "gauge"  # untyped
    assert ("workqueue_depth", {"name": "kustomization"}, 150.0, "gauge") in parse_prom(PROM)


def test_splunk_type_counter_family_named_directly():
    assert splunk_type("requests", {"requests": "counter"}) == "cumulative_counter"
    assert splunk_type("latency_sum", {"latency": "summary"}) == "cumulative_counter"
    assert splunk_type("latency", {"latency": "summary"}) == "gauge"  # quantile series


def test_matches_label_subset():
    sel = 'clustersecretstore_status_condition{status="False"}'
    assert matches(sel, "clustersecretstore_status_condition", {"name": "aws", "condition": "Ready", "status": "False"})
    assert not matches(sel, "clustersecretstore_status_condition", {"name": "aws", "status": "True"})
    assert not matches(sel, "other_metric", {"status": "False"})


def test_breach():
    assert breach("max", 2, 1, 0) and not breach("max", 1, 1, 0)
    assert breach("min", 0, 1, 0) and not breach("min", 1, 1, 0)
    assert breach("max_age", 100, 50, 200) and not breach("max_age", 180, 50, 200)


# --- metrics checks against a real HTTP endpoint ------------------------------------------------

@pytest.fixture
def prom_url():
    class Prom(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(PROM.encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Prom)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/metrics"
    srv.shutdown()


def fails(url, check, out=None):
    try:
        scrape({"metrics": {"url": url}, **check}, [] if out is None else out)
    except RuntimeError as e:
        return str(e)


def test_scrape_forwarding_and_thresholds(prom_url):
    out = []
    assert not fails(prom_url, {"forward": ['clustersecretstore_status_condition{status="False"}', "go_gc_duration_seconds"]}, out)
    assert [(n, d["status"]) for n, _, d, _ in out] == [("clustersecretstore_status_condition", "False")]  # NaN dropped
    out = []
    assert "kustomization" in fails(prom_url, {"forward": ["workqueue_depth"], "max": {"workqueue_depth": 100}}, out)
    assert len(out) == 2  # series still forwarded when the check fails
    assert not fails(prom_url, {"max": {'clustersecretstore_status_condition{status="False"}': 0}})
    assert not fails(prom_url, {"max": {"not_exported": 0}})  # max passes on absence
    assert "missing" in fails(prom_url, {"min": {"not_exported": 1}})
    assert "breaches min" in fails(prom_url, {"min": {"external_dns_registry_endpoints_total": 50}})
    assert "breaches max_age" in fails(prom_url, {"max_age": {"external_dns_controller_last_sync_timestamp_seconds": 600}})
    assert not fails(prom_url, {"max_age": {"external_dns_controller_last_sync_timestamp_seconds": 10**10}})


AGG_PROM = """# TYPE reqs_total counter
reqs_total{allowed="true",ns="a",kind="Pod"} 5
reqs_total{allowed="true",ns="b",kind="Pod"} 7
reqs_total{allowed="false",ns="a",kind="Pod"} 2
reqs_total{ns="c"} 1
reqs_total{allowed="true",ns="d"} NaN
# TYPE lat_seconds histogram
lat_seconds_bucket{le="0.1",ns="a"} 3
lat_seconds_bucket{le="0.1",ns="b"} 4
lat_seconds_bucket{le="+Inf",ns="a"} 9
"""


def test_scrape_aggregate_sums_by_kept_labels(monkeypatch):
    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(agent.urllib.request, "urlopen", lambda url, timeout: Resp(AGG_PROM.encode()))
    out = []
    scrape({"metrics": {"url": "http://x/metrics"},
            "aggregate": {"reqs_total": ["allowed"], 'lat_seconds_bucket{ns="a"}': ["le"], "lat_seconds_bucket": []}}, out)
    got = {(n, tuple(sorted(d.items())), t): v for n, v, d, t in out}
    assert got[("reqs_total", (("allowed", "true"),), "cumulative_counter")] == 12  # ns and kind dropped, NaN skipped
    assert got[("reqs_total", (("allowed", "false"),), "cumulative_counter")] == 2
    assert got[("reqs_total", (("allowed", ""),), "cumulative_counter")] == 1  # label missing on the series
    assert got[("lat_seconds_bucket", (("le", "0.1"),), "cumulative_counter")] == 3  # label-filtered selector: ns="a" only
    assert got[("lat_seconds_bucket", (("le", "+Inf"),), "cumulative_counter")] == 9
    assert got[("lat_seconds_bucket", (), "cumulative_counter")] == 16  # no labels kept: one total
    assert len(out) == 6


def test_metrics_targets_by_selector(monkeypatch):
    seen = []

    def api(path, **_):
        seen.append(path)
        return {"items": [{"metadata": {"name": "p1"}, "status": {"podIP": "10.0.0.1"}},
                          {"metadata": {"name": "p2"}, "status": {"podIP": "fd00::2"}},
                          {"metadata": {"name": "p3"}, "status": {}}]}  # no IP yet: skipped

    monkeypatch.setattr(agent, "k8s", api)
    targets = agent.metrics_targets({"namespace": "ns", "selector": "app=x", "port": 9090, "path": "/m"})
    assert targets == [("http://10.0.0.1:9090/m", {"pod": "p1"}), ("http://[fd00::2]:9090/m", {"pod": "p2"})]
    assert seen[0].startswith("/api/v1/namespaces/ns/pods?") and "labelSelector=app%3Dx" in seen[0] and "status.phase%3DRunning" in seen[0]

    monkeypatch.setattr(agent, "k8s", lambda path, **_: {"items": []})
    with pytest.raises(RuntimeError, match="no running pods"):
        agent.metrics_targets({"namespace": "ns", "selector": "app=x", "port": 9090})


# --- workload readiness, nodes, simple checks --------------------------------------------------

def test_ready():
    assert ready("deployment", {"spec": {"replicas": 2}, "status": {"readyReplicas": 2}})
    assert not ready("deployment", {"spec": {"replicas": 2}, "status": {"readyReplicas": 1}})
    assert not ready("deployment", {"spec": {"replicas": 0}, "status": {}})
    assert ready("daemonset", {"status": {"desiredNumberScheduled": 3, "numberReady": 3}})
    assert not ready("daemonset", {"status": {"desiredNumberScheduled": 3, "numberReady": 2}})


NOW = time.time()


def node(name, ready_status, age_s):
    created = datetime.fromtimestamp(NOW - age_s, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"metadata": {"name": name, "creationTimestamp": created}, "status": {"conditions": [{"type": "Ready", "status": ready_status}]}}


def test_not_ready_nodes_ignores_young_nodes():
    nodes = [node("a", "True", 3600), node("b", "False", 3600), node("c", "Unknown", 60),
             {"metadata": {"name": "d", "creationTimestamp": node("d", "x", 3600)["metadata"]["creationTimestamp"]}, "status": {}}]
    assert not_ready_nodes(nodes, NOW, 300) == ["b", "d"]  # d has no Ready condition at all


def test_run_check_dns_and_apiserver(monkeypatch):
    looked_up = []
    monkeypatch.setattr(agent.socket, "getaddrinfo", lambda name, port: looked_up.append(name))
    run_check({"dns": "kubernetes.default.svc.cluster.local."}, [])
    assert looked_up == ["kubernetes.default.svc.cluster.local."]

    calls = []
    monkeypatch.setattr(agent, "k8s", lambda path, raw=False, **_: calls.append((path, raw)) or "ok")
    run_check({"apiserver": "/readyz"}, [])
    assert calls == [("/readyz", True)]


def test_label_of_every_kind():
    assert label_of({"apiserver": "/readyz"}) == "apiserver/readyz"
    assert label_of({"nodes": {}}) == "nodes/ready"
    assert label_of({"metrics": {"url": "http://x/m"}}) == "metrics/http://x/m"
    assert label_of({"metrics": {"namespace": "n", "selector": "a=b"}}) == "metrics/n/a=b"
    assert label_of({"dns": "x."}) == "dns/x."
    assert label_of({"kind": "deployment", "namespace": "n", "name": "d"}) == "deployment/n/d"


def test_bracket():
    assert bracket("10.0.0.1") == "10.0.0.1" and bracket("fd00::1") == "[fd00::1]"


# --- collect(): universal checks and auto mode -------------------------------------------------

def test_collect_universal_checks_and_auto_mode(monkeypatch):
    objects = {"/readyz": "ok",
               "/api/v1/nodes": {"items": [node("n1", "True", 3600), node("n2", "False", 3600)]},
               "/apis/apps/v1/namespaces/kube-system/deployments/coredns": {"spec": {"replicas": 2}, "status": {"readyReplicas": 2}}}

    def cluster_api(path, method="GET", body=None, raw=False):
        if path not in objects:
            raise not_found(path)
        return objects[path]

    monkeypatch.setattr(agent, "k8s", cluster_api)
    services = {
        "kubernetes": {"mode": "always", "checks": [{"apiserver": "/readyz"}, {"nodes": {"max_not_ready": 0}}]},
        "coredns": {"mode": "auto", "checks": [{"kind": "deployment", "namespace": "kube-system", "name": "coredns"}]},
        "cilium": {"mode": "auto", "checks": [{"kind": "daemonset", "namespace": "kube-system", "name": "cilium"}]},
        "argocd": {"mode": "always", "checks": [{"kind": "deployment", "namespace": "argocd", "name": "argocd-server"}]},
    }
    absent = set()

    def up():
        return {(d["service"], d["check"]): v for name, v, d, _ in collect(services, "c1", absent) if name == agent.METRIC}

    assert up() == {("kubernetes", "apiserver/readyz"): 1, ("kubernetes", "nodes/ready"): 0,  # n2 NotReady > max 0
                    ("coredns", "deployment/kube-system/coredns"): 1,
                    ("argocd", "deployment/argocd/argocd-server"): 0}  # "always": missing workload is down
    assert absent == {"cilium"}  # "auto" and not installed: skipped entirely, no datapoints
    services["kubernetes"]["checks"][1]["nodes"]["max_not_ready"] = 1
    assert up()[("kubernetes", "nodes/ready")] == 1  # one NotReady node is within the allowance
    objects["/apis/apps/v1/namespaces/kube-system/daemonsets/cilium"] = {"status": {"desiredNumberScheduled": 2, "numberReady": 1}}
    assert up()[("cilium", "daemonset/kube-system/cilium")] == 0 and absent == set()  # installed later: reported, and down


def test_collect_forwards_extra_series_with_service_dims(monkeypatch):
    def fake_scrape(c, out):
        out.append(("x_total", 3.0, {"pod": "p", "service": "spoofed"}, "cumulative_counter"))

    monkeypatch.setattr(agent, "scrape", fake_scrape)
    points = collect({"svc": {"checks": [{"metrics": {"url": "http://x"}}]}}, "c1", set())
    extra = [p for p in points if p[0] == "x_total"]
    assert extra == [("x_total", 3.0, {"pod": "p", "service": "svc", "cluster": "c1"}, "cumulative_counter")]


# --- OTLP payload and sending ------------------------------------------------------------------

def test_otlp_payload():
    req = otlp([("k8s.health.up", 1, {"cluster": "c1", "service": "coredns", "check": "dns/x."}, "gauge"),
                ("k8s.health.up", 0, {"cluster": "c1", "service": "coredns", "check": "deployment/kube-system/coredns"}, "gauge"),
                ("coredns_dns_requests_total", 42, {"cluster": "c1", "pod": "coredns-1"}, "cumulative_counter")], 1_700_000_000 * 10**9)
    metrics = {m["name"]: m for m in req["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]}
    up = metrics["k8s.health.up"]["gauge"]["dataPoints"]
    assert [p["asDouble"] for p in up] == [1.0, 0.0] and up[0]["timeUnixNano"] == "1700000000000000000"
    assert {"key": "check", "value": {"stringValue": "dns/x."}} in up[0]["attributes"]
    counter = metrics["coredns_dns_requests_total"]["sum"]
    assert counter["isMonotonic"] and counter["aggregationTemporality"] == 2 and counter["dataPoints"][0]["asDouble"] == 42.0


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_send_ok_rejected_and_failed(monkeypatch, capsys):
    sent = []

    def ok(req, timeout):
        sent.append((req.full_url, json.loads(req.data)))
        return FakeResponse(b"")

    monkeypatch.setattr(agent.urllib.request, "urlopen", ok)
    assert send("http://collector:4318", [("m", 1, {}, "gauge")], 1.0)
    assert sent[0][0] == "http://collector:4318/v1/metrics" and sent[0][1]["resourceMetrics"]

    monkeypatch.setattr(agent.urllib.request, "urlopen",
                        lambda req, timeout: FakeResponse(b'{"partialSuccess": {"rejectedDataPoints": 2, "errorMessage": "bad"}}'))
    assert send("http://c", [("m", 1, {}, "gauge")], 1.0)  # accepted the request; some points rejected
    assert "collector rejected datapoints" in capsys.readouterr().out

    def down(req, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(agent.urllib.request, "urlopen", down)
    assert not send("http://c", [("m", 1, {}, "gauge")], 1.0)
    assert "ingest failed" in capsys.readouterr().out


# --- the Kubernetes API client -----------------------------------------------------------------

def test_k8s_client(monkeypatch, tmp_path):
    (tmp_path / "token").write_text("tok-1")
    monkeypatch.setattr(agent, "SA", str(tmp_path))
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "fd00::1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    monkeypatch.setattr(agent.ssl, "create_default_context", lambda cafile: ("ctx", cafile))
    seen = {}

    def urlopen(req, context, timeout):
        seen.update(url=req.full_url, method=req.get_method(), auth=req.get_header("Authorization"),
                    body=req.data, context=context)
        return FakeResponse(b'{"kind": "Lease"}' if seen["url"].endswith("/x") else b"ok")

    monkeypatch.setattr(agent.urllib.request, "urlopen", urlopen)
    assert agent.k8s("/x", "PUT", {"a": 1}) == {"kind": "Lease"}
    assert seen["url"] == "https://[fd00::1]:443/x" and seen["method"] == "PUT"  # IPv6 host bracketed
    assert seen["auth"] == "Bearer tok-1" and json.loads(seen["body"]) == {"a": 1}
    assert seen["context"] == ("ctx", f"{tmp_path}/ca.crt")
    (tmp_path / "token").write_text("tok-2")  # projected tokens rotate: re-read on every call
    assert agent.k8s("/readyz", raw=True) == "ok" and seen["auth"] == "Bearer tok-2" and seen["body"] is None


# --- leader election ---------------------------------------------------------------------------

class FakeLeaseAPI:
    """Enforces resourceVersion compare-and-swap like the real API server."""

    def __init__(self):
        self.lease, self.rv, self.before_put = None, 0, None

    def __call__(self, path, method="GET", body=None, raw=False):
        if method == "GET":
            if self.lease is None:
                raise not_found(path)
            return copy.deepcopy(self.lease)
        if self.before_put:
            hook, self.before_put = self.before_put, None
            hook()
        if (method == "POST" and self.lease) or (method == "PUT" and body["metadata"]["resourceVersion"] != self.lease["metadata"]["resourceVersion"]):
            raise urllib.error.HTTPError(path, 409, "conflict", {}, None)
        self.rv += 1
        self.lease = {**copy.deepcopy(body), "metadata": {**body["metadata"], "resourceVersion": str(self.rv)}}
        return copy.deepcopy(self.lease)


def test_leader_election_scenarios(monkeypatch):
    api = FakeLeaseAPI()
    monkeypatch.setattr(agent, "k8s", api)
    a, b, c = Elector("ns", "a"), Elector("ns", "b"), Elector("ns", "c")
    t = 1_000_000.0
    a.tick(t)  # no lease yet: a creates it
    assert a.is_leader(t) and api.lease["spec"]["holderIdentity"] == "a"
    b.tick(t + 1)
    assert not b.is_leader(t + 1)  # a's lease is fresh: b stands by
    a.tick(t + 10)
    assert a.is_leader(t + 15) and api.lease["spec"]["holderIdentity"] == "a"

    # a's node breaks: it stops renewing. It must stop acting before anyone else can take over.
    assert not a.is_leader(t + 10 + LEASE_SECONDS - 5)
    b.tick(t + 10 + LEASE_SECONDS - 5)
    assert not b.is_leader(t + 10 + LEASE_SECONDS - 5)  # lease not expired yet
    t2 = t + 10 + LEASE_SECONDS + 1
    b.tick(t2)  # expired: b takes over
    assert b.is_leader(t2) and api.lease["spec"]["holderIdentity"] == "b" and api.lease["spec"]["leaseTransitions"] == 1

    # Takeover race: c and a both see an expired lease; a writes first, so c's write conflicts.
    t3 = t2 + LEASE_SECONDS + 1
    api.before_put = lambda: a.tick(t3)
    c.tick(t3)
    assert a.is_leader(t3) and not c.is_leader(t3) and api.lease["spec"]["holderIdentity"] == "a"

    # Stepping down: lease freed, a holds off, b takes over at once.
    a.release(hold_off=2 * LEASE_SECONDS, now=t3)
    assert not a.is_leader(t3) and api.lease["spec"]["holderIdentity"] is None
    a.tick(t3 + 1)
    assert not a.is_leader(t3 + 1)  # holding off
    b.tick(t3 + 1)
    assert b.is_leader(t3 + 1) and api.lease["spec"]["holderIdentity"] == "b"
    b.release(now=t3 + 2)
    a.tick(t3 + 3)
    assert not a.is_leader(t3 + 3)
    c.tick(t3 + 3)
    assert c.is_leader(t3 + 3)
    c.release(now=t3 + 2 * LEASE_SECONDS + 2)
    a.tick(t3 + 2 * LEASE_SECONDS + 3)  # a's hold-off is over
    assert a.is_leader(t3 + 2 * LEASE_SECONDS + 3)


def test_lease_create_respects_hold_off(monkeypatch):
    api = FakeLeaseAPI()
    monkeypatch.setattr(agent, "k8s", api)
    e = Elector("ns", "a")
    e.hold_off_until = 2_000_000.0
    e.tick(1_000_000.0)  # no lease exists, but we're holding off: don't create one
    assert api.lease is None and not e.is_leader(1_000_000.0) and e.api_ok == 1_000_000.0


def test_tick_propagates_unexpected_api_errors(monkeypatch):
    def boom(path, method="GET", body=None, raw=False):
        raise urllib.error.HTTPError(path, 500, "server error", {}, None)

    monkeypatch.setattr(agent, "k8s", boom)
    with pytest.raises(urllib.error.HTTPError):
        Elector("ns", "a").tick(1.0)

    lease = {"metadata": {"resourceVersion": "1"}, "spec": {}}

    def put_fails(path, method="GET", body=None, raw=False):
        if method == "GET":
            return copy.deepcopy(lease)
        raise urllib.error.HTTPError(path, 500, "server error", {}, None)

    monkeypatch.setattr(agent, "k8s", put_fails)
    with pytest.raises(urllib.error.HTTPError):
        Elector("ns", "a").tick(1.0)


def test_release_edge_cases(monkeypatch, capsys):
    puts = []
    held_by_other = {"metadata": {"resourceVersion": "1"}, "spec": {"holderIdentity": "someone-else"}}
    monkeypatch.setattr(agent, "k8s", lambda path, method="GET", body=None, raw=False: puts.append(method) or copy.deepcopy(held_by_other))
    e = Elector("ns", "a")
    e.release(now=1.0)  # never led: nothing to do
    assert puts == []
    e.renewed = 1.0
    e.release(now=2.0)  # lease already moved on: read it, don't overwrite someone else's
    assert puts == ["GET"]

    def unreachable(path, method="GET", body=None, raw=False):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(agent, "k8s", unreachable)
    e.renewed = 1.0
    e.release(now=3.0)  # API down: log and let the lease expire on its own
    assert "lease release failed" in capsys.readouterr().out and not e.renewed


def test_lost_leadership_is_logged(monkeypatch, capsys):
    api = FakeLeaseAPI()
    monkeypatch.setattr(agent, "k8s", api)
    a, b = Elector("ns", "a"), Elector("ns", "b")
    a.tick(1_000_000.0)
    b.tick(1_000_000.0 + LEASE_SECONDS + 1)  # a stopped renewing; b takes over
    a.tick(1_000_000.0 + LEASE_SECONDS + 2)  # a notices
    assert '"lost leadership"' in capsys.readouterr().out and not a.is_leader(1_000_000.0 + LEASE_SECONDS + 2)


class Stop(Exception):
    pass


def test_elector_run_loop_survives_errors(monkeypatch, capsys):
    e = Elector("ns", "a")
    monkeypatch.setattr(e, "tick", lambda: (_ for _ in ()).throw(urllib.error.URLError("api down")))
    monkeypatch.setattr(agent.time, "sleep", lambda s: (_ for _ in ()).throw(Stop()))
    with pytest.raises(Stop):
        e.run()
    assert "leader election failed" in capsys.readouterr().out


def test_micro_round_trip():
    assert parse_micro(micro(1_700_000_000.25)) == pytest.approx(1_700_000_000.25)


# --- health endpoints --------------------------------------------------------------------------

class StubElector:
    def __init__(self, leader, api_ok, ticked):
        self.leader, self.api_ok, self.ticked = leader, api_ok, ticked

    def is_leader(self, now=None):
        return self.leader


def get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_health_server_endpoints():
    now = time.time()
    state = {"cycle_since": 0.0}
    elector = StubElector(True, now, now)
    server = agent.health_server(elector, state, 60, 0)
    port = server.server_address[1]
    try:
        assert get(port, "/readyz") == (200, {"ok": True, "role": "leader"})
        assert get(port, "/healthz") == (200, {"ok": True, "role": "leader"})
        assert get(port, "/other")[0] == 404
        elector.leader, elector.api_ok = False, now - 3600  # API unreachable: not ready, but still live
        assert get(port, "/readyz") == (503, {"ok": False, "role": "standby"})
        assert get(port, "/healthz")[0] == 200
        state["cycle_since"] = now - 3 * 60 - 1  # a check cycle hung for 3 intervals: restart me
        assert get(port, "/healthz")[0] == 503
        state["cycle_since"], elector.ticked = 0.0, now - 3600  # election loop stopped
        assert get(port, "/healthz")[0] == 503
    finally:
        server.shutdown()


# --- the main loop -----------------------------------------------------------------------------

class SpyElector:
    def __init__(self, leader=True):
        self.leader, self.released = leader, []

    def is_leader(self, now=None):
        return self.leader

    def release(self, hold_off=0.0, now=None):
        self.released.append(hold_off)


def test_step_runs_cycles_only_when_leading_and_due(monkeypatch):
    sends = []
    monkeypatch.setattr(agent, "collect", lambda services, cluster, absent: [("m", 1, {}, "gauge")])
    monkeypatch.setattr(agent, "send", lambda endpoint, points, start: sends.append(endpoint) or True)
    state = {"cycle_since": 0.0, "next_run": 0.0, "failures": 0}
    agent.step(SpyElector(leader=False), state, {}, "c1", "http://c", 60, set())
    assert sends == []  # standby: never collects
    agent.step(SpyElector(), state, {}, "c1", "http://c", 60, set())
    assert sends == ["http://c"] and state["next_run"] > time.time() and state["cycle_since"] == 0.0
    agent.step(SpyElector(), state, {}, "c1", "http://c", 60, set())
    assert sends == ["http://c"]  # not due again until the interval passes


def test_step_steps_down_after_repeated_send_failures(monkeypatch):
    monkeypatch.setattr(agent, "collect", lambda services, cluster, absent: [])
    monkeypatch.setattr(agent, "send", lambda endpoint, points, start: False)
    elector, state = SpyElector(), {"cycle_since": 0.0, "next_run": 0.0, "failures": 0}
    for _ in range(agent.SEND_FAILURES_TO_STEP_DOWN):
        state["next_run"] = 0.0
        agent.step(elector, state, {}, "c1", "http://c", 60, set())
    assert elector.released == [2 * LEASE_SECONDS] and state["failures"] == 0


@pytest.mark.parametrize("otlp_endpoint,expected", [("", "http://[fd00::9]:4318"), ("http://gw:4318", "http://gw:4318")])
def test_main_wires_everything_and_shuts_down_cleanly(monkeypatch, tmp_path, otlp_endpoint, expected):
    (tmp_path / "namespace").write_text("k8s-health\n")
    checks = tmp_path / "checks.json"
    checks.write_text(json.dumps({"services": {"kubernetes": {"mode": "always", "checks": []}}}))
    monkeypatch.setattr(agent, "SA", str(tmp_path))
    for k, v in {"CLUSTER_NAME": "c1", "INTERVAL_SECONDS": "30", "CHECKS_FILE": str(checks), "POD_NAME": "pod-1",
                 "SPLUNK_OTEL_AGENT": "fd00::9", "OTLP_ENDPOINT": otlp_endpoint, "HEALTH_PORT": "0"}.items():
        monkeypatch.setenv(k, v)
    handlers, started, steps, released = {}, [], [], []
    monkeypatch.setattr(agent.signal, "signal", lambda sig, fn: handlers.update({sig: fn}))
    monkeypatch.setattr(agent, "health_server", lambda elector, state, interval, port: started.append(("health", interval, port)))
    monkeypatch.setattr(agent.threading, "Thread", lambda target, daemon: type("T", (), {"start": lambda self: started.append("election")})())
    monkeypatch.setattr(agent.Elector, "release", lambda self, hold_off=0.0, now=None: released.append(self.identity))

    def one_step(elector, state, services, cluster, endpoint, interval, absent):
        steps.append((elector.identity, cluster, endpoint, interval, sorted(services)))
        raise Stop()

    monkeypatch.setattr(agent, "step", one_step)
    with pytest.raises(Stop):
        agent.main()
    assert steps == [("pod-1", "c1", expected, 30, ["kubernetes"])]
    assert started == [("health", 30, 0), "election"]
    with pytest.raises(SystemExit):  # SIGTERM: hand the lease over, then exit
        handlers[agent.signal.SIGTERM]()
    assert released == ["pod-1"]
