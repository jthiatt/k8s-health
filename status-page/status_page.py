"""k8s-health status page (Flask): reads the agents' `k8s.health.up` and detector incidents from the Splunk API.
A check with no datapoint within STALE_AFTER_SECONDS is stale (the agent missed its interval)."""
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import json
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template

REALM = os.environ.get("SPLUNK_REALM", "us1")
API = f"https://api.{REALM}.signalfx.com"
APP = f"https://app.{REALM}.observability.splunkcloud.com"
# strip(): a token written to a Secret with `echo`/a file usually ends in "\n", which is an invalid header value
TOKEN = os.environ.get("SPLUNK_API_TOKEN", "").strip()
INTERVAL = int(os.environ.get("INTERVAL_SECONDS", "60"))
STALE_AFTER = int(os.environ.get("STALE_AFTER_SECONDS", str(3 * INTERVAL)))
DETECTOR_MATCH = os.environ.get("DETECTOR_MATCH", "k8s-health").lower()  # substring of detector names; "" = all
PAGE_TITLE = os.environ.get("PAGE_TITLE", "Cluster status")
METRIC = os.environ.get("METRIC_NAME", "k8s.health.up")  # must match the agents
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "30"))
INCIDENT_DAYS = int(os.environ.get("INCIDENT_DAYS", "7"))
CACHE_SECONDS, HISTORY_CACHE_SECONDS = 30, 600
QUERY = f'sf_metric:"{METRIC}"'
RANK = {"down": 0, "stale": 1, "up": 2}
HOUR, DAY = 3_600_000, 86_400_000

app = Flask(__name__)


def api(path, **params):
    req = urllib.request.Request(f"{API}{path}?{urllib.parse.urlencode(params)}", headers={"X-SF-Token": TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:  # timeserieswindow 404s when no series match
            return {}
        raise


def check_key(d):
    return d.get("cluster", "?"), d.get("service", "?"), d.get("check", "?")


def summarize(mts, data, now_ms):
    """-> {cluster: {service: {"state", "checks": [{"check", "state", "last_ms"}]}}}
    The OTel collector adds host dims, so one check becomes a new MTS each time the agent pod changes
    node; merge MTS by (cluster, service, check) and keep the newest point.
    A check with no data in the whole window, while its cluster's agent is still reporting other checks,
    was removed from checks.json: drop it. If the whole cluster is silent, everything shows as stale."""
    latest = {}
    for m in mts:
        key = check_key(m["dimensions"])
        points = [p for p in data.get(m["id"], []) if p[1] is not None]
        latest[key] = max([latest.get(key, (0, None)), *points], key=lambda p: p[0])
    live = {cluster for (cluster, _, _), (last_ms, _) in latest.items() if now_ms - last_ms <= STALE_AFTER * 1000}
    clusters = {}
    for (cluster, service, check), (last_ms, value) in latest.items():
        if not last_ms and cluster in live:
            continue
        state = "stale" if now_ms - last_ms > STALE_AFTER * 1000 else "up" if value >= 1 else "down"
        svc = clusters.setdefault(cluster, {}).setdefault(service, {"checks": []})
        svc["checks"].append({"check": check, "state": state, "last_ms": last_ms})
    for services in clusters.values():
        for svc in services.values():
            svc["state"] = min((c["state"] for c in svc["checks"]), key=RANK.get)
    return clusters


def day_starts(now_ms, days=HISTORY_DAYS):
    today = now_ms // DAY * DAY
    return [today - i * DAY for i in reversed(range(days))]


def history(mts, hourly, now_ms, days=HISTORY_DAYS):
    """Hourly means of the 0/1 gauge -> {(cluster, service): [fraction up, or None, per UTC day, oldest first]}.
    A service's day is its worst series' mean that day. Hours with no data don't count as down."""
    per = {}
    for m in mts:
        cluster, service, _ = check_key(m["dimensions"])
        by_day = {}
        for ts, v in hourly.get(m["id"], []):
            if v is not None:
                by_day.setdefault(ts // DAY * DAY, []).append(v)
        worst = per.setdefault((cluster, service), {})
        for day, vs in by_day.items():
            worst[day] = min(worst.get(day, 1.0), sum(vs) / len(vs))
    return {k: [worst.get(d) for d in day_starts(now_ms, days)] for k, worst in per.items()}


def detector_url(detector_id):
    # Verified in the Splunk UI. Not the `url` the Terraform provider reports (#/detector/<id>), which is "not found".
    return f"{APP}/#/detector/v2/{detector_id}/edit?detectorSignalFlowEditor=1"


def incidents(now_ms):
    out = []
    for i in api("/v2/incident", includeResolved="true", limit=100) or []:
        name = i.get("detectorName", "")
        # anomalyStateUpdateTimestamp isn't reliably the resolve time (it can stay at the ANOMALOUS time),
        # so take start/last-change from the incident's events.
        times = [e["timestamp"] for e in i.get("events") or [] if e.get("timestamp")] or [i.get("anomalyStateUpdateTimestamp", 0)]
        started = min(times)
        if DETECTOR_MATCH not in name.lower() or (not i.get("active") and now_ms - started > INCIDENT_DAYS * DAY):
            continue
        out.append({"detector": name, "rule": i.get("detectLabel", ""), "severity": i.get("severity", ""),
                    "active": bool(i.get("active")), "started_ms": started,
                    "updated_ms": max(times),
                    "url": detector_url(i.get("detectorId"))})
    return sorted(out, key=lambda x: (not x["active"], -x["started_ms"]))


_cache = {}


def cached(key, ttl, fn):
    # ponytail: per-process cache; run one gunicorn worker (threads are fine) so Splunk is polled once.
    hit = _cache.get(key)
    if not hit or time.time() - hit[0] > ttl:
        hit = _cache[key] = (time.time(), fn())
    return hit[1]


def status():
    now = int(time.time() * 1000)
    mts = cached("mts", CACHE_SECONDS, lambda: api("/v2/metrictimeseries", query=QUERY, limit=10000).get("results", []))
    live = cached("live", CACHE_SECONDS, lambda: api(
        "/v1/timeserieswindow", query=QUERY, startMs=now - 5 * STALE_AFTER * 1000, endMs=now, resolution=1000).get("data", {}))
    hourly = cached("hourly", HISTORY_CACHE_SECONDS, lambda: api(  # Splunk rejects 1-day resolution; 1h it is
        "/v1/timeserieswindow", query=QUERY, startMs=day_starts(now)[0], endMs=now, resolution=HOUR).get("data", {}))
    incs = cached("incidents", CACHE_SECONDS, lambda: incidents(now))

    clusters, hist = summarize(mts, live, now), history(mts, hourly, now)
    for cluster, services in clusters.items():
        for service, svc in services.items():
            svc["history"] = hist.get((cluster, service), [None] * HISTORY_DAYS)
            known = [u for u in svc["history"] if u is not None]
            svc["uptime"] = sum(known) / len(known) if known else None

    states = [svc["state"] for services in clusters.values() for svc in services.values()]
    active = sum(i["active"] for i in incs)
    if not clusters:
        overall, headline = "stale", "No data from any cluster agent"
    elif "down" in states:
        overall, headline = "down", f"{states.count('down')} service{'s' * (states.count('down') > 1)} down"
    elif "stale" in states:
        overall, headline = "stale", "Some checks are not reporting"
    elif active:
        overall, headline = "alert", f"Systems operational, {active} active alert{'s' * (active > 1)}"
    else:
        overall, headline = "up", "All systems operational"
    return {"generated_ms": now, "overall": overall, "headline": headline, "days": day_starts(now),
            "clusters": clusters, "incidents": incs}


@app.template_filter()
def date(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%b %-d, %Y")


@app.template_filter()
def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


@app.template_filter()
def datetime_utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%B %-d, %Y at %H:%M UTC")


@app.template_filter()
def ago(ms):
    if not ms:
        return "never"
    s = int(time.time() - ms / 1000)
    return f"{s}s ago" if s < 120 else f"{s // 60}m ago" if s < 7200 else f"{s // 3600}h ago" if s < 172800 else f"{s // 86400}d ago"


@app.template_filter()
def pct(u):
    if u is None:
        return "no data"
    floored = math.floor(u * 10000) / 100  # round down: 99.995% must not show as 100%
    return f"{floored:.2f}".rstrip("0").rstrip(".") + "%"


@app.template_filter()
def bar(u):
    return "nodata" if u is None else "up" if u >= 0.999 else "partial" if u >= 0.99 else "down"


@app.errorhandler(urllib.error.URLError)
@app.errorhandler(TimeoutError)
def splunk_error(e):
    return f"Splunk API error: {e}", 502


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/api/status")
def api_status():
    return jsonify(status())


@app.get("/")
def index():
    return render_template("index.html", s=status(), interval=INTERVAL, stale_after=STALE_AFTER,
                           days=HISTORY_DAYS, incident_days=INCIDENT_DAYS, title=PAGE_TITLE, metric=METRIC)


if __name__ == "__main__":  # pragma: no cover  (local dev; the container runs gunicorn)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
