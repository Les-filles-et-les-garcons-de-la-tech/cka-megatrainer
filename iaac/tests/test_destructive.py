"""Break a control-plane component, then run the game's correct answers to find and fix it.

These tests really break the cluster (API server, scheduler, controller manager, kubelet,
kube-proxy, certificates) and repair it afterwards, so they are opt-in:

    DESTRUCTIVE=1 iaac/run-tests.sh test_destructive

Each test restores what it touched, even when it fails. If a run is killed half-way, reset with
`iaac/cluster.sh down && iaac/cluster.sh up`.
"""
import json
import os
import re
import shlex
import time
import unittest

from lib import CONTROL_PLANE, WORKER, ClusterTest, answer, final_file, node, sh, wait_for

MANIFESTS = "/etc/kubernetes/manifests"


def api_up():
    return bool(sh("kubectl get --raw /readyz", timeout=15))


def wait_api(up, timeout=240):
    end = time.time() + timeout
    while time.time() < end:
        if api_up() == up:
            return True
        time.sleep(3)
    return False


@unittest.skipUnless(os.environ.get("DESTRUCTIVE"), "set DESTRUCTIVE=1: these tests break and repair the cluster")
class StaticPodBreakFix(ClusterTest):
    def break_manifest(self, name, old, new):
        """Edit a static Pod manifest of the control plane; the original is restored at the end."""
        path = f"{MANIFESTS}/{name}.yaml"
        self.assertTrue(node(f"cp {path} /root/{name}.orig"))
        self.addCleanup(self.restore_manifest, name)
        res = node(f"sed -i 's#{old}#{new}#' {path} && grep -c -- '{new}' {path}")
        self.assertTrue(res, f"could not break {name}: {res}")

    def restore_manifest(self, name):
        node(f"cp /root/{name}.orig {MANIFESTS}/{name}.yaml")
        wait_api(True, 300)

    def container_id(self, name):
        return node(f"crictl ps -a --name {name} -q | head -1").out.strip()

    def test_trb018_apiserver_with_a_misspelled_flag(self):
        self.break_manifest("kube-apiserver", "--advertise-address", "--advertise-adress")
        self.assertTrue(wait_api(False, 180), "the API server should be down")
        listing = node(answer("TRB-018", "s1").replace("sudo ", "")).out          # kubectl is dead, crictl is not
        self.assertIn("kube-apiserver", listing)
        self.assertIn("Exited", wait_for_node("crictl ps -a --name kube-apiserver", "Exited", 120))
        cid = self.container_id("kube-apiserver")
        logs = node(answer("TRB-018", "s2").replace("sudo ", "").replace("3f2a1b9c8d7e", cid[:12]))
        self.assertIn("unknown flag: --advertise-adress", logs.out)
        self.assertIn("--advertise-address=", final_file("TRB-018", "s3"))         # the game's fixed line
        self.restore_manifest("kube-apiserver")
        self.assertTrue(api_up(), "the API server did not come back")

    def test_trb019_scheduler_that_cannot_load_its_kubeconfig(self):
        self.cleanup_cmd("kubectl delete deploy sched-test")
        self.break_manifest("kube-scheduler", "--kubeconfig=/etc/kubernetes/scheduler.conf",
                            "--kubeconfig=/etc/kubernetes/scheduler.cnf")
        pods = self.ok(answer("TRB-019", "s1")).out                                # the mirror Pod is listed
        self.assertIn(f"kube-scheduler-{CONTROL_PLANE}", pods)
        res = wait_for(f"kubectl get pods -n kube-system kube-scheduler-{CONTROL_PLANE}", "Error", 150)
        self.assertRegex(res.out, r"Error|CrashLoopBackOff")
        self.ok("kubectl create deployment sched-test --image=nginx:1.26")
        wait_for("kubectl get pods -l app=sched-test -o name", "pod/", 60)
        time.sleep(10)
        pod = sh("kubectl get pods -l app=sched-test -o jsonpath='{.items[0].metadata.name}'").out.strip("'")
        self.assertIn("Pending", self.ok(f"kubectl get pod {pod}").out)
        desc = self.ok(f"kubectl describe pod {pod}").out
        self.assertNotIn("FailedScheduling", desc)                                 # explain: NO events at all
        log = sh(answer("TRB-019", "s2").replace("kube-scheduler-controlplane", f"kube-scheduler-{CONTROL_PLANE}")
                 + " --previous")
        self.assertIn("scheduler.cnf", log.out)
        self.restore_manifest("kube-scheduler")
        res = wait_for(f"kubectl get pod {pod}", "Running", 150)
        self.assertIn("Running", res.out)

    def test_trb020_controller_manager_with_a_broken_ca_path(self):
        self.cleanup_cmd("kubectl delete deploy cm-test")
        self.break_manifest("kube-controller-manager", "--client-ca-file=/etc/kubernetes/pki/ca.crt",
                            "--client-ca-file=/etc/kubernetes/pki/ca.crt.missing")
        res = wait_for(answer("TRB-020", "s1"), "Error", 150)
        self.assertRegex(res.out, r"Error|CrashLoopBackOff")                       # selected by component=<name>
        self.ok("kubectl create deployment cm-test --image=nginx:1.26")
        time.sleep(10)
        self.assertNotIn("cm-test-", self.ok("kubectl get rs -l app=cm-test --no-headers").out)  # nobody makes the ReplicaSet
        log = self.ok(answer("TRB-020", "s2").replace("kube-controller-manager-controlplane",
                                                       f"kube-controller-manager-{CONTROL_PLANE}"))
        self.assertIn("unable to load client CA provider", log.out)              # names the file it cannot open
        self.assertIn("/etc/kubernetes/pki/ca.crt.missing", log.out)
        self.assertIn("--client-ca-file=/etc/kubernetes/pki/ca.crt", final_file("TRB-020", "s3"))
        self.restore_manifest("kube-controller-manager")
        res = wait_for("kubectl get rs -l app=cm-test", "cm-test", 150)
        self.assertIn("cm-test", res.out)

    def test_trb021_apiserver_pointing_at_the_wrong_etcd_port(self):
        self.break_manifest("kube-apiserver", "--etcd-servers=https://127.0.0.1:2379",
                            "--etcd-servers=https://127.0.0.1:2399")
        self.assertTrue(wait_api(False, 240))
        logs = None
        for _ in range(40):                                                        # the newest container may have just started
            for cid in node("crictl ps -a --name kube-apiserver -q").out.split():
                logs = node(answer("TRB-021", "s1").replace("sudo ", "").replace("8c1e2f4a9b3d", cid[:12]))
                if "2399" in logs.out:
                    break
            else:
                time.sleep(3)
                continue
            break
        self.assertIn("2399", logs.out)                                            # the log names the bad endpoint
        self.assertIn("connection refused", logs.out)
        listen = node(answer("TRB-021", "s2").replace("sudo ", "")).out
        self.assertIn("https://127.0.0.1:2379", listen)                            # what etcd really listens on
        self.assertIn("--etcd-servers=https://127.0.0.1:2379", final_file("TRB-021", "s3"))
        self.restore_manifest("kube-apiserver")


def wait_for_node(cmd, expect, timeout):
    end, out = time.time() + timeout, ""
    while time.time() < end:
        out = node(cmd).out
        if expect in out:
            return out
        time.sleep(3)
    return out


@unittest.skipUnless(os.environ.get("DESTRUCTIVE"), "set DESTRUCTIVE=1: these tests break and repair the cluster")
class KubeletAndProxy(ClusterTest):
    def test_trb022_kubelet_with_a_missing_config_file(self):
        self.cleanup_cmd(f"podman exec {WORKER} sh -c 'systemctl daemon-reload; systemctl restart kubelet'")
        unit = node(answer("TRB-022", "s2").replace("sudo ", ""), which=WORKER).out      # prints unit + drop-ins
        path = re.search(r"# (/\S*10-kubeadm\.conf)", unit)
        self.assertTrue(path, unit)
        path = path.group(1)
        self.addCleanup(lambda: node(f"cp /root/10-kubeadm.conf.orig {path}; systemctl daemon-reload; systemctl restart kubelet",
                                     which=WORKER))
        node(f"cp {path} /root/10-kubeadm.conf.orig", which=WORKER)
        self.assertIn("--config=/var/lib/kubelet/config.yaml", unit)
        node(f"sed -i 's#--config=/var/lib/kubelet/config.yaml#--config=/var/lib/kubelet/missing.yaml#' {path}", which=WORKER)
        self.assertTrue(node(answer("TRB-022", "s4").replace("sudo ", ""), which=WORKER))
        time.sleep(15)
        journal = node(answer("TRB-022", "s1").replace("sudo ", "") + " --no-pager | tail -40", which=WORKER).out
        self.assertIn("missing.yaml", journal)                                      # the log names the file
        self.assertIn('KUBELET_CONFIG_ARGS=--config=/var/lib/kubelet/config.yaml', final_file("TRB-022", "s3"))
        node(f"cp /root/10-kubeadm.conf.orig {path}", which=WORKER)
        self.assertTrue(node(answer("TRB-022", "s4").replace("sudo ", ""), which=WORKER))
        res = wait_for(f"kubectl get node {WORKER}", " Ready", 150, every=5)
        self.assertNotIn("NotReady", res.out)

    def test_trb028_kube_proxy_with_a_bad_mode(self):
        cm = json.loads(self.ok("kubectl get cm kube-proxy -n kube-system -o json").out)
        original = cm["data"]["config.conf"]
        self.assertRegex(original, r"mode: \S*")
        restore = lambda: sh("kubectl -n kube-system patch cm kube-proxy --type=merge -p "  # noqa: E731
                             + shlex.quote(json.dumps({"data": {"config.conf": original}})))
        self.addCleanup(lambda: (restore(), sh("kubectl rollout restart ds kube-proxy -n kube-system")))
        broken = re.sub(r"mode: \S*", "mode: iptabels", original)
        self.ok("kubectl -n kube-system patch cm kube-proxy --type=merge -p "
                + shlex.quote(json.dumps({"data": {"config.conf": broken}})))
        self.answer_ok("TRB-028", "s4")                                              # restart: pods read the bad config
        res = wait_for(answer("TRB-028", "s1"), "Error", 150)
        self.assertRegex(res.out, r"Error|CrashLoopBackOff")
        log = None
        for _ in range(45):
            names = sh("kubectl get pods -n kube-system -l k8s-app=kube-proxy -o name").out.split()
            for name in names:
                log = sh(re.sub(r"kube-proxy-[a-z0-9]+", name.split("/")[1], answer("TRB-028", "s2")))
                if "iptabels" in log.out:
                    break
            else:
                time.sleep(4)
                continue
            break
        self.assertIn("iptabels", log.out)
        self.assertIn('mode: "iptables"', final_file("TRB-028", "s3"))
        restore()
        self.answer_ok("TRB-028", "s4")
        res = wait_for("kubectl get pods -n kube-system -l k8s-app=kube-proxy", "Running", 150)
        self.assertIn("Running", res.out)


@unittest.skipUnless(os.environ.get("DESTRUCTIVE"), "set DESTRUCTIVE=1: these tests break and repair the cluster")
class Certificates(ClusterTest):
    def test_clu011_trb025_renew_all_certificates_then_restart_the_control_plane(self):
        table = node(answer("TRB-025", "s1")).out
        self.assertIn("apiserver", table)
        node("mkdir -p /root/sp /root/.kube")
        self.addCleanup(lambda: node(f"mv /root/sp/*.yaml {MANIFESTS}/ 2>/dev/null; true"))
        res = node(answer("TRB-025", "s2"))
        self.assertTrue(res, res)
        self.assertIn("renewed", res.out)
        self.assertIn("restart", res.out.lower())                                    # the output tells you to restart
        out = node(answer("TRB-025", "s3"))                                           # manifests out of the way
        self.assertTrue(out, out)
        self.assertTrue(wait_api(False, 120), "the control plane should stop")
        time.sleep(20)                                                                # as the question says: ~20 s
        self.assertTrue(node(f"mv /root/sp/*.yaml {MANIFESTS}/"))
        self.assertTrue(wait_api(True, 300), "the control plane did not come back with the new certificates")
        self.assertTrue(node(answer("TRB-025", "s4")))                                # re-copy admin.conf
        self.assertTrue(node("test -s /root/.kube/config"))


if __name__ == "__main__":
    unittest.main()
