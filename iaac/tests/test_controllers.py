"""Answers that only a real controller can prove: a LoadBalancer IP (NET-021), traffic through a Gateway
(NET-016 split, NET-023 HTTPS + allowedRoutes), the certificate an Ingress controller serves (NET-024), a CSI
volume that grows (STO-009). Needs the VM lab with the controllers profile:

    make vm-up ADDONS=full            (or HA=1 ADDONS=full)
    make vm-test ARGS=test_controllers

The questions name the classes 'envoy' and 'nginx'; the lab runs Envoy Gateway (class 'eg') and Traefik
(class 'traefik'): the tests substitute them. NET-017's corporate DNS server is a CoreDNS run by the test
itself, so StubDomain runs on kind too.
"""
import collections
import json
import re
import shlex
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from lib import VM, ClusterTest, answer, final_file, run_pod, sh, wait_for, wrong
from test_networking_more import make_cert

CORP_DNS_IP = "10.96.0.53"            # inside the Service range of kind and of the lab


def present(cmd):
    return bool(sh(cmd))


def wait_jsonpath(kind, name, ns, path, timeout=180):
    """Poll until a jsonpath is non-empty; returns its value (or '')."""
    end = time.time() + timeout
    while time.time() < end:
        out = sh(f"kubectl get {kind} {name} -n {ns} -o jsonpath='{{{path}}}'").out.strip("'")
        if out:
            return out
        time.sleep(3)
    return ""


def host_until(cmd, expect, timeout=120):
    """Retry a command on this machine until its output contains `expect` (an IP is assigned
    before the L2 announcement / the proxy behind it is ready)."""
    end, out = time.time() + timeout, ""
    while time.time() < end:
        rc, out = host(cmd)
        if expect in out:
            return out
        time.sleep(3)
    return out


def host(cmd, timeout=60):
    """A command on THIS machine (curl / openssl against the lab's LoadBalancer IPs)."""
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).strip()


class Controllers(ClusterTest):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ok = VM and present("kubectl get gatewayclass eg") and present("kubectl get ingressclass traefik") \
            and present("kubectl get ipaddresspool lab -n metallb-system") and present("kubectl get sc csi-hostpath-sc")
        if not ok:
            raise unittest.SkipTest("needs the VM lab with ADDONS=full (make vm-up ADDONS=full)")

    def echo(self, name, ns, text, port=8080):
        """A tiny HTTP server answering `text` on 8080, behind Service `name`:`port`."""
        self.cleanup_cmd(f"kubectl delete deploy,svc {name} -n {ns}")
        self.ok(f"kubectl create deployment {name} -n {ns} --image=hashicorp/http-echo:1.0 -- "
                f"/http-echo -listen=:8080 -text={text}")
        self.ok(f"kubectl expose deployment {name} -n {ns} --port={port} --target-port=8080")
        self.ok(f"kubectl rollout status deployment {name} -n {ns} --timeout=180s", timeout=200)

    # ------------------------------------------------------------------ NET-021
    def test_net021_loadbalancer_gets_an_ip(self):
        self.cleanup_cmd("kubectl delete deploy shop-api -n shop; kubectl delete svc shop-lb -n shop")
        self.ok("kubectl create deployment shop-api -n shop --image=nginxinc/nginx-unprivileged:1.27")
        self.ok("kubectl rollout status deployment shop-api -n shop --timeout=180s", timeout=200)
        self.answer_ok("NET-021", "s1")
        ip = wait_jsonpath("svc", "shop-lb", "shop", ".status.loadBalancer.ingress[0].ip")
        self.assertRegex(ip, r"^192\.168\.124\.2\d\d$")                  # from the MetalLB pool
        page = host_until(f"curl -s -m 5 http://{ip}/", "Welcome to nginx")
        self.assertIn("Welcome to nginx", page)
        port = self.answer_ok("NET-021", "s3").out.strip("'")             # s3: the NodePort still works
        page = host_until(f"curl -s -m 5 http://192.168.124.10:{port}/", "Welcome to nginx")
        self.assertIn("Welcome to nginx", page)

    # ------------------------------------------------------------------ NET-016 (+ NET-014/015 trap)
    def test_net016_weighted_split_through_envoy_gateway(self):
        self.echo("checkout-v1", "shop", "v1")
        self.echo("checkout-v2", "shop", "v2")
        self.cleanup_cmd("kubectl delete gateway shop-gw -n shop; kubectl delete httproute checkout empty -n shop")
        self.ok("kubectl apply -f -", stdin=(
            "apiVersion: gateway.networking.k8s.io/v1\nkind: Gateway\nmetadata: {name: shop-gw, namespace: shop}\n"
            "spec:\n  gatewayClassName: eg\n  listeners:\n  - {name: http, protocol: HTTP, port: 80}\n"))
        ip = wait_jsonpath("gateway", "shop-gw", "shop", ".status.addresses[0].value", 300)
        self.assertTrue(ip, "the Gateway got no address")
        self.ok("kubectl apply -f -", stdin=final_file("NET-016", "s1"))
        desc = wait_for(answer("NET-016", "s4"), "ResolvedRefs", 120).out                 # s4: conditions
        self.assertRegex(desc, r"(?s)Accepted.*True")
        loop = answer("NET-016", "s2").replace("curl -s", f"curl -s --resolve shop.example.com:80:{ip}")
        host_until(loop, "v1")                                                               # proxy ready
        counts = collections.Counter()
        for _ in range(10):                                                                # 200 requests
            rc, out = host(loop, timeout=120)
            counts.update(re.findall(r"v[12]", out))
        self.assertGreaterEqual(sum(counts.values()), 190, counts)                         # a few may time out
        self.assertTrue(8 <= counts["v2"] <= 40, f"expected ~10 % to v2: {counts}")       # 90/10, loosely
        # NET-014/015 trap claim: a rule without backendRefs answers 5xx
        self.ok("kubectl apply -f -", stdin=(
            "apiVersion: gateway.networking.k8s.io/v1\nkind: HTTPRoute\nmetadata: {name: empty, namespace: shop}\n"
            "spec:\n  parentRefs: [{name: shop-gw}]\n  hostnames: [empty.example.com]\n"
            "  rules: [{matches: [{path: {type: PathPrefix, value: /}}]}]\n"))
        code = ""
        for _ in range(30):
            rc, code = host(f"curl -s -o /dev/null -w '%{{http_code}}' --resolve empty.example.com:80:{ip} "
                            "http://empty.example.com/")
            if code.startswith("5"):
                break
            time.sleep(2)
        self.assertRegex(code, r"^5\d\d$")

    # ------------------------------------------------------------------ NET-023
    def test_net023_https_gateway_and_allowed_routes(self):
        self.cleanup_cmd("kubectl delete gateway secure-gw -n infra; kubectl delete secret shop-tls -n infra; "
                         "kubectl delete httproute shop-route -n shop; kubectl delete httproute shop-route -n default")
        with tempfile.TemporaryDirectory() as d:
            make_cert(d, "tls.crt", "tls.key", "shop.example.com")
            self.answer_ok("NET-023", "s1", cwd=d)
        self.echo("shop-web", "shop", "shop")
        self.ok("kubectl apply -f -", stdin=final_file("NET-023", "s3").replace("gatewayClassName: envoy",
                                                                                "gatewayClassName: eg"))
        ip = wait_jsonpath("gateway", "secure-gw", "infra", ".status.addresses[0].value", 300)
        self.assertTrue(ip)
        self.ok("kubectl apply -f -", stdin=final_file("NET-023", "s4"))
        desc = wait_for(answer("NET-023", "s5"), "Attached Routes:  1", 120).out          # s5
        self.assertIn("Attached Routes:  1", desc)
        out = host_until(f"curl -sk -m 5 --resolve shop.example.com:443:{ip} https://shop.example.com/", "shop")
        self.assertIn("shop", out)
        rc, cert = host(f"echo | openssl s_client -connect {ip}:443 -servername shop.example.com 2>/dev/null "
                        "| openssl x509 -noout -subject")
        self.assertIn("shop.example.com", cert)                                            # our Secret's cert
        # the same route from a namespace the listener does not admit is refused
        other = final_file("NET-023", "s4").replace("namespace: shop", "namespace: default")
        self.ok("kubectl apply -f -", stdin=other)
        st = wait_for("kubectl get httproute shop-route -n default -o jsonpath='{.status.parents[0].conditions}'",
                      "NotAllowedByListeners", 120).out
        self.assertIn("NotAllowedByListeners", st)

    # ------------------------------------------------------------------ NET-024
    def test_net024_ingress_serves_the_secret_certificate(self):
        self.cleanup_cmd("kubectl delete ingress store -n store; kubectl delete secret store-tls -n store")
        with tempfile.TemporaryDirectory() as d:
            make_cert(d, "store.crt", "store.key", "store.example.com")
            self.answer_ok("NET-024", "s1", cwd=d)
        self.echo("web-svc", "store", "store", port=80)
        self.ok("kubectl apply -f -", stdin=final_file("NET-024", "s2").replace("ingressClassName: nginx",
                                                                                "ingressClassName: traefik"))
        ip = wait_jsonpath("svc", "traefik", "traefik", ".status.loadBalancer.ingress[0].ip", 120)
        self.assertTrue(ip)
        cert = host_until(f"echo | openssl s_client -connect {ip}:443 -servername store.example.com 2>/dev/null "
                          "| openssl x509 -noout -subject", "store.example.com")
        self.assertIn("store.example.com", cert)                                           # the Secret's cert
        rc, other = host(f"echo | openssl s_client -connect {ip}:443 -servername www.store.example.com 2>/dev/null "
                         "| openssl x509 -noout -subject")
        self.assertNotIn("CN=store.example.com", other)                                    # trap: host mismatch
        page = host_until(f"curl -sk -m 5 --resolve store.example.com:443:{ip} https://store.example.com/", "store")
        self.assertIn("store", page)

    # ------------------------------------------------------------------ STO-009
    def test_sto009_a_csi_claim_really_grows(self):
        self.cleanup_cmd("kubectl delete pod reporter -n finance; kubectl delete pvc reports -n finance")
        self.ok("kubectl apply -f -", stdin=(
            "apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata: {name: reports, namespace: finance}\n"
            "spec:\n  storageClassName: csi-hostpath-sc\n  accessModes: [ReadWriteOnce]\n"
            "  resources: {requests: {storage: 2Gi}}\n"))
        self.ok("kubectl apply -f -", stdin=(
            "apiVersion: v1\nkind: Pod\nmetadata: {name: reporter, namespace: finance}\n"
            "spec:\n  containers: [{name: app, image: busybox:1.36, command: [sleep, '3600'],\n"
            "    volumeMounts: [{name: data, mountPath: /reports}]}]\n"
            "  volumes: [{name: data, persistentVolumeClaim: {claimName: reports}}]\n"))
        self.ok("kubectl wait --for=condition=Ready pod/reporter -n finance --timeout=180s", timeout=200)
        claim = json.loads(self.ok("kubectl get pvc reports -n finance -o json").out)
        for k in ("metadata", "status"):
            claim.pop(k, None)
        claim["metadata"] = {"name": "reports", "namespace": "finance"}
        claim["spec"]["resources"]["requests"]["storage"] = "5Gi"                          # what s2's edit does
        self.ok("kubectl apply -f -", stdin=json.dumps(claim))
        res = wait_for(answer("STO-009", "s4"), "5Gi", 180)                                 # s4: CAPACITY 5Gi
        self.assertRegex(res.out, r"reports\s+Bound\s+\S+\s+5Gi")
        events = self.answer_ok("STO-009", "s3").out                                       # s3: the resize story
        self.assertRegex(events, r"Resizing|ExternalExpanding|VolumeResizeSuccessful|FileSystemResizeSuccessful")
        self.assertIn("/reports", self.answer_ok("STO-009", "s5").out)                      # s5 (a host-path
        # volume reports the node's filesystem size in df: the size check is CAPACITY above)


class StubDomain(ClusterTest):
    """NET-017 s5 for real: a 'company' DNS server (a CoreDNS answering git.corp.example) at a fixed
    ClusterIP standing in for 10.0.0.53, and the cluster's CoreDNS forwarding corp.example to it."""

    def test_net017_corp_example_resolves_through_the_stub_domain(self):
        original = json.loads(self.ok("kubectl get cm coredns -n kube-system -o json").out)["data"]["Corefile"]
        self.cleanup_cmd("kubectl rollout status deployment coredns -n kube-system --timeout=120s")
        self.cleanup_cmd("kubectl rollout restart deployment coredns -n kube-system")
        self.addCleanup(lambda: sh("kubectl -n kube-system patch cm coredns --type=merge -p "
                                   + shlex.quote(json.dumps({"data": {"Corefile": original}}))))
        image = self.ok("kubectl get deploy coredns -n kube-system "
                        "-o jsonpath='{.spec.template.spec.containers[0].image}'").out.strip("'")
        self.cleanup_cmd("kubectl delete ns corp-dns")
        corp = ("apiVersion: v1\nkind: Namespace\nmetadata: {name: corp-dns}\n---\n"
                "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: corefile, namespace: corp-dns}\n"
                "data:\n  Corefile: |\n    corp.example:53 {\n        hosts {\n            10.20.0.7 git.corp.example\n"
                "        }\n    }\n---\n"
                "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: corp-dns, namespace: corp-dns}\n"
                "spec:\n  selector: {matchLabels: {app: corp-dns}}\n  template:\n    metadata: {labels: {app: corp-dns}}\n"
                f"    spec:\n      containers:\n      - name: coredns\n        image: {image}\n"
                "        args: [-conf, /etc/coredns/Corefile]\n"
                "        volumeMounts: [{name: cfg, mountPath: /etc/coredns}]\n"
                "      volumes: [{name: cfg, configMap: {name: corefile}}]\n---\n"
                "apiVersion: v1\nkind: Service\nmetadata: {name: corp-dns, namespace: corp-dns}\n"
                f"spec:\n  clusterIP: {CORP_DNS_IP}\n  selector: {{app: corp-dns}}\n"
                "  ports: [{name: dns, port: 53, protocol: UDP}, {name: dns-tcp, port: 53, protocol: TCP}]\n")
        self.ok("kubectl apply -f -", stdin=corp)
        self.ok("kubectl rollout status deployment corp-dns -n corp-dns --timeout=180s", timeout=200)
        with tempfile.TemporaryDirectory() as d:
            Path(d, "cf").write_text(final_file("NET-017", "s2").replace("10.0.0.53", CORP_DNS_IP))
            self.ok("kubectl -n kube-system create cm coredns --from-file=Corefile=cf --dry-run=client -o yaml "
                    "| kubectl apply -f -", cwd=d)
        self.answer_ok("NET-017", "s3")
        self.answer_ok("NET-017", "s4", timeout=200)                                          # s4: rollout status
        res = run_pod(answer("NET-017", "s5"), expect="10.20.0.7")                            # s5: through CoreDNS
        self.assertIn("10.20.0.7", res.out)
        direct = run_pod(wrong("NET-017", "s5", 2).replace("10.0.0.53", CORP_DNS_IP), expect="10.20.0.7")
        self.assertIn("10.20.0.7", direct.out)                 # trap: asks the server directly, works anyway


class CorefileReload(ClusterTest):
    """NET-017 s3's explain: without a restart, the 'reload' plugin picks up a changed Corefile within
    about two minutes (kubelet ConfigMap sync + reload interval). Measured: ~1 min on the lab. A
    comment-only change is NOT reloaded (the plugin hashes the parsed configuration)."""

    def test_net017_reload_plugin_picks_up_a_change_without_restart(self):
        original = json.loads(self.ok("kubectl get cm coredns -n kube-system -o json").out)["data"]["Corefile"]
        self.assertIn("reload", original)
        self.addCleanup(lambda: sh("kubectl -n kube-system patch cm coredns --type=merge -p "
                                   + shlex.quote(json.dumps({"data": {"Corefile": original}}))))
        # an unambiguous signal: a record that only exists in the new Corefile
        probe = original + "iaac.probe:53 {\n    hosts {\n        10.9.9.9 x.iaac.probe\n    }\n}\n"
        self.cleanup_cmd("kubectl delete pod dnsprobe --wait=false")
        self.ok("kubectl run dnsprobe --image=busybox:1.36 --restart=Never -- sleep 600")
        self.ok("kubectl wait --for=condition=Ready pod/dnsprobe --timeout=120s")
        self.assertNotIn("10.9.9.9", sh("kubectl exec dnsprobe -- nslookup x.iaac.probe").out)
        start = time.time()
        self.ok("kubectl -n kube-system patch cm coredns --type=merge -p "
                + shlex.quote(json.dumps({"data": {"Corefile": probe}})))
        took = None
        while time.time() - start < 180:
            if "10.9.9.9" in sh("kubectl exec dnsprobe -- nslookup x.iaac.probe", timeout=20).out:
                took = time.time() - start
                break
            time.sleep(3)
        self.assertIsNotNone(took, "the new Corefile was not active within 180 s")
        self.assertLess(took, 150, "slower than the 'about two minutes' the explanation states")
        print(f"\nNET-017 note: the changed Corefile answered after {took:.0f} s, without a restart")
