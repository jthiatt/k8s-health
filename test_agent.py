import copy
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

import agent
from agent import LEASE_SECONDS, Elector, matches, otlp, parse_prom, ready, scrape

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
types = {n: t for n, _, _, t in parse_prom(PROM)}
assert types["controller_runtime_reconcile_errors_total"] == "cumulative_counter"
assert types["externalsecret_sync_calls_error"] == "cumulative_counter"  # counter without _total
assert types["external_dns_registry_endpoints_total"] == "gauge"  # gauge with _total
assert types["rest_client_request_duration_seconds_bucket"] == "cumulative_counter"
assert types["go_gc_duration_seconds"] == "gauge"  # untyped
assert ("workqueue_depth", {"name": "kustomization"}, 150.0, "gauge") in parse_prom(PROM)
assert matches('clustersecretstore_status_condition{status="False"}', "clustersecretstore_status_condition",
               {"name": "aws", "condition": "Ready", "status": "False"})
assert not matches('clustersecretstore_status_condition{status="False"}', "clustersecretstore_status_condition",
                   {"name": "aws", "status": "True"})


class Prom(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(PROM.encode())

    def log_message(self, *a):
        pass


def fails(check, out=None):
    try:
        scrape({"metrics": {"url": url}, **check}, [] if out is None else out)
    except RuntimeError as e:
        return str(e)


srv = HTTPServer(("127.0.0.1", 0), Prom)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_port}/metrics"
out = []
assert not fails({"forward": ['clustersecretstore_status_condition{status="False"}', "go_gc_duration_seconds"]}, out)
assert [(n, d["status"]) for n, _, d, _ in out] == [("clustersecretstore_status_condition", "False")]  # NaN dropped
out = []
assert "kustomization" in fails({"forward": ["workqueue_depth"], "max": {"workqueue_depth": 100}}, out)
assert len(out) == 2  # series still forwarded when the check fails
assert not fails({"max": {'clustersecretstore_status_condition{status="False"}': 0}})
assert not fails({"max": {"not_exported": 0}})  # max passes on absence
assert "missing" in fails({"min": {"not_exported": 1}})
assert "breaches min" in fails({"min": {"external_dns_registry_endpoints_total": 50}})
assert "breaches max_age" in fails({"max_age": {"external_dns_controller_last_sync_timestamp_seconds": 600}})
assert not fails({"max_age": {"external_dns_controller_last_sync_timestamp_seconds": 10**10}})
srv.shutdown()

assert ready("deployment", {"spec": {"replicas": 2}, "status": {"readyReplicas": 2}})
assert not ready("deployment", {"spec": {"replicas": 2}, "status": {"readyReplicas": 1}})
assert not ready("deployment", {"spec": {"replicas": 0}, "status": {}})
assert ready("daemonset", {"status": {"desiredNumberScheduled": 3, "numberReady": 3}})
assert not ready("daemonset", {"status": {"desiredNumberScheduled": 3, "numberReady": 2}})
# OTLP payload: one metric per name; counters as monotonic cumulative sums, the rest as gauges
req = otlp([("eks.health.up", 1, {"cluster": "c1", "service": "coredns", "check": "dns/x."}, "gauge"),
            ("eks.health.up", 0, {"cluster": "c1", "service": "coredns", "check": "deployment/kube-system/coredns"}, "gauge"),
            ("coredns_dns_requests_total", 42, {"cluster": "c1", "pod": "coredns-1"}, "cumulative_counter")], 1_700_000_000 * 10**9)
metrics = {m["name"]: m for m in req["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]}
up = metrics["eks.health.up"]["gauge"]["dataPoints"]
assert [p["asDouble"] for p in up] == [1.0, 0.0] and up[0]["timeUnixNano"] == "1700000000000000000"
assert {"key": "check", "value": {"stringValue": "dns/x."}} in up[0]["attributes"]
counter = metrics["coredns_dns_requests_total"]["sum"]
assert counter["isMonotonic"] and counter["aggregationTemporality"] == 2 and counter["dataPoints"][0]["asDouble"] == 42.0
# Leader election against a fake API server that enforces resourceVersion compare-and-swap like the real one
class FakeAPI:
    def __init__(self):
        self.lease, self.rv, self.before_put = None, 0, None

    def __call__(self, path, method="GET", body=None):
        if method == "GET":
            if self.lease is None:
                raise urllib.error.HTTPError(path, 404, "not found", {}, None)
            return copy.deepcopy(self.lease)
        if self.before_put:
            hook, self.before_put = self.before_put, None
            hook()
        if (method == "POST" and self.lease) or (method == "PUT" and body["metadata"]["resourceVersion"] != self.lease["metadata"]["resourceVersion"]):
            raise urllib.error.HTTPError(path, 409, "conflict", {}, None)
        self.rv += 1
        self.lease = {**copy.deepcopy(body), "metadata": {**body["metadata"], "resourceVersion": str(self.rv)}}
        return copy.deepcopy(self.lease)


api = agent.k8s = FakeAPI()
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

# Takeover race: c and a both see an expired lease; a writes first, so c's write conflicts and c doesn't lead.
t3 = t2 + LEASE_SECONDS + 1
api.before_put = lambda: a.tick(t3)
c.tick(t3)
assert a.is_leader(t3) and not c.is_leader(t3) and api.lease["spec"]["holderIdentity"] == "a"

# Stepping down (e.g. can't reach this node's collector): lease freed, a holds off, b takes over at once.
a.release(hold_off=2 * LEASE_SECONDS, now=t3)
assert not a.is_leader(t3) and api.lease["spec"]["holderIdentity"] is None
a.tick(t3 + 1)
assert not a.is_leader(t3 + 1)  # holding off
b.tick(t3 + 1)
assert b.is_leader(t3 + 1) and api.lease["spec"]["holderIdentity"] == "b"
b.release(now=t3 + 2)  # b shuts down; a's hold-off has not expired, c's never started
a.tick(t3 + 3)
assert not a.is_leader(t3 + 3)
c.tick(t3 + 3)
assert c.is_leader(t3 + 3)
c.release(now=t3 + 2 * LEASE_SECONDS + 2)
a.tick(t3 + 2 * LEASE_SECONDS + 3)  # a's hold-off is over
assert a.is_leader(t3 + 2 * LEASE_SECONDS + 3)
print("ok")
