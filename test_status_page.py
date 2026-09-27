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
       {"id": "g", "dimensions": {**dims("traefik", "deploy"), "host.name": "node-2"}}]
data = {"a": [[now - 5000, 1]], "b": [[now - 70000, 1], [now - 5000, 0]],
        "c": [[now - STALE_AFTER * 1000 - 1, 1]], "e": [[now - 5000, 1], [now - 1000, None]],
        "f": [[now - STALE_AFTER * 5000, 0]], "g": [[now - 5000, 1]]}
s = summarize(mts, data, now)["c1"]
assert s["coredns"]["state"] == "down", s["coredns"]
assert s["cilium"]["state"] == "stale"
assert s["argocd"]["state"] == "stale"  # never reported
assert s["fluxcd"]["state"] == "up"
assert s["traefik"]["state"] == "up" and len(s["traefik"]["checks"]) == 1  # old node's MTS merged away

# history: hourly 0/1 means -> per-day worst series; missing days are None
today = now // DAY * DAY
hourly = {"a": [[today - DAY, 1], [today - DAY + 3_600_000, 0.5], [today, 1]],  # yesterday mean .75
          "b": [[today - DAY, 1], [today, 0]]}                                  # today mean 0
h = history(mts, hourly, now, days=3)[("c1", "coredns")]
assert h == [None, 0.75, 0.0], h

# page renders (Flask test client, Splunk stubbed)
for svc in s.values():
    svc.update(history=[None, 0.995, 1.0], uptime=0.9975)
status_page.status = lambda: {"generated_ms": now, "overall": "down", "headline": "1 service down",
                              "days": [today - 2 * DAY, today - DAY, today], "clusters": {"c1": s},
                              "incidents": [{"detector": "EKS core services", "rule": "EKS service down", "severity": "Critical",
                                             "active": True, "started_ms": now - 60000, "updated_ms": now, "url": "#"}]}
client = status_page.app.test_client()
page = client.get("/").get_data(as_text=True)
for text in ("1 service down", "argocd", "Operation in last", "99.75%", "bar partial", "bar nodata",
             "EKS service down", "ongoing since", "<code>deploy</code>"):
    assert text in page, text
assert client.get("/api/status").json["headline"] == "1 service down"
assert client.get("/healthz").get_data(as_text=True) == "ok"
print("ok")
