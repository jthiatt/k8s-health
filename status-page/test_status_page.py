"""Unit tests for status_page.py. Run: pytest status-page (CI also enforces >= 90% line coverage)."""
import io
import os
import time
import urllib.error

import pytest

os.environ["SPLUNK_API_TOKEN"] = "abc123\n"  # as written by `echo token > file; kubectl create secret --from-file`
import status_page  # noqa: E402
from status_page import DAY, STALE_AFTER, history, summarize  # noqa: E402

NOW = 10**12
TODAY = NOW // DAY * DAY


def dims(svc, chk, cluster="c1"):
    return {"cluster": cluster, "service": svc, "check": chk}


MTS = [{"id": "a", "dimensions": dims("coredns", "dns")},
       {"id": "b", "dimensions": dims("coredns", "deploy")},
       {"id": "c", "dimensions": dims("cilium", "ds")},
       {"id": "d", "dimensions": dims("argocd", "sts")},
       {"id": "e", "dimensions": dims("fluxcd", "deploy")},
       # same check from two nodes (agent pod rescheduled; collector adds host dims)
       {"id": "f", "dimensions": {**dims("traefik", "deploy"), "host.name": "node-1"}},
       {"id": "g", "dimensions": {**dims("traefik", "deploy"), "host.name": "node-2"}},
       # a whole cluster whose agent has gone silent
       {"id": "z", "dimensions": dims("coredns", "dns", "c2")}]
DATA = {"a": [[NOW - 5000, 1]], "b": [[NOW - 70000, 1], [NOW - 5000, 0]],
        "c": [[NOW - STALE_AFTER * 1000 - 1, 1]], "e": [[NOW - 5000, 1], [NOW - 1000, None]],
        "f": [[NOW - STALE_AFTER * 5000, 0]], "g": [[NOW - 5000, 1]]}


def test_token_whitespace_is_stripped():
    assert status_page.TOKEN == "abc123"


def test_summarize_states():
    summary = summarize(MTS, DATA, NOW)
    s = summary["c1"]
    assert s["coredns"]["state"] == "down"
    assert s["cilium"]["state"] == "stale"
    assert "argocd" not in s  # no data in the window while c1 reports: removed from checks.json, dropped
    assert summary["c2"]["coredns"]["state"] == "stale"  # whole cluster silent: kept, shown as stale
    assert s["fluxcd"]["state"] == "up"
    assert s["traefik"]["state"] == "up" and len(s["traefik"]["checks"]) == 1  # old node's MTS merged away


def test_history_worst_series_per_day():
    hourly = {"a": [[TODAY - DAY, 1], [TODAY - DAY + 3_600_000, 0.5], [TODAY, 1]],  # yesterday mean .75
              "b": [[TODAY - DAY, 1], [TODAY, 0], [TODAY + 1, None]]}               # today mean 0
    assert history(MTS, hourly, NOW, days=3)[("c1", "coredns")] == [None, 0.75, 0.0]


def incident(name, label, active, events, **kw):
    return {"detectorName": name, "detectLabel": label, "severity": "Critical", "active": active,
            "detectorId": "D1", "events": events, **kw}


def test_incidents_times_filter_and_links(monkeypatch):
    monkeypatch.setattr(status_page, "DETECTOR_MATCH", "k8s-health")
    monkeypatch.setattr(status_page, "api", lambda path, **_: [
        incident("k8s-health core services", "Service down", False,
                 [{"anomalyState": "OK", "timestamp": NOW - 60_000}, {"anomalyState": "ANOMALOUS", "timestamp": NOW - 180_000}],
                 anomalyStateUpdateTimestamp=NOW - 180_000),  # lags: must not be used as the resolve time
        incident("k8s-health core services", "old", False, [{"timestamp": NOW - 30 * DAY}]),  # resolved too long ago
        incident("k8s-health agent deadman", "Agent not reporting", True, [], anomalyStateUpdateTimestamp=NOW - 5000),
        incident("book-tracker SLO", "x", True, [{"timestamp": NOW}])])  # another detector: filtered out
    active, resolved = status_page.incidents(NOW)
    assert active["active"] and active["started_ms"] == NOW - 5000  # no events: falls back to the state timestamp
    assert resolved["started_ms"] == NOW - 180_000 and resolved["updated_ms"] == NOW - 60_000 and not resolved["active"]
    assert resolved["url"] == "https://app.us1.observability.splunkcloud.com/#/detector/v2/D1/edit?detectorSignalFlowEditor=1"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_api_client(monkeypatch):
    seen = {}

    def ok(req, timeout):
        seen.update(url=req.full_url, token=req.get_header("X-sf-token"))
        return FakeResponse(b'{"results": []}')

    monkeypatch.setattr(status_page.urllib.request, "urlopen", ok)
    assert status_page.api("/v2/metrictimeseries", query='sf_metric:"x"') == {"results": []}
    assert seen["url"].startswith("https://api.us1.signalfx.com/v2/metrictimeseries?query=") and seen["token"] == "abc123"

    def error(code):
        def urlopen(req, timeout):
            raise urllib.error.HTTPError(req.full_url, code, "err", {}, None)
        return urlopen

    monkeypatch.setattr(status_page.urllib.request, "urlopen", error(404))
    assert status_page.api("/v1/timeserieswindow") == {}  # no matching series yet
    monkeypatch.setattr(status_page.urllib.request, "urlopen", error(401))
    with pytest.raises(urllib.error.HTTPError):
        status_page.api("/v2/incident")


def test_cached_respects_ttl(monkeypatch):
    status_page._cache.clear()
    calls = []
    assert status_page.cached("k", 30, lambda: calls.append(1) or "v1") == "v1"
    assert status_page.cached("k", 30, lambda: calls.append(1) or "v2") == "v1"  # still fresh
    real_time = time.time
    monkeypatch.setattr(status_page.time, "time", lambda: real_time() + 31)
    assert status_page.cached("k", 30, lambda: calls.append(1) or "v3") == "v3" and len(calls) == 2
    status_page._cache.clear()


def fake_splunk(now_ms, values, incidents=()):
    """api() stand-in: one cluster with one service per (name, value, age_s)."""
    mts = [{"id": name, "dimensions": dims(name, "check")} for name, _, _ in values]
    live = {name: [[now_ms - age * 1000, v]] for name, v, age in values}
    hourly = {name: [[now_ms // DAY * DAY, v]] for name, v, _ in values}

    def api(path, **params):
        if path == "/v2/metrictimeseries":
            return {"results": mts}
        if path == "/v2/incident":
            return list(incidents)
        return {"data": hourly if params.get("resolution") == status_page.HOUR else live}
    return api


@pytest.mark.parametrize("values,incidents,overall,headline", [
    ([], [], "stale", "No data from any cluster agent"),
    ([("svc1", 0, 5), ("svc2", 0, 5)], [], "down", "2 services down"),
    ([("svc1", 0, 5), ("svc2", 1, 5)], [], "down", "1 service down"),
    ([("svc1", 1, 5), ("svc2", 1, 10_000)], [], "stale", "Some checks are not reporting"),
    ([("svc1", 1, 5)], [incident("k8s-health x", "r", True, [])], "alert", "Systems operational, 1 active alert"),
    ([("svc1", 1, 5)], [incident("k8s-health x", "r", True, [])] * 2, "alert", "Systems operational, 2 active alerts"),
    ([("svc1", 1, 5)], [], "up", "All systems operational"),
])
def test_status_overall(monkeypatch, values, incidents, overall, headline):
    status_page._cache.clear()
    now_ms = int(time.time() * 1000)
    monkeypatch.setattr(status_page, "api", fake_splunk(now_ms, values, incidents))
    monkeypatch.setattr(status_page, "DETECTOR_MATCH", "k8s-health")
    s = status_page.status()
    assert (s["overall"], s["headline"]) == (overall, headline)
    if values:
        svc = s["clusters"]["c1"]["svc1"]
        assert len(svc["history"]) == status_page.HISTORY_DAYS and svc["uptime"] is not None
    status_page._cache.clear()


def test_status_service_without_history(monkeypatch):
    status_page._cache.clear()
    now_ms = int(time.time() * 1000)
    api = fake_splunk(now_ms, [("svc1", 1, 5)])
    monkeypatch.setattr(status_page, "api", lambda path, **p: {"data": {}} if p.get("resolution") == status_page.HOUR else api(path, **p))
    svc = status_page.status()["clusters"]["c1"]["svc1"]
    assert svc["history"] == [None] * status_page.HISTORY_DAYS and svc["uptime"] is None
    status_page._cache.clear()


def test_template_filters():
    assert status_page.date(0) == "Jan 1, 1970"
    assert status_page.iso(0) == "1970-01-01T00:00:00+00:00"
    assert status_page.datetime_utc(0) == "January 1, 1970 at 00:00 UTC"
    now_ms = time.time() * 1000
    assert status_page.ago(0) == "never"
    assert status_page.ago(now_ms - 30_000) == "30s ago"
    assert status_page.ago(now_ms - 600_000) == "10m ago"
    assert status_page.ago(now_ms - 3 * 3_600_000) == "3h ago"
    assert status_page.ago(now_ms - 3 * 86_400_000) == "3d ago"
    assert status_page.pct(None) == "no data" and status_page.pct(1.0) == "100%"
    assert status_page.pct(0.99995) == "99.99%" and status_page.pct(0.9975) == "99.75%"  # rounds down
    assert [status_page.bar(u) for u in (None, 1.0, 0.995, 0.5)] == ["nodata", "up", "partial", "down"]


def page_status():
    s = summarize(MTS, DATA, NOW)["c1"]
    for svc in s.values():
        svc.update(history=[None, 0.995, 1.0], uptime=0.9975)
    return {"generated_ms": NOW, "overall": "down", "headline": "1 service down",
            "days": [TODAY - 2 * DAY, TODAY - DAY, TODAY], "clusters": {"c1": s},
            "incidents": [{"detector": "k8s-health core services", "rule": "Service down", "severity": "Critical",
                           "active": True, "started_ms": NOW - 60000, "updated_ms": NOW, "url": "#"},
                          {"detector": "k8s-health core services", "rule": "Service down", "severity": "Critical",
                           "active": False, "started_ms": NOW - 600000, "updated_ms": NOW - 300000, "url": "#"}]}


def test_routes(monkeypatch):
    monkeypatch.setattr(status_page, "status", page_status)
    client = status_page.app.test_client()
    page = client.get("/").get_data(as_text=True)
    for text in ("1 service down", "cilium", "Operation in last", "99.75%", "bar partial", "bar nodata",
                 "Service down", "ongoing since", "resolved", "<title>Cluster status</title>", "<code>deploy</code>"):
        assert text in page, text
    assert client.get("/api/status").json["headline"] == "1 service down"
    assert client.get("/healthz").get_data(as_text=True) == "ok"


def test_empty_page_explains_what_to_check(monkeypatch):
    monkeypatch.setattr(status_page, "status", lambda: {**page_status(), "clusters": {}, "incidents": []})
    page = status_page.app.test_client().get("/").get_data(as_text=True)
    assert "No agent has reported yet" in page and "k8s.health.up" in page and "No incidents in the last" in page


@pytest.mark.parametrize("error", [urllib.error.URLError("unreachable"), TimeoutError("timed out")])
def test_splunk_errors_return_502(monkeypatch, error):
    def broken():
        raise error

    monkeypatch.setattr(status_page, "status", broken)
    r = status_page.app.test_client().get("/api/status")
    assert r.status_code == 502 and "Splunk API error" in r.get_data(as_text=True)
