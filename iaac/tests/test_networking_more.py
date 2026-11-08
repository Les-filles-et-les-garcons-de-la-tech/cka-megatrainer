"""Cluster tests for NET-021..NET-024 (LoadBalancer, EndpointSlice, Gateway HTTPS, Ingress TLS).

The test cluster has the Gateway API v1 CRDs but no Gateway controller, no Ingress controller and
no load balancer provider: the tests check what the API server can prove (objects accepted or
rejected, fields, EndpointSlices, NodePort) plus real traffic where kube-proxy alone suffices.
"""
import tempfile
import time
from pathlib import Path

from lib import Q, WORKER, ClusterTest, answer, final_file, node, sh, t
from lib import run_pod as lib_run_pod


def run_pod(cmd, expect=None):
    return lib_run_pod(cmd, expect=expect, timeout=240)


def trap_file(qid, sid, why_start):
    """The file a player gets by typing the wrong snippet whose 'why' starts with why_start."""
    step = Q[qid]["steps"][sid]
    opt = next(o for o in step["options"] if not o.get("correct") and o["why"].startswith(why_start))
    return t.apply_snippet(step, t.lines_of(opt["snippet"]))


def make_cert(d, crt, key, cn):
    res = sh(f"openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN={cn} -keyout {key} -out {crt}", cwd=d)
    assert res, res


class NewNetworking(ClusterTest):
    def test_net021_loadbalancer_pending_but_nodeport_works(self):
        self.cleanup_cmd("kubectl delete svc shop-lb -n shop; kubectl delete deploy shop-api -n shop")
        self.ok("kubectl create deployment shop-api --image=busybox:1.36 -n shop -- httpd -f -p 8080 -h /etc")
        self.ok("kubectl wait --for=condition=Available deploy/shop-api -n shop --timeout=240s", timeout=270)
        self.answer_ok("NET-021", "s1")
        spec = self.ok("kubectl get svc shop-lb -n shop -o jsonpath='{.spec.type} {.spec.ports[0].port} "
                       "{.spec.ports[0].targetPort} {.spec.selector}'").out
        self.assertIn("LoadBalancer 80 8080", spec)
        self.assertIn('"app":"shop-api"', spec)                             # expose copied the selector
        trap = self.ok("kubectl create service loadbalancer shop-lb --tcp=80:8080 -n shop --dry-run=client -o yaml").out
        self.assertIn("app: shop-lb", trap)                                  # why: create service invents app=<name>
        time.sleep(10)
        self.assertIn("<pending>", self.ok("kubectl get svc shop-lb -n shop").out)   # no LB provider in kind
        desc = self.answer_ok("NET-021", "s2").out
        self.assertIn("NodePort:", desc)
        self.assertNotIn("EnsuredLoadBalancer", desc)
        self.assertEqual(sh(answer("NET-021", "s3").replace(
            "{.spec.ports[0].nodePort}", "{.status.loadBalancer.ingress[0].ip}")).out.strip("'"), "")
        port = self.answer_ok("NET-021", "s3").out.strip("'")
        self.assertRegex(port, r"^3\d{4}$")
        ip = self.ok(f"kubectl get node {WORKER} -o jsonpath='{{.status.addresses[?(@.type==\"InternalIP\")].address}}'"
                     ).out.strip("'")
        body = ""
        for _ in range(15):                                                  # explain: the NodePort works already
            body = node(f"curl -s -m 3 http://{ip}:{port}/hostname").out
            if "shop-api" in body:
                break
            time.sleep(2)
        self.assertIn("shop-api", body)

    def test_net022_service_without_selector_and_manual_endpointslice(self):
        self.cleanup_cmd("kubectl delete endpointslice legacy-db-1 -n shop; kubectl delete svc legacy-db -n shop; "
                         "kubectl delete pod pg -n shop --wait=false")
        with tempfile.TemporaryDirectory() as d:
            self.answer_ok("NET-022", "s1", cwd=d)
            generated = Path(d, "svc.yaml").read_text()
        # the prefill of s2 is what kubectl really generates (the target block is in it)
        self.assertGreaterEqual(t.find_block(generated.split("\n"), t.lines_of(Q["NET-022"]["steps"]["s2"]["target"])), 0,
                                generated)
        self.apply(final_file("NET-022", "s2"))
        self.assertEqual(self.ok("kubectl get svc legacy-db -n shop -o jsonpath='{.spec.selector}'").out.strip("'"), "")
        self.ok("kubectl apply --dry-run=server -f -", stdin=trap_file("NET-022", "s3", "app=legacy-db"))
        self.fails("kubectl apply --dry-run=server -f -", stdin=trap_file("NET-022", "s3", "addresses holds bare IPs"))
        self.apply(final_file("NET-022", "s3"))
        out = self.answer_ok("NET-022", "s4").out
        self.assertIn("legacy-db-1", out)
        self.assertIn("10.20.30.40", out)
        self.assertIn("5432", out)
        self.fails(answer("NET-022", "s4").replace("kubernetes.io/service-name", "app")+
                   " -o name | grep .")                                     # trap: no slice carries app=legacy-db
        self.fails("kubectl get endpoints legacy-db -n shop")               # trap: no Endpoints object
        # the mechanism, with a reachable address: point the same slice at a Pod that answers on 5432
        self.ok("kubectl run pg -n shop --image=busybox:1.36 -- sh -c 'while true; do echo hi | nc -l -p 5432; done'")
        self.ok("kubectl wait --for=condition=Ready pod/pg -n shop --timeout=240s", timeout=270)
        pod_ip = self.ok("kubectl get pod pg -n shop -o jsonpath='{.status.podIP}'").out.strip("'")
        self.ok("kubectl apply -f -", stdin=final_file("NET-022", "s3").replace("10.20.30.40", pod_ip))
        time.sleep(5)
        res = run_pod("kubectl run t --rm -i --restart=Never --image=busybox:1.36 -n shop -- "
                      "sh -c 'nc -w 3 legacy-db 5432 </dev/null'", expect="hi")
        self.assertIn("hi", res.out)

    def test_net023_https_gateway_and_cross_namespace_route(self):
        self.cleanup_cmd("kubectl delete httproute shop-route -n shop; kubectl delete gateway secure-gw -n infra; "
                         "kubectl delete secret shop-tls -n infra")
        with tempfile.TemporaryDirectory() as d:
            make_cert(d, "tls.crt", "tls.key", "shop.example.com")
            self.fails("kubectl create secret tls shop-tls --cert=tls.key --key=tls.crt -n infra --dry-run=client",
                       cwd=d)                                                # trap: swapped pair is refused
            self.fails("kubectl create secret tls shop-tls --from-file=tls.crt --from-file=tls.key -n infra "
                       "--dry-run=client", "unknown flag", cwd=d)
            self.answer_ok("NET-023", "s1", cwd=d)
        self.assertEqual(self.ok("kubectl get secret shop-tls -n infra -o jsonpath='{.type}'").out.strip("'"),
                         "kubernetes.io/tls")
        self.ok("kubectl apply --dry-run=server -f -", stdin=final_file("NET-023", "s2"))
        self.fails("kubectl apply --dry-run=server -f -", stdin=trap_file("NET-023", "s2", "Passthrough"),
                   expect="Terminate")
        self.fails("kubectl apply --dry-run=server -f -", stdin=trap_file("NET-023", "s2", "secretName"))
        self.apply(final_file("NET-023", "s3"))
        got = self.ok("kubectl get gateway secure-gw -n infra -o jsonpath='{.spec.listeners[0].tls.mode} "
                      "{.spec.listeners[0].tls.certificateRefs[0].name} {.spec.listeners[0].allowedRoutes.namespaces.from} "
                      "{.spec.listeners[0].allowedRoutes.namespaces.selector.matchLabels}'").out
        self.assertIn("Terminate shop-tls Selector", got)
        self.assertIn('"kubernetes.io/metadata.name":"shop"', got)
        self.assertIn("kubernetes.io/metadata.name=shop", self.ok("kubectl get ns shop --show-labels").out)
        self.apply(final_file("NET-023", "s4"))
        ref = self.ok("kubectl get httproute shop-route -n shop -o jsonpath='{.spec.parentRefs[0].name} "
                      "{.spec.parentRefs[0].namespace} {.spec.parentRefs[0].sectionName}'").out
        self.assertIn("secure-gw infra https", ref)
        # trap 'infra/secure-gw' is accepted by the API (no pattern on names): it just points nowhere
        self.ok("kubectl apply --dry-run=server -f -", stdin=trap_file("NET-023", "s4", "parentRefs has no"))
        out = self.answer_ok("NET-023", "s5").out
        self.assertIn("secure-gw", out)
        self.assertIn("https", out)
        self.assertIn("shop-tls", out)

    def test_net024_ingress_tls(self):
        self.cleanup_cmd("kubectl delete ingress store -n store; kubectl delete secret store-tls -n store")
        with tempfile.TemporaryDirectory() as d:
            make_cert(d, "store.crt", "store.key", "store.example.com")
            self.answer_ok("NET-024", "s1", cwd=d)
            generic = self.ok("kubectl create secret generic store-generic --from-file=store.crt --from-file=store.key "
                              "-n store --dry-run=server -o yaml", cwd=d).out
        self.assertIn("store.crt:", generic)                                 # trap: wrong key names, type Opaque
        self.assertIn("type: Opaque", generic)                               # defaulted by the API server
        sec = self.ok("kubectl get secret store-tls -n store -o jsonpath='{.type} {.data}'").out
        self.assertIn("kubernetes.io/tls", sec)
        self.assertIn('"tls.crt"', sec)
        self.fails("kubectl apply --dry-run=server -f -", stdin=trap_file("NET-024", "s2", "spec.tls is a list"))
        self.fails("kubectl apply --dry-run=server -f -", stdin=trap_file("NET-024", "s2", "certificateRefs"))
        self.apply(final_file("NET-024", "s2"))
        out = self.answer_ok("NET-024", "s3").out
        self.assertIn("store-tls terminates store.example.com", out)
        self.assertIn("443", self.ok("kubectl get ingress store -n store").out)   # trap: PORTS 80, 443 only
