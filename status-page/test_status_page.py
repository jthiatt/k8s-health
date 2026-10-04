import status_page
from status_page import DAY, STALE_AFTER, history, summarize

now = 10**12
dims = lambda svc, chk: {"cluster": "c1", "service": svc, "check": chk}  # noqa: E731
mts = [{"id": "a", "dimensions": dims("coredns", "dns")},
       {"id": "b", "dimensions": dims("coredns", "deploy")},
       {"id": "c", "dimensions": dims("cilium", "ds")},
       {"id": "d", "dimensions": dims("argocd", "sts")},
       {"id": "e", "dimensions": dims("fluxcd", "deploy")},
       # same check from two nodes (agent pod rescheduled; collector adds host dims)
       {"id": "f", "dimensions": {**dims("traefik", "deploy"), "host.name": "node-1"}},
       {"id": "g", "dimensions": {**dims("traefik", "deploy"), "host.name": "node-2"}},
       # a whole cluster whose agent has gone silent
       {"id": "z", "dimensions": {"cluster": "c2", "service": "coredns", "check": "dns"}}]
data = {"a": [[now - 5000, 1]], "b": [[now - 70000, 1], [now - 5000, 0]],
        "c": [[now - STALE_AFTER * 1000 - 1, 1]], "e": [[now - 5000, 1], [now - 1000, None]],
        "f": [[now - STALE_AFTER * 5000, 0]], "g": [[now - 5000, 1]]}
summary = summarize(mts, data, now)
s = summary["c1"]
assert s["coredns"]["state"] == "down", s["coredns"]
assert s["cilium"]["state"] == "stale"
assert "argocd" not in s  # no data in the window while c1 reports: removed from checks.json, dropped
assert summary["c2"]["coredns"]["state"] == "stale"  # whole cluster silent: kept, shown as stale
assert s["fluxcd"]["state"] == "up"
assert s["traefik"]["state"] == "up" and len(s["traefik"]["checks"]) == 1  # old node's MTS merged away

# history: hourly 0/1 means -> per-day worst series; missing days are None
today = now // DAY * DAY
hourly = {"a": [[today - DAY, 1], [today - DAY + 3_600_000, 0.5], [today, 1]],  # yesterday mean .75
          "b": [[today - DAY, 1], [today, 0]]}                                  # today mean 0
h = history(mts, hourly, now, days=3)[("c1", "coredns")]
assert h == [None, 0.75, 0.0], h

# incidents: start = first event, resolved = last event (anomalyStateUpdateTimestamp can lag, as seen in Splunk)
status_page.api = lambda path, **_: [
    {"detectorName": "k8s-health core services", "detectLabel": "Service down", "severity": "Critical", "active": False,
     "detectorId": "D1", "anomalyState": "OK", "anomalyStateUpdateTimestamp": now - 180_000,
     "events": [{"anomalyState": "OK", "timestamp": now - 60_000}, {"anomalyState": "ANOMALOUS", "timestamp": now - 180_000}]},
    {"detectorName": "book-tracker SLO", "detectLabel": "x", "active": True, "events": [{"timestamp": now}]}]
status_page.DETECTOR_MATCH = "k8s-health"
(inc,) = status_page.incidents(now)
assert inc["started_ms"] == now - 180_000 and inc["updated_ms"] == now - 60_000 and not inc["active"], inc
assert inc["url"] == "https://app.us1.observability.splunkcloud.com/#/detector/v2/D1/edit?detectorSignalFlowEditor=1", inc["url"]

# page renders (Flask test client, Splunk stubbed)
for svc in s.values():
    svc.update(history=[None, 0.995, 1.0], uptime=0.9975)
status_page.status = lambda: {"generated_ms": now, "overall": "down", "headline": "1 service down",
                              "days": [today - 2 * DAY, today - DAY, today], "clusters": {"c1": s},
                              "incidents": [{"detector": "k8s-health core services", "rule": "Service down", "severity": "Critical",
                                             "active": True, "started_ms": now - 60000, "updated_ms": now, "url": "#"}]}
client = status_page.app.test_client()
page = client.get("/").get_data(as_text=True)
for text in ("1 service down", "cilium", "Operation in last", "99.75%", "bar partial", "bar nodata",
             "Service down", "ongoing since", "<title>Cluster status</title>", "<code>deploy</code>"):
    assert text in page, text
assert client.get("/api/status").json["headline"] == "1 service down"
assert client.get("/healthz").get_data(as_text=True) == "ok"
print("ok")
