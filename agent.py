"""EKS core-service health agent: every INTERVAL_SECONDS runs the checks in checks.json and pushes
one `eks.health.up` gauge (1/0) per check to Splunk, dims: cluster, service, check.
`metrics` checks also forward selected Prometheus series from the scraped endpoints."""
import json
import math
import os
import re
import socket
import ssl
import time
import urllib.parse
import urllib.request

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
METRIC = "eks.health.up"
PATHS = {"deployment": "deployments", "statefulset": "statefulsets", "daemonset": "daemonsets"}
PROM_LINE = re.compile(r"^([a-zA-Z_:][\w:]*)(?:\{(.*)\})?\s+(\S+)")
PROM_LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def ready(kind, obj):
    s = obj.get("status", {})
    if kind == "daemonset":
        want, have = s.get("desiredNumberScheduled", 0), s.get("numberReady", 0)
    else:
        want, have = obj.get("spec", {}).get("replicas", 1), s.get("readyReplicas", 0)
    return want > 0 and have >= want


def bracket(host):
    return f"[{host}]" if ":" in host else host  # IPv6


def k8s_get(path):
    # Talk to the API by IP, not kubernetes.default.svc, so a CoreDNS outage doesn't fail every check.
    url = f"https://{bracket(os.environ['KUBERNETES_SERVICE_HOST'])}:{os.environ['KUBERNETES_SERVICE_PORT']}{path}"
    with open(f"{SA}/token") as f:  # re-read: projected tokens rotate
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + f.read()})
    with urllib.request.urlopen(req, context=ssl.create_default_context(cafile=f"{SA}/ca.crt"), timeout=5) as r:
        return json.load(r)


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
    pods = [p for p in k8s_get(f"/api/v1/namespaces/{t['namespace']}/pods?{q}")["items"] if p["status"].get("podIP")]
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


def run_check(c, out):
    """Raises if the check fails. Metrics checks append forwarded series to out."""
    if "metrics" in c:
        return scrape(c, out)
    if "dns" in c:
        socket.getaddrinfo(c["dns"], 443)
        return
    obj = k8s_get(f"/apis/apps/v1/namespaces/{c['namespace']}/{PATHS[c['kind']]}/{c['name']}")
    if not ready(c["kind"], obj):
        raise RuntimeError(f"not ready: {obj.get('status')}")


def label_of(c):
    if "metrics" in c:
        t = c["metrics"]
        return f"metrics/{t['url']}" if "url" in t else f"metrics/{t['namespace']}/{t['selector']}"
    return f"dns/{c['dns']}" if "dns" in c else f"{c['kind']}/{c['namespace']}/{c['name']}"


def main():
    cluster = os.environ["CLUSTER_NAME"]
    interval = int(os.environ.get("INTERVAL_SECONDS", "60"))
    # Default: the Splunk OTel Collector agent on this node (signalfx receiver), which adds its own token.
    url = os.environ.get("INGEST_URL") or f"http://{bracket(os.environ['SPLUNK_OTEL_AGENT'])}:9943/v2/datapoint"
    headers = {"Content-Type": "application/json"}
    if os.environ.get("SPLUNK_ACCESS_TOKEN"):  # only needed when INGEST_URL is Splunk's ingest endpoint
        headers["X-SF-Token"] = os.environ["SPLUNK_ACCESS_TOKEN"]
    with open(os.environ.get("CHECKS_FILE", "/app/checks.json")) as f:
        checks = json.load(f)

    while True:
        start = time.time()
        ts = int(start * 1000)
        payload = {"gauge": [], "cumulative_counter": []}
        for service, items in checks.items():
            for c in items:
                extra = []
                try:
                    run_check(c, extra)
                    up = 1
                except Exception as e:
                    up = 0
                    print(json.dumps({"level": "warn", "service": service, "check": label_of(c), "error": str(e)}), flush=True)
                base = {"cluster": cluster, "service": service}
                payload["gauge"].append({"metric": METRIC, "value": up, "timestamp": ts,
                                         "dimensions": {**base, "check": label_of(c)}})
                for name, value, dims, kind in extra:
                    payload[kind].append({"metric": name, "value": value, "timestamp": ts, "dimensions": {**dims, **base}})
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
            urllib.request.urlopen(req, timeout=10).close()
        except Exception as e:
            print(json.dumps({"level": "error", "msg": "ingest failed", "error": str(e)}), flush=True)
        time.sleep(max(0, interval - (time.time() - start)))


if __name__ == "__main__":
    main()
