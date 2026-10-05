#!/usr/bin/env python3
"""End-to-end tests for k8s-health on minikube, in two tiers.

  core     Light and repeatable. Needs only the Splunk OTel Collector and k8s-health with the status page
           (see /minikube-setup). Tests every stage: agent -> collector -> Splunk -> status page (and alerts,
           if the Terraform detectors exist).
  addons   Each add-on on its own: install it from its official chart, wait for the agent to report it up,
           break one of its workloads, wait for "down", restore, wait for "up", uninstall.
  cleanup  Uninstall every add-on below (and the CRDs they leave behind).
  list     The add-ons this script knows.

  python3 dev/minikube/test.py core [--deadman]
  python3 dev/minikube/test.py addons [NAME ...] [--keep]   # no names: all of them, one at a time
  python3 dev/minikube/test.py cleanup

Results come from the status page's /api/status, so they reflect what's in Splunk (about 1-2 minutes behind
the cluster). Needs kubectl, helm 3.14+ (or HELM=/path/to/helm), and openssl (Linkerd only).
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

NS = "k8s-health"
RELEASE = "k8s-health"
CANARY = "k8s-health-canary"  # the extraServices entry the core tier breaks on purpose
CORE = {"kubernetes", "coredns", "kube-proxy"}  # always present on minikube
HELM = os.environ.get("HELM", "helm")
HERE = os.path.dirname(os.path.abspath(__file__))
WAIT = 360  # seconds to wait for a state change to reach the page (checks run every 60 s, then Splunk)

# --- Add-ons --------------------------------------------------------------------------------------
# installs: ("helm", release, chart, namespace, [extra args], repo_url or None) or ("manifest", url, namespace)
# brk: (kind, namespace, name) of a workload to break. Deployments/StatefulSets are scaled to 0; DaemonSets
#      get an image that can't be pulled (a DaemonSet with 0 pods would still count as "ready").
# crds: API groups whose CRDs are deleted on uninstall (charts leave them behind).
# service: the name the agent reports (its profile name).

def helm_(release, chart, ns, args=(), repo=None):
    return ("helm", release, chart, ns, list(args), repo)


ADDONS = {
    "argocd": dict(
        installs=[("manifest", "https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml", "argocd")],
        brk=("deployment", "argocd", "argocd-repo-server"), crds=["argoproj.io"]),
    "fluxcd": dict(
        installs=[("manifest", "https://github.com/fluxcd/flux2/releases/latest/download/install.yaml", "flux-system")],
        brk=("deployment", "flux-system", "source-controller"), crds=["toolkit.fluxcd.io", "extensions.fluxcd.io"]),
    "external-secrets": dict(
        installs=[helm_("external-secrets", "external-secrets/external-secrets", "external-secrets",
                        repo="https://charts.external-secrets.io")],
        brk=("deployment", "external-secrets", "external-secrets-webhook"), crds=["external-secrets.io"]),
    "traefik": dict(
        installs=[helm_("traefik", "traefik/traefik", "traefik", repo="https://traefik.github.io/charts")],
        brk=("deployment", "traefik", "traefik"), crds=["traefik.io", "gateway.networking.k8s.io", "gateway.networking.x-k8s.io"]),
    "external-dns": dict(
        installs=[helm_("external-dns", "external-dns/external-dns", "external-dns",
                        ["--set", "provider.name=inmemory", "--set", "policy=upsert-only"],
                        repo="https://kubernetes-sigs.github.io/external-dns/")],
        brk=("deployment", "external-dns", "external-dns"), crds=["externaldns.k8s.io"]),
    "keda": dict(
        installs=[helm_("keda", "kedacore/keda", "keda", repo="https://kedacore.github.io/charts")],
        brk=("deployment", "keda", "keda-operator"), crds=["keda.sh"]),
    "kyverno": dict(
        installs=[helm_("kyverno", "kyverno/kyverno", "kyverno", repo="https://kyverno.github.io/kyverno/")],
        brk=("deployment", "kyverno", "kyverno-background-controller"),
        crds=["kyverno.io", "wgpolicyk8s.io"]),
    "fluentd": dict(
        installs=[helm_("fluentd", "fluent/fluentd", "logging", ["-f", os.path.join(HERE, "fluentd-values.yaml")],
                        repo="https://fluent.github.io/helm-charts")],
        brk=("daemonset", "logging", "fluentd"), crds=[]),
    "arc": dict(
        installs=[helm_("arc", "oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set-controller",
                        "arc-systems")],
        brk=("deployment", "arc-systems", "arc-gha-rs-controller"), crds=["actions.github.com"]),
    "cert-manager": dict(
        installs=[helm_("cert-manager", "jetstack/cert-manager", "cert-manager", ["--set", "crds.enabled=true"],
                        repo="https://charts.jetstack.io")],
        brk=("deployment", "cert-manager", "cert-manager-webhook"), crds=["cert-manager.io"]),
    "metrics-server": dict(
        installs=[helm_("metrics-server", "metrics-server/metrics-server", "kube-system",
                        ["--set", "args={--kubelet-insecure-tls}"], repo="https://kubernetes-sigs.github.io/metrics-server/")],
        brk=("deployment", "kube-system", "metrics-server"), crds=[]),
    "prometheus": dict(
        installs=[helm_("kps", "prometheus-community/kube-prometheus-stack", "monitoring",
                        ["--set", "grafana.enabled=false", "--set", "prometheus.prometheusSpec.retention=6h"],
                        repo="https://prometheus-community.github.io/helm-charts")],
        brk=("deployment", "monitoring", "kps-kube-prometheus-stack-operator"), crds=["monitoring.coreos.com"]),
    "istio": dict(
        installs=[helm_("istio-base", "istio/base", "istio-system", repo="https://istio-release.storage.googleapis.com/charts"),
                  helm_("istiod", "istio/istiod", "istio-system",
                        ["--set", "resources.requests.memory=256Mi", "--set", "resources.requests.cpu=100m",
                         "--set", "autoscaleEnabled=false"]),
                  helm_("istio-ingressgateway", "istio/gateway", "istio-ingress",
                        ["--set", "autoscaling.enabled=false", "--set", "service.type=ClusterIP"])],
        brk=("deployment", "istio-ingress", "istio-ingressgateway"), crds=["istio.io"]),
    "linkerd": dict(
        installs=[helm_("linkerd-crds", "linkerd-edge/linkerd-crds", "linkerd", repo="https://helm.linkerd.io/edge"),
                  helm_("linkerd-control-plane", "linkerd-edge/linkerd-control-plane", "linkerd", ["@linkerd-certs"])],
        brk=("deployment", "linkerd", "linkerd-identity"), crds=["linkerd.io"]),
    "gatekeeper": dict(
        installs=[helm_("gatekeeper", "gatekeeper/gatekeeper", "gatekeeper-system", ["--set", "replicas=1"],
                        repo="https://open-policy-agent.github.io/gatekeeper/charts")],
        brk=("deployment", "gatekeeper-system", "gatekeeper-audit"), crds=["gatekeeper.sh"]),
    "falco": dict(  # the syscall driver needs a kernel with BTF (Docker Desktop's recent kernels have it)
        installs=[helm_("falco", "falcosecurity/falco", "falco",
                        ["--set", "driver.kind=modern_ebpf", "--set", "metrics.enabled=true", "--set", "tty=true"],
                        repo="https://falcosecurity.github.io/charts")],
        brk=("daemonset", "falco", "falco"), crds=["falcosecurity.dev"]),
    "envoy-gateway": dict(
        installs=[helm_("eg", "oci://docker.io/envoyproxy/gateway-helm", "envoy-gateway-system")],
        brk=("deployment", "envoy-gateway-system", "envoy-gateway"),
        crds=["envoyproxy.io", "gateway.networking.k8s.io", "gateway.networking.x-k8s.io"]),
    "opentelemetry-operator": dict(
        needs=["cert-manager"],  # for its webhook certificate
        installs=[helm_("otel-operator", "open-telemetry/opentelemetry-operator", "opentelemetry-operator-system",
                        ["--set", "manager.collectorImage.repository=otel/opentelemetry-collector-k8s"],
                        repo="https://open-telemetry.github.io/opentelemetry-helm-charts")],
        brk=("deployment", "opentelemetry-operator-system", "otel-operator-opentelemetry-operator"), crds=["opentelemetry.io"]),
    "cloudnative-pg": dict(
        installs=[helm_("cnpg", "cnpg/cloudnative-pg", "cnpg-system", repo="https://cloudnative-pg.github.io/charts")],
        brk=("deployment", "cnpg-system", "cnpg-cloudnative-pg"), crds=["postgresql.cnpg.io"]),
}

# --- Plumbing -------------------------------------------------------------------------------------
results = []


def run(*cmd, check=True, quiet=False, timeout=900):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode:
        raise RuntimeError(f"{' '.join(cmd[:4])}... failed: {(p.stderr or p.stdout).strip()[-400:]}")
    return p.stdout


def kubectl(*args, **kw):
    return run("kubectl", *args, **kw)


def say(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def step(name, fn):
    t = time.time()
    try:
        detail = fn() or ""
        results.append((True, name, detail))
        say(f"PASS  {name} ({time.time() - t:.0f}s) {detail}")
    except Exception as e:  # report every step, keep going where it makes sense
        results.append((False, name, str(e)))
        say(f"FAIL  {name} ({time.time() - t:.0f}s): {e}")
        return False
    return True


def until(what, cond, timeout=WAIT, every=10):
    end = time.time() + timeout
    while True:
        try:
            got = cond()
            if got:
                return got
        except Exception:  # transient (port-forward restarting, API slow): retry until the deadline
            pass
        if time.time() > end:
            raise TimeoutError(f"{what}: not after {timeout}s")
        time.sleep(every)


class Page:
    """The status page's /api/status, through a kubectl port-forward."""

    def __init__(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.proc = None

    def get(self):
        if not self.proc or self.proc.poll() is not None:
            self.proc = subprocess.Popen(["kubectl", "-n", NS, "port-forward", f"svc/{RELEASE}-status-page", f"{self.port}:80"],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(3)
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/status", timeout=30) as r:
            return json.load(r)

    def services(self):
        return next(iter(self.get()["clusters"].values()), {})

    def state(self, service):
        return self.services().get(service, {}).get("state")

    def with_state(self, state, ignore=(CANARY,)):
        """Services in a state. A just-uninstalled add-on (or the canary) shows as stale for ~15 minutes
        before the page drops it, so the tests compare sets of up/down services rather than the headline."""
        return {k for k, v in self.services().items() if v["state"] == state and k not in ignore}

    def opened(self, rule, since):
        """started_ms of an active `rule` incident that started after `since` (epoch s), else None. Matching on the
        start time keeps older incidents (e.g. from add-ons just removed, still auto-resolving) from counting."""
        return next((i["started_ms"] for i in self.get()["incidents"]
                     if i["active"] and i["rule"] == rule and i["started_ms"] >= (since - 30) * 1000), None)

    def wait_opened(self, rule, since, into, **kw):
        """Wait for opened(); remember its start time in `into` so the resolve step waits for that incident."""
        into["t"] = until(f"a new {rule!r} incident", lambda: self.opened(rule, since), **kw)
        return "opened"

    def resolved(self, rule, started_ms):
        return not any(i["active"] and i["rule"] == rule and i["started_ms"] == started_ms for i in self.get()["incidents"])

    def close(self):
        if self.proc:
            self.proc.terminate()


def leader():
    return kubectl("-n", NS, "get", "lease", f"{RELEASE}-agent", "-o", "jsonpath={.spec.holderIdentity}").strip()


def splunk_detectors_exist():
    """True if the Terraform detectors exist, so the alert steps apply (asks Splunk with the page's token)."""
    env = json.loads(kubectl("-n", NS, "get", "deploy", f"{RELEASE}-status-page", "-o", "json"))["spec"]["template"]["spec"]["containers"][0]["env"]
    realm = next(e["value"] for e in env if e["name"] == "SPLUNK_REALM")
    ref = next(e["valueFrom"]["secretKeyRef"] for e in env if e["name"] == "SPLUNK_API_TOKEN")
    token = kubectl("-n", NS, "get", "secret", ref["name"], "-o", f"go-template={{{{index .data \"{ref['key']}\" | base64decode}}}}").strip()
    req = urllib.request.Request(f"https://api.{realm}.signalfx.com/v2/detector?" + urllib.parse.urlencode({"name": "k8s-health core services"}),
                                 headers={"X-SF-Token": token})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("count", 0) > 0


def preflight():
    ctx = kubectl("config", "current-context").strip()
    if ctx != "minikube":
        sys.exit(f"kubectl context is {ctx!r}, not minikube: refusing to run (these tests break workloads on purpose)")
    ver = run(HELM, "version", "--template", "{{.Version}}").strip().lstrip("v").split(".")
    if (int(ver[0]), int(ver[1])) < (3, 14):
        sys.exit(f"helm {'.'.join(ver)} is too old: need 3.14+ (or set HELM=/path/to/newer/helm)")
    releases = {(r["namespace"], r["name"]) for r in json.loads(run(HELM, "list", "-A", "-o", "json"))}
    if not any(name == RELEASE for _, name in releases):
        sys.exit(f"no {RELEASE} release: set up the project first (Claude Code: /minikube-setup)")
    if not kubectl("-n", NS, "get", "deploy", f"{RELEASE}-status-page", "--ignore-not-found", "-o", "name").strip():
        sys.exit("the status page isn't enabled: the tests read results from it (see /minikube-setup phase 5)")
    return releases


# --- Tier 1: core ---------------------------------------------------------------------------------

def core(args):
    preflight()
    page = Page()
    alerts = splunk_detectors_exist()
    say(f"Splunk detectors {'found: alert steps included' if alerts else 'not found (Terraform not applied): alert steps skipped'}")
    try:
        step("agents ready and a leader elected", lambda: (
            kubectl("-n", NS, "rollout", "status", f"deploy/{RELEASE}-agent", "--timeout=180s"), f"leader {leader()}")[1])
        step("leader completes a check cycle without errors", lambda: until(
            "a clean cycle", lambda: "check failed" not in kubectl("-n", NS, "logs", leader(), "--since=70s") and "clean", timeout=300, every=30))
        def all_up():
            up, down = page.with_state("up"), page.with_state("down")
            return not down and CORE <= up and f"{len(up)} up" + (f", {len(page.with_state('stale'))} stale ignored" if page.with_state("stale") else "")
        step("status page: core services up, none down", lambda: until("all up", all_up))
        reporting = page.with_state("up")

        # Failover first: the canary's leftover series would otherwise read as stale for 15 minutes.
        def failover():
            old = leader()
            kubectl("-n", NS, "delete", "pod", old, "--wait=false")
            t = time.time()
            new = until("a new leader", lambda: (lambda l: l if l and l != old else None)(leader()), timeout=90, every=1)
            took = time.time() - t
            time.sleep(150)  # 2+ cycles: data must keep flowing
            lost = reporting - page.with_state("up")
            if lost:
                raise RuntimeError(f"not up after failover: {sorted(lost)}")
            return f"{old} -> {new} in {took:.1f}s, no gap"
        step("leader failover", failover)

        ensure_canary_configured()
        kubectl("create", "namespace", CANARY, check=False, quiet=True)
        kubectl("-n", CANARY, "create", "deployment", "canary", "--image=registry.k8s.io/pause:3.10", check=False)
        step("canary service detected and up", lambda: until("canary up", lambda: page.state(CANARY) == "up" and "up"))
        broke = time.time()
        kubectl("-n", CANARY, "scale", "deployment", "canary", "--replicas=0")
        step("outage shows on the page", lambda: until("canary down", lambda: page.state(CANARY) == "down" and "down"))
        incident = {}
        if alerts:
            step("outage opens a 'Service down' incident", lambda: page.wait_opened("Service down", broke, incident))
        kubectl("-n", CANARY, "scale", "deployment", "canary", "--replicas=1")
        step("recovery shows on the page", lambda: until("canary up", lambda: page.state(CANARY) == "up" and "up"))
        if incident.get("t"):
            step("incident resolves", lambda: until("resolved", lambda: page.resolved("Service down", incident["t"]) and "resolved"))
        kubectl("delete", "namespace", CANARY, "--wait=false")

        if args.deadman:
            stopped, deadman = time.time(), {}
            kubectl("-n", NS, "scale", f"deploy/{RELEASE}-agent", "--replicas=0")
            try:
                step("dead man's switch: page goes stale with no agents", lambda: until(
                    "stale", lambda: reporting <= page.with_state("stale") and "stale", timeout=600))
                if alerts:
                    step("dead man's switch: 'Agent not reporting' fires", lambda: page.wait_opened(
                        "Agent not reporting", stopped, deadman, timeout=600, every=15))
            finally:
                kubectl("-n", NS, "scale", f"deploy/{RELEASE}-agent", "--replicas=3")
            step("agents restored: page up again", lambda: until("up", lambda: reporting <= page.with_state("up") and "up", timeout=600))
            if deadman.get("t"):
                step("dead man's switch clears", lambda: until(
                    "cleared", lambda: page.resolved("Agent not reporting", deadman["t"]) and "cleared", timeout=600, every=15))
    finally:
        page.close()


def ensure_canary_configured():
    """Add the canary to the release's extraServices (once). It's in auto mode, so without its namespace
    it's simply not reported: leaving it configured is harmless."""
    cm = kubectl("-n", NS, "get", "configmap", f"{RELEASE}-agent", "-o", "jsonpath={.data.checks\\.json}")
    if CANARY in cm:
        return
    say(f"adding the {CANARY} service to the release (agent.extraServices, auto mode)")
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(f"agent:\n  extraServices:\n    {CANARY}:\n      enabled: auto\n"
                f"      checks: [{{kind: deployment, namespace: {CANARY}, name: canary}}]\n")
    chart = json.loads(run(HELM, "list", "-n", NS, "-o", "json"))[0]["chart"].rsplit("-", 1)
    run(HELM, "upgrade", RELEASE, "oci://ghcr.io/jthiatt/charts/k8s-health", "--version", chart[1], "-n", NS,
        "--reset-then-reuse-values", "-f", f.name, "--wait")
    os.unlink(f.name)
    kubectl("-n", NS, "rollout", "status", f"deploy/{RELEASE}-agent", "--timeout=180s")


# --- Tier 2: add-ons ------------------------------------------------------------------------------

def linkerd_cert_args(tmp):
    def ssl(*a):
        run("openssl", *a)
    ssl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", f"{tmp}/ca.key")
    ssl("req", "-x509", "-new", "-key", f"{tmp}/ca.key", "-subj", "/CN=root.linkerd.cluster.local", "-days", "365",
        "-out", f"{tmp}/ca.crt", "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    ssl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", f"{tmp}/issuer.key")
    ssl("req", "-new", "-key", f"{tmp}/issuer.key", "-subj", "/CN=identity.linkerd.cluster.local", "-out", f"{tmp}/issuer.csr")
    with open(f"{tmp}/ext", "w") as f:
        f.write("basicConstraints=critical,CA:TRUE,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\n")
    ssl("x509", "-req", "-in", f"{tmp}/issuer.csr", "-CA", f"{tmp}/ca.crt", "-CAkey", f"{tmp}/ca.key", "-CAcreateserial",
        "-days", "30", "-extfile", f"{tmp}/ext", "-out", f"{tmp}/issuer.crt")
    return ["--set-file", f"identityTrustAnchorsPEM={tmp}/ca.crt", "--set-file", f"identity.issuer.tls.crtPEM={tmp}/issuer.crt",
            "--set-file", f"identity.issuer.tls.keyPEM={tmp}/issuer.key"]


def install(name):
    tmp = tempfile.mkdtemp()
    try:
        for inst in ADDONS[name]["installs"]:
            if inst[0] == "manifest":
                _, url, ns = inst
                kubectl("create", "namespace", ns, check=False)
                kubectl("apply", "--server-side", "--force-conflicts", "-n", ns, "-f", url)
                continue
            _, release, chart, ns, extra, repo = inst
            if repo:
                run(HELM, "repo", "add", chart.split("/")[0], repo, "--force-update")
                run(HELM, "repo", "update", chart.split("/")[0])
            extra = [a for x in extra for a in (linkerd_cert_args(tmp) if x == "@linkerd-certs" else [x])]
            run(HELM, "upgrade", "--install", release, chart, "-n", ns, "--create-namespace", *extra, "--wait", "--timeout", "10m")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def uninstall(name):
    a = ADDONS[name]
    for inst in reversed(a["installs"]):
        if inst[0] == "manifest":
            kubectl("delete", "-n", inst[2], "-f", inst[1], "--ignore-not-found", "--wait=false", check=False)
        else:
            run(HELM, "uninstall", inst[1], "-n", inst[3], "--ignore-not-found", "--wait", check=False)
    if a["crds"]:
        crds = [c for c in kubectl("get", "crd", "-o", "name").split() if c.split("/", 1)[1].split(".", 1)[1].endswith(tuple(a["crds"]))]
        if crds:
            kubectl("delete", *crds, "--wait=false", check=False)
    namespaces = {inst[2] if inst[0] == "manifest" else inst[3] for inst in a["installs"]} - {"kube-system", "default"}
    for ns in namespaces:
        kubectl("delete", "namespace", ns, "--ignore-not-found", "--wait=false", check=False)


def installed(name, releases):
    first = ADDONS[name]["installs"][0]
    if first[0] == "manifest":
        return bool(kubectl("get", "namespace", first[2], "--ignore-not-found", "-o", "name").strip())
    return (first[3], first[1]) in releases


def break_workload(kind, ns, name):
    if kind == "daemonset":
        kubectl("-n", ns, "patch", kind, name, "--type=json", "-p",
                '[{"op":"replace","path":"/spec/template/spec/containers/0/image","value":"registry.k8s.io/pause:does-not-exist"}]')
        return lambda: kubectl("-n", ns, "rollout", "undo", f"{kind}/{name}")
    replicas = kubectl("-n", ns, "get", kind, name, "-o", "jsonpath={.spec.replicas}").strip() or "1"
    kubectl("-n", ns, "scale", kind, name, "--replicas=0")
    return lambda: kubectl("-n", ns, "scale", kind, name, f"--replicas={replicas}")


def addons(args):
    releases = preflight()
    names = args.names or list(ADDONS)
    unknown = [n for n in names if n not in ADDONS]
    if unknown:
        sys.exit(f"unknown add-on(s): {', '.join(unknown)} (see: test.py list)")
    page = Page()
    try:
        for name in names:
            a = ADDONS[name]
            say(f"=== {name}")
            was_installed = installed(name, releases)
            deps = [d for d in a.get("needs", []) if not installed(d, releases)]
            for d in deps:
                say(f"installing dependency {d}")
                install(d)
            if not step(f"{name}: install from its official chart", lambda: install(name)):
                continue
            ok = step(f"{name}: detected and up", lambda: until(f"{name} up", lambda: page.state(name) == "up" and "up", timeout=600))
            if ok:
                kind, ns, wl = a["brk"]
                restore = break_workload(kind, ns, wl)
                try:
                    step(f"{name}: {kind} {ns}/{wl} broken -> down", lambda: until(f"{name} down", lambda: page.state(name) == "down" and "down"))
                finally:
                    restore()
                step(f"{name}: restored -> up", lambda: until(f"{name} up", lambda: page.state(name) == "up" and "up", timeout=600))
            if not args.keep and not was_installed:
                uninstall(name)
                for d in deps:
                    uninstall(d)
                say(f"{name} uninstalled")
    finally:
        page.close()


def cleanup(_):
    releases = preflight()
    for name in ADDONS:
        if installed(name, releases):
            say(f"uninstalling {name}")
            uninstall(name)
    groups = tuple(g for a in ADDONS.values() for g in a["crds"])  # also CRDs left by earlier, partial uninstalls
    crds = [c for c in kubectl("get", "crd", "-o", "name").split() if c.split("/", 1)[1].split(".", 1)[1].endswith(groups)]
    if crds:
        say(f"deleting {len(crds)} leftover CRDs")
        kubectl("delete", *crds, "--wait=false", check=False)
    say("done (namespaces finish terminating in the background)")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("core", help="the light end-to-end tier")
    c.add_argument("--deadman", action="store_true", help="also stop all agents to test the dead man's switch (~10 min; "
                                                          "with the Terraform detectors this sends a real alert)")
    a = sub.add_parser("addons", help="install, check, break, restore and uninstall add-ons one at a time")
    a.add_argument("names", nargs="*", help="add-ons to test (default: all)")
    a.add_argument("--keep", action="store_true", help="leave them installed afterwards")
    sub.add_parser("cleanup", help="uninstall every known add-on")
    sub.add_parser("list", help="list the add-ons")
    args = p.parse_args()
    if args.cmd == "list":
        print("\n".join(ADDONS))
        return
    start = time.time()
    {"core": core, "addons": addons, "cleanup": cleanup}[args.cmd](args)
    if results:
        failed = [r for r in results if not r[0]]
        print(f"\n{len(results) - len(failed)}/{len(results)} passed in {(time.time() - start) / 60:.0f} min")
        for _, name, detail in failed:
            print(f"  FAIL {name}: {detail}")
        sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
