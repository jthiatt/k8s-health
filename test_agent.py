import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from agent import matches, parse_prom, ready, scrape

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
print("ok")
