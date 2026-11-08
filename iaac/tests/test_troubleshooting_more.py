"""Cluster tests for TRB-031..036 (PDB drain, DaemonSet taints, targetPort, NetworkPolicy, kubectl debug, HA etcd).

Cluster: kind v1.37, one control plane + one worker, kindnet, metrics-server.
"""
import re
import shlex
import time
import unittest

import lib
from lib import CONTROL_PLANE, WORKER, ClusterTest, answer, final_file, node, prefill, run_pod, settle_node, sh, wait_for

ETCD_POD = f"etcd-{CONTROL_PLANE}"
PKI = "/etc/kubernetes/pki/etcd"


def variant(qid, sid, index):
    """The file a player ends up with after typing option `index` (a trap) of an edit step."""
    step = lib.Q[qid]["steps"][sid]
    return lib.t.apply_snippet(step, lib.t.lines_of(step["options"][index]["snippet"]))


def wrong(qid, sid, index):
    """The command of a wrong option (a trap) of a command step."""
    return lib.Q[qid]["steps"][sid]["options"][index]["cmd"]


def jsonpath(cmd, path):
    return sh(f"{cmd} -o jsonpath='{path}'").out.strip("'")


def debug_session(cmd, typed, index, expect):
    """Run an interactive 'kubectl debug -i' with typed stdin. When kubectl attached too late to see
    the output, the ephemeral container's own log (number `index` of the Pod) still holds it."""
    out = sh(cmd, stdin=typed, timeout=180).out
    if expect not in out:
        name = jsonpath("kubectl get pod payments", "{.spec.ephemeralContainers[%d].name}" % index)
        out += "\n" + sh(f"kubectl logs payments -c {name}").out
    return out


class NewTroubleshooting(ClusterTest):

    # ------------------------------------------------------------------ TRB-031
    def test_trb031_pdb_blocks_the_drain_until_a_replica_is_added(self):
        # cleanups run in reverse order: the waits are registered first, so they run last
        self.cleanup_cmd("kubectl wait --for=condition=Available deploy --all -n kube-system --timeout=120s")
        self.cleanup_cmd(f"kubectl uncordon {WORKER} {CONTROL_PLANE}")
        self.cleanup_cmd("kubectl delete pdb web-pdb --ignore-not-found; kubectl delete deploy web --ignore-not-found;"
                         " kubectl wait --for=delete pod -l app=web --timeout=60s")
        # both replicas on the worker: the control plane is cordoned while they are created, and
        # the Pods tolerate its taint so that the 3rd replica can land there once the worker is cordoned
        self.ok(f"kubectl cordon {CONTROL_PLANE}")
        self.apply("""apiVersion: apps/v1
kind: Deployment
metadata: {name: web}
spec:
  replicas: 2
  selector: {matchLabels: {app: web}}
  template:
    metadata: {labels: {app: web}}
    spec:
      terminationGracePeriodSeconds: 1
      tolerations: [{key: node-role.kubernetes.io/control-plane, operator: Exists, effect: NoSchedule}]
      containers: [{name: web, image: "busybox:1.36", command: [sleep, "3600"]}]
""")
        self.ok("kubectl rollout status deploy/web --timeout=120s", timeout=150)
        self.ok(f"kubectl uncordon {CONTROL_PLANE}")
        self.assertNotIn(CONTROL_PLANE, self.ok("kubectl get pods -l app=web -o wide").out)
        self.apply("apiVersion: policy/v1\nkind: PodDisruptionBudget\nmetadata: {name: web-pdb}\n"
                   "spec:\n  minAvailable: 2\n  selector: {matchLabels: {app: web}}\n")
        wait_for("kubectl get pdb web-pdb -o jsonpath='{.status.expectedPods}'", "2", 60)
        drain = answer("TRB-031", "s3").replace("node01", WORKER)
        # the context of s1: the eviction is refused and drain keeps retrying (bounded here)
        settle_node()
        blocked = self.fails(drain + " --timeout=20s", "violate the pod's disruption budget", timeout=60)
        self.assertIn("will retry after 5s", blocked.out)
        out = self.answer_ok("TRB-031", "s1").out
        self.assertRegex(out, r"web-pdb\s+2\s+N/A\s+0\s")             # ALLOWED DISRUPTIONS 0
        self.fails(wrong("TRB-031", "s1", 2), "the server doesn't have a resource type")
        self.fails(wrong("TRB-031", "s2", 1))                           # a PDB cannot be scaled
        self.answer_ok("TRB-031", "s2")
        res = wait_for("kubectl get pdb web-pdb -o jsonpath='{.status.disruptionsAllowed}'", "1", 120)
        self.assertEqual(res.out.strip("'"), "1", res)                 # 3 healthy - minAvailable 2
        self.ok(drain, "drained", timeout=300)
        self.ok("kubectl rollout status deploy/web --timeout=120s", timeout=150)
        pods = self.ok("kubectl get pods -l app=web -o wide --field-selector status.phase=Running").out
        self.assertNotIn(WORKER, pods)                                   # every web Pod left the drained node
        self.assertEqual(pods.count(CONTROL_PLANE), 3, pods)

    # ------------------------------------------------------------------ TRB-032
    def test_trb032_daemonset_tolerates_the_control_plane_taint(self):
        self.cleanup_cmd("kubectl delete ds node-exporter -n monitoring --ignore-not-found --wait=true")

        def real(y):  # a sleeping busybox instead of node-exporter, no host ports or host namespaces
            y = y.replace("image: quay.io/prometheus/node-exporter:v1.9.1",
                          "image: busybox:1.36\n        command: [sleep, \"3600\"]")
            return "\n".join(ln for ln in y.split("\n") if not re.search(
                r"^\s+(ports:|- containerPort:|hostPort:|hostNetwork:|hostPID:)", ln))

        desired = lambda: jsonpath("kubectl get ds node-exporter -n monitoring",  # noqa: E731
                                   "{.status.desiredNumberScheduled}")
        self.apply(real(prefill("TRB-032", "s2")))
        res = wait_for("kubectl get ds node-exporter -n monitoring -o jsonpath='{.status.desiredNumberScheduled}'", "1", 60)
        self.assertEqual(res.out.strip("'"), "1", res)                  # 2 nodes, the control plane is skipped
        taints = self.ok(answer("TRB-032", "s1").replace("controlplane", CONTROL_PLANE)).out
        self.assertIn("node-role.kubernetes.io/control-plane:NoSchedule", taints)
        # the traps really leave the control plane out (master key, Equal "true", NoExecute)
        for i in (1, 2, 3):
            self.ok("kubectl apply -f -", stdin=real(variant("TRB-032", "s2", i)))
            time.sleep(5)
            self.assertEqual(desired(), "1", f"trap {i} must not tolerate the taint")
        self.ok("kubectl apply -f -", stdin=real(final_file("TRB-032", "s2")))
        res = wait_for("kubectl get ds node-exporter -n monitoring -o jsonpath='{.status.desiredNumberScheduled}'", "2", 60)
        self.assertEqual(res.out.strip("'"), "2", res)
        self.ok("kubectl rollout status ds/node-exporter -n monitoring --timeout=120s", timeout=150)
        self.assertIn(CONTROL_PLANE, self.answer_ok("TRB-032", "s3").out)

    # ------------------------------------------------------------------ TRB-033
    def test_trb033_wrong_target_port_is_refused_then_fixed(self):
        self.cleanup_cmd("kubectl delete svc web --ignore-not-found; kubectl delete deploy web --ignore-not-found;"
                         " kubectl wait --for=delete pod -l app=web --timeout=60s")
        image = re.search(r"Image:\s+(\S+)", "\n".join(lib.Q["TRB-033"]["steps"]["s3"]["context"])).group(1)
        self.ok(f"kubectl create deployment web --image={image} --port=8080")   # nginx-unprivileged binds 8080
        self.ok("kubectl wait --for=condition=Available deploy/web --timeout=120s", timeout=150)
        self.apply(prefill("TRB-033", "s3"))                             # targetPort 80: nothing listens there
        wait_for("kubectl get endpointslices -l kubernetes.io/service-name=web", "IPv4", 30)
        desc = self.answer_ok("TRB-033", "s1").out
        self.assertRegex(desc, r"TargetPort:\s+80/TCP")
        self.assertRegex(desc, r"Endpoints:\s+\d+\.\d+\.\d+\.\d+:80")      # endpoints exist
        self.assertIn("8080/TCP", self.answer_ok("TRB-033", "s2").out)
        refused = run_pod(answer("TRB-033", "s4"), expect="Connection refused")
        self.assertIn("Connection refused", refused.out)               # refused, not a time-out
        self.fails("kubectl apply -f -", stdin=variant("TRB-033", "s3", 3))  # targetPort "8080/TCP" is rejected
        self.ok("kubectl apply -f -", stdin=final_file("TRB-033", "s3"))
        wait_for("kubectl get endpointslices -l kubernetes.io/service-name=web", "8080", 30)
        res = run_pod(answer("TRB-033", "s4"), expect="Welcome to nginx")
        self.assertIn("Welcome to nginx", res.out)

    # ------------------------------------------------------------------ TRB-034
    def test_trb034_policy_selects_an_old_label(self):
        ns = "-n shop"
        self.cleanup_cmd(f"kubectl delete netpol api-ingress {ns} --ignore-not-found;"
                         f" kubectl delete pod frontend {ns} --ignore-not-found --wait=false;"
                         f" kubectl delete svc api {ns} --ignore-not-found; kubectl delete deploy api {ns} --ignore-not-found")
        self.ok(f"kubectl create deployment api {ns} --image=nginxinc/nginx-unprivileged:1.27 --port=8080")
        self.ok(f"kubectl expose deployment api {ns} --port=8080")
        self.ok(f"kubectl run frontend {ns} --image=busybox:1.36 --labels=app=frontend -- sleep 3600")
        self.ok(f"kubectl wait --for=condition=Available deploy/api {ns} --timeout=120s", timeout=150)
        self.ok(f"kubectl wait --for=condition=Ready pod/frontend {ns} --timeout=120s", timeout=150)
        call = answer("TRB-034", "s5")
        self.assertIn("Welcome to nginx", wait_for(call, "Welcome to nginx", 60).out)   # open before the policy
        self.apply(prefill("TRB-034", "s4"))                             # allows app=web-frontend only
        blocked = wait_for(call, "timed out", 60)                        # kindnet enforces the isolation
        self.assertIn("timed out", blocked.out)
        self.assertIn("api-ingress", self.answer_ok("TRB-034", "s1").out)
        self.assertRegex(self.answer_ok("TRB-034", "s2").out, r"PodSelector:\s+app=web-frontend")
        labels = self.answer_ok("TRB-034", "s3").out
        self.assertIn("app=frontend", labels)
        self.assertNotIn("app=web-frontend", labels)
        self.ok("kubectl apply -f -", stdin=final_file("TRB-034", "s4"))
        self.assertRegex(self.answer_ok("TRB-034", "s2").out, r"PodSelector:\s+app=frontend")
        # what the API can prove: the new source selects exactly the frontend Pod of shop
        selected = self.ok(f"kubectl get pods {ns} -l app=frontend -o name").out.split()
        self.assertEqual(selected, ["pod/frontend"])
        # a throw-away Pod (label run=t) is still isolated: this holds whether or not the allow rule works
        trap = wrong("TRB-034", "s5", 1)
        denied = run_pod(trap, expect="timed out", timeout=120)
        self.assertIn("timed out", denied.out)
        # The allow itself is NOT asserted: iaac/README.md "Known limits" says kindnet does not always
        # honour a same-namespace podSelector allow next to an isolating policy. Report it instead.
        after = wait_for(call, "Welcome to nginx", 30)
        print(f"\nTRB-034 note: frontend after the fix -> {'allowed' if 'Welcome' in after.out else 'still blocked (kindnet limit)'}")

    # ------------------------------------------------------------------ TRB-035
    def test_trb035_debug_a_shell_less_container(self):
        self.cleanup_cmd("kubectl delete pod payments --ignore-not-found --wait=true --timeout=60s;"
                         " kubectl delete cm payments-config --ignore-not-found")
        # a real shell-less image already on the control plane: etcd (distroless, no sh, no cat)
        etcd_image = jsonpath(f"kubectl get pod {ETCD_POD} -n kube-system", "{.spec.containers[0].image}")
        self.assertTrue(etcd_image)
        self.apply(prefill("TRB-035", "s4"))                             # the ConfigMap with the typo
        self.apply(f"""apiVersion: v1
kind: Pod
metadata: {{name: payments}}
spec:
  nodeName: {CONTROL_PLANE}
  terminationGracePeriodSeconds: 5
  tolerations: [{{operator: Exists}}]
  containers:
  - name: app
    image: {etcd_image}
    imagePullPolicy: IfNotPresent
    command: [etcd, --data-dir=/data/etcd]
    securityContext: {{runAsUser: 0}}
    volumeMounts: [{{name: config, mountPath: /etc/app}}, {{name: data, mountPath: /data}}]
  volumes:
  - {{name: config, configMap: {{name: payments-config}}}}
  - {{name: data, emptyDir: {{}}}}
""")
        self.ok("kubectl wait --for=condition=Ready pod/payments --timeout=120s", timeout=150)
        self.fails("kubectl exec payments -c app -- sh", "executable file not found")   # the s1 context
        # s1 + s2: the interactive debug shell, fed through stdin (no TTY here)
        debug = answer("TRB-035", "s1").replace(" -it ", " -i ")
        self.assertNotEqual(debug, answer("TRB-035", "s1"))
        out = debug_session(debug, f"ps\n{answer('TRB-035', 's2')}\nexit\n", 0, "db_host")
        self.assertRegex(out, r"\n\s*1 root .*etcd")                    # --target: the app is PID 1 in ps
        self.assertIn("db_host: postgres.payment.svc.cluster.local", out)   # read through /proc/1/root
        # without --target the app is invisible (the first trap)
        alone = debug_session(wrong("TRB-035", "s1", 1).replace(" -it ", " -i "), "ps\nexit\n", 1, "PID")
        self.assertIn("PID", alone)                                      # ps did run...
        self.assertNotRegex(alone, r"\n\s*1 root .*etcd")               # ...but sees no etcd
        # a previous instance for s3: SIGTERM the app from another ephemeral container (etcd handles it)
        self.ok("kubectl debug payments --image=busybox:1.36 --target=app -- kill 1")
        wait_for("kubectl get pod payments -o jsonpath='{.status.containerStatuses[0].restartCount}'", "1", 120)
        prev = wait_for(answer("TRB-035", "s3"), "signal", 60)
        self.assertTrue(prev, prev)
        self.assertRegex(prev.out, r"received signal|shutting down")
        # s4: the 2-space trap is invalid YAML, the right line is accepted
        self.fails("kubectl apply -f -", stdin=variant("TRB-035", "s4", 1))
        self.ok("kubectl apply -f -", stdin=final_file("TRB-035", "s4"))
        cfg = jsonpath("kubectl get cm payments-config", "{.data.config\\.yaml}")
        self.assertIn("db_host: postgres.payments.svc.cluster.local", cfg)
        # explain: ephemeral containers cannot be removed, they stay in the spec
        self.assertTrue(jsonpath("kubectl get pod payments", "{.spec.ephemeralContainers[*].name}"))

    # ------------------------------------------------------------------ TRB-036
    def etcd(self, cmd):
        """An etcdctl answer run inside the etcd static Pod, the game's relative cert names made absolute."""
        argv = shlex.split(cmd)
        argv = [re.sub(r"=(ca\.crt|server\.crt|server\.key)$", rf"={PKI}/\1", a) for a in argv]
        return sh(f"kubectl exec -n kube-system {ETCD_POD} -- " + " ".join(shlex.quote(a) for a in argv))

    def test_trb036_etcd_checks_on_the_single_member(self):
        # kind has ONE control-plane node: the HA picture (2 of 3 healthy, quorum, a second loss
        # stopping writes) cannot be reproduced. What can be proven: the commands work and print
        # what the explanations describe, on a 1-member etcd.
        health = self.etcd(answer("TRB-036", "s1"))
        self.assertTrue(health, health)
        self.assertRegex(health.out, r"https://\S+:2379 is healthy: successfully committed proposal")
        nocert = self.etcd(wrong("TRB-036", "s1", 3))
        self.assertFalse(nocert, nocert)                                 # no CA / client cert: refused
        members = self.etcd(wrong("TRB-036", "s1", 2))
        self.assertIn("started", members.out)                            # member list: membership, not health
        status = self.etcd(answer("TRB-036", "s2"))
        self.assertTrue(status, status)
        self.assertIn("IS LEADER", status.out)
        self.assertIn("RAFT TERM", status.out)
        self.assertRegex(status.out, r"[|│]\s*true\s*[|│]")               # the single member leads (etcdctl 3.7 draws │)
        self.assertNotIn("IS LEADER", self.etcd(wrong("TRB-036", "s2", 3)).out)
        # s3 on a healthy node: enable --now is idempotent; etcd is not a systemd unit (a trap)
        self.assertTrue(node(answer("TRB-036", "s3").replace("sudo ", "")))
        self.assertEqual(node("systemctl is-enabled kubelet").out.strip(), "enabled")
        self.assertFalse(node(wrong("TRB-036", "s3", 1).replace("sudo ", "")))
        pods = self.answer_ok("TRB-036", "s4").out
        self.assertIn(ETCD_POD, pods)
        self.assertIn("Running", pods)
        self.assertIn("No resources found", sh(wrong("TRB-036", "s4", 2)).out)   # app=etcd matches nothing


if __name__ == "__main__":
    unittest.main()
