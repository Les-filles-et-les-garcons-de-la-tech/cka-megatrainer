"""Cluster Architecture, Installation & Configuration (CLU-*): the correct answers, run for real."""
import shlex
import time
import shutil
import tempfile
import unittest
from pathlib import Path

from lib import CONTROL_PLANE, ENV, WORKER, ClusterTest, answer, final_file, node, sh, wait_for

ETCD_POD = f"etcd-{CONTROL_PLANE}"
PKI = "/etc/kubernetes/pki/etcd"


class RBAC(ClusterTest):
    def test_clu001_role_and_binding_for_one_namespace(self):
        self.cleanup_cmd("kubectl delete role pod-reader -n dev; kubectl delete rolebinding read-pods -n dev")
        self.answer_ok("CLU-001", "s1")
        self.answer_ok("CLU-001", "s2")
        self.assertEqual(self.answer_ok("CLU-001", "s3").out, "yes")
        self.fails("kubectl auth can-i list pods --as=jane -n default", "no")  # a Role stays in its namespace

    def test_clu003_clusterrole_for_a_group(self):
        self.cleanup_cmd("kubectl delete clusterrole node-reader; kubectl delete clusterrolebinding auditors-node-reader")
        self.answer_ok("CLU-003", "s1")
        self.answer_ok("CLU-003", "s2")
        self.assertEqual(self.answer_ok("CLU-003", "s3").out.splitlines()[0], "yes")  # a harmless Warning follows
        # explain says --as-group must go with --as
        self.fails("kubectl auth can-i list nodes --as-group=auditors")

    def test_clu004_serviceaccount_with_the_built_in_edit_role(self):
        self.cleanup_cmd("kubectl delete rolebinding deployer-edit -n ci; kubectl delete sa deployer -n ci")
        self.answer_ok("CLU-004", "s1")
        self.assertEqual(sh("kubectl get secrets -n ci -o name").out, "")  # no token Secret since 1.24
        self.ok("kubectl create token deployer -n ci")
        self.answer_ok("CLU-004", "s2")
        self.assertEqual(self.answer_ok("CLU-004", "s3").out, "yes")
        self.fails("kubectl auth can-i create deployments --as=system:serviceaccount:ci:deployer -n default", "no")


class Kubeconfig(ClusterTest):
    def test_clu010_contexts_and_default_namespace(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = f"{d}/config"
            env = dict(ENV, KUBECONFIG=cfg)
            sh(f"kubectl config view --minify --flatten > {cfg}")
            sh("kubectl config rename-context kind-cka-test prod-admin@prod", env=env)
            sh("kubectl config set-context dev-admin@dev --cluster=kind-cka-test --user=kind-cka-test", env=env)
            sh("kubectl config use-context dev-admin@dev", env=env)
            listing = self.ok(answer("CLU-010", "s1"), env=env).out
            self.assertIn("prod-admin@prod", listing)
            self.assertRegex(listing, r"\*\s+dev-admin@dev")  # '*' marks the current one
            self.ok(answer("CLU-010", "s2"), env=env)
            self.ok(answer("CLU-010", "s3"), env=env)
            view = self.ok("kubectl config view --minify", env=env).out
            self.assertIn("namespace: payments", view)
            self.assertIn("current-context: prod-admin@prod", view)


class Helm(ClusterTest):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not shutil.which("helm"):
            raise unittest.SkipTest("helm not installed")
        res = sh("helm repo add podinfo https://stefanprodan.github.io/podinfo && helm repo update", timeout=180)
        if not res:
            raise unittest.SkipTest(f"no network for helm repos\n{res}")

    def test_clu015_add_update_search_versions(self):
        self.answer_ok("CLU-015", "s1")
        self.answer_ok("CLU-015", "s2")
        out = self.answer_ok("CLU-015", "s3", timeout=180).out
        self.assertIn("prometheus-community/kube-state-metrics", out)
        self.assertGreater(len(out.splitlines()), 3)  # --versions lists many
        latest = self.ok("helm search repo prometheus-community/kube-state-metrics").out  # without it: one row
        self.assertEqual(len(latest.splitlines()), 2)

    def test_clu016_install_into_a_new_namespace(self):
        self.cleanup_cmd("helm uninstall web -n frontend; kubectl delete ns frontend --wait=false")
        sh("kubectl delete ns frontend --wait=true --timeout=60s")
        self.answer_ok("CLU-016", "s1", timeout=180)
        self.assertIn("deployed", self.answer_ok("CLU-016", "s2").out)
        res = wait_for("kubectl get pods -n frontend --no-headers", "podinfo", 60)
        self.assertEqual(len(res.out.splitlines()), 2, res.out)

    def test_clu017_upgrade_then_roll_back(self):
        self.cleanup_cmd("helm uninstall api -n prod")
        with tempfile.TemporaryDirectory() as d:
            Path(d, "prod-values.yaml").write_text("replicaCount: 1\n")
            self.ok("helm install api podinfo/podinfo -n prod", timeout=120)
            # revision 2 FAILED, as in the scenario: an image that does not exist plus --wait
            self.fails("helm upgrade api podinfo/podinfo -n prod --set image.tag=doesnotexist --wait --timeout 25s",
                       timeout=120)
            self.answer_ok("CLU-017", "s1", cwd=d, timeout=120)            # revision 3
            history = self.answer_ok("CLU-017", "s2").out
            self.assertRegex(history, r"2\s.*failed")
            self.answer_ok("CLU-017", "s3")                                # roll back to 1
            after = self.ok("helm history api -n prod").out.splitlines()[-1]
            self.assertRegex(after, r"^4\s")                               # a NEW revision (explain says so)
            self.assertIn("Rollback to 1", after)

    def test_clu018_values_file_keys_exist_in_the_chart(self):
        self.cleanup_cmd("helm uninstall shop -n shop")
        with tempfile.TemporaryDirectory() as d:
            self.answer_ok("CLU-018", "s1", cwd=d)
            real = Path(d, "values.yaml").read_text()
            for key in ("replicaCount:", "nodePort:", "type:"):
                self.assertIn(key, real, key)  # the game's file uses real chart keys
            Path(d, "values.yaml").write_text(final_file("CLU-018", "s2"))
            rendered = self.ok("helm template shop podinfo/podinfo -n shop -f values.yaml", cwd=d).out
            self.assertIn("nodePort: 30080", rendered)
            self.assertIn("type: NodePort", rendered)
            self.answer_ok("CLU-018", "s3", cwd=d, timeout=120)
            self.assertIn("30080", self.ok("kubectl get svc -n shop").out)


class Kustomize(ClusterTest):
    BASE = """apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
- deployment.yaml
"""
    DEPLOY = """apiVersion: apps/v1
kind: Deployment
metadata: {name: web}
spec:
  replicas: 1
  selector: {matchLabels: {app: web}}
  template:
    metadata: {labels: {app: web}}
    spec:
      containers:
      - {name: web, image: nginx, ports: [{containerPort: 80}]}
"""

    def tree(self, d):
        shop = Path(d, "shop")
        for sub in ("base", "overlays/prod", "overlays/staging"):
            (shop / sub).mkdir(parents=True)
        (shop / "base/kustomization.yaml").write_text(self.BASE)
        (shop / "base/deployment.yaml").write_text(self.DEPLOY)
        (shop / "overlays/prod/kustomization.yaml").write_text(
            "resources:\n- ../../base\nnamespace: shop\npatches:\n- target: {kind: Deployment, name: web}\n"
            "  patch: |-\n    - {op: replace, path: /spec/replicas, value: 2}\n")
        (shop / "overlays/staging/service.yaml").write_text(
            "apiVersion: v1\nkind: Service\nmetadata: {name: web}\nspec:\n  selector: {app: web}\n"
            "  ports: [{port: 80}]\n")
        (shop / "overlays/staging/kustomization.yaml").write_text(final_file("CLU-020", "s1"))
        return str(shop)

    def test_clu019_render_apply_delete_overlay(self):
        with tempfile.TemporaryDirectory() as d:
            shop = self.tree(d)
            fix = lambda sid: answer("CLU-019", sid).replace("/opt/shop", shop)  # noqa: E731
            rendered = self.ok(fix("s1")).out
            self.assertIn("replicas: 2", rendered)
            self.assertEqual(self.ok("kubectl get deploy -n shop -o name").out.count("web"), 0)  # nothing applied
            self.ok(fix("s2"))
            self.assertIn("web", self.ok("kubectl get deploy -n shop -o name").out)
            self.ok(fix("s3"))
            self.assertNotIn("web", sh("kubectl get deploy -n shop -o name").out)

    def test_clu020_staging_overlay_sets_namespace_and_image(self):
        self.cleanup_cmd("kubectl delete deploy,svc web -n staging")
        with tempfile.TemporaryDirectory() as d:
            shop = self.tree(d)
            fix = lambda sid: answer("CLU-020", sid).replace("/opt/shop", shop)  # noqa: E731
            self.ok(fix("s2"))
            self.assertIn("nginx:1.27", self.ok(fix("s3")).out)
            # explain: quote the tag, a bare 1.27 is a number
            bare = final_file("CLU-020", "s1").replace('"1.27"', "1.27")
            Path(shop, "overlays/staging/kustomization.yaml").write_text(bare)
            self.fails(f"kubectl kustomize {shop}/overlays/staging")


CRD = """apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata: {{name: {plural}.{group}}}
spec:
  group: {group}
  scope: Namespaced
  names: {{plural: {plural}, singular: {singular}, kind: {kind}}}
  versions:
  - name: {version}
    served: true
    storage: true
    schema:
      openAPIV3Schema:
        type: object
        properties:
          spec:
            type: object
            properties: {props}
"""


class CustomResources(ClusterTest):
    def crd(self, **kw):
        self.apply(CRD.format(**kw))
        self.cleanup_cmd(f"kubectl delete crd {kw['plural']}.{kw['group']}")
        self.ok(f"kubectl wait --for=condition=Established crd/{kw['plural']}.{kw['group']} --timeout=30s")

    def test_clu021_create_a_custom_resource_from_its_crd(self):
        self.crd(plural="databases", singular="database", kind="Database", group="db.example.com",
                 version="v1alpha1",
                 props="{engine: {type: string}, backup: {type: object, properties: "
                       "{schedule: {type: string}, retention: {type: integer}}}}")
        self.assertIn("databases.db.example.com", self.answer_ok("CLU-021", "s1").out)
        res = wait_for(answer("CLU-021", "s2"), "schedule", 60)  # the schema is published a moment later
        self.assertIn("schedule", res.out, res)
        with tempfile.TemporaryDirectory() as d:
            Path(d, "db.yaml").write_text(final_file("CLU-021", "s3"))
            self.answer_ok("CLU-021", "s4", cwd=d)
            self.ok("kubectl get databases -n shop", "orders")
            # explain: strict field validation rejects unknown fields
            Path(d, "bad.yaml").write_text(final_file("CLU-021", "s3").replace("retention", "retentio"))
            self.fails("kubectl apply -f bad.yaml", "unknown field", cwd=d)
        self.ok("kubectl api-resources --api-group=db.example.com", "Database")

    def test_clu022_operator_and_its_resource(self):
        self.crd(plural="redisclusters", singular="rediscluster", kind="RedisCluster", group="cache.acme.io",
                 version="v1", props="{replicas: {type: integer}, version: {type: string}}")
        self.cleanup_cmd("kubectl delete deploy redis-operator -n redis-system")
        self.ok("kubectl create deploy redis-operator -n redis-system --image=busybox:1.36 "
                "-- sh -c 'echo Starting workers controller=rediscluster; sleep 3600'")
        res = wait_for(answer("CLU-022", "s2"), "Starting workers", 90)
        self.assertIn("Starting workers controller=rediscluster", res.out)
        self.apply(final_file("CLU-022", "s3"))
        self.assertIn("sessions", self.answer_ok("CLU-022", "s4").out)


class Etcd(ClusterTest):
    """The etcd commands run inside the etcd static Pod (it has etcdctl and etcdutl)."""

    def etcd(self, cmd):
        argv = shlex.split(cmd)
        argv = [a.replace("ca.crt", f"{PKI}/ca.crt").replace("server.crt", f"{PKI}/server.crt")
                 .replace("server.key", f"{PKI}/server.key").replace("/opt/", "/tmp/") for a in argv]
        return sh(f"kubectl exec -n kube-system {ETCD_POD} -- " + " ".join(shlex.quote(a) for a in argv))

    def test_clu008_snapshot_save_and_status(self):
        res = self.etcd(answer("CLU-008", "s1"))
        self.assertTrue(res, res)
        self.assertIn("saved", res.out.lower())
        status = self.etcd(answer("CLU-008", "s2"))
        self.assertTrue(status, status)
        self.assertIn("TOTAL KEY", status.out.upper())
        # explain: 'etcdctl snapshot status' is gone from etcdctl 3.6
        help_text = self.etcd("etcdctl snapshot --help").out
        self.assertIn("save", help_text)
        self.assertNotIn("status", help_text.split("Available Commands:")[1])

    def test_clu009_restore_into_a_new_directory(self):
        self.etcd(answer("CLU-008", "s1"))
        # the etcd image has no shell or rm: a fresh directory name per run
        cmd = answer("CLU-009", "s1").replace("/var/lib/etcd-restored", f"/tmp/etcd-restored-{time.time_ns()}")
        first = self.etcd(cmd)
        self.assertTrue(first, first)
        again = self.etcd(cmd)
        self.assertFalse(again, "restore must refuse an existing directory")  # explain says so
        self.assertIn("not empty", again.out)


class NodeLevel(ClusterTest):
    """Commands the player types on a node: run inside the kind node (a systemd machine)."""

    def test_clu011_certificate_expiration_table(self):
        out = node(answer("CLU-011", "s1")).out
        self.assertIn("apiserver", out)
        self.assertIn("EXPIRES", out)
        for cert in ("admin.conf", "controller-manager.conf", "scheduler.conf"):
            self.assertIn(cert, out)  # explain: kubeconfig-embedded certificates are covered
        self.assertIn("super-admin.conf", out)

    def test_clu013_containerd_default_has_the_option_the_game_edits(self):
        default = node("containerd config default").out
        self.assertEqual(default.count("SystemdCgroup = false"), 1)
        shown = final_file("CLU-013", "s2")
        # the file the game shows must start with the version the real default file starts with
        self.assertEqual(default.splitlines()[0], shown.splitlines()[0])
        prefill = [ln.strip() for ln in final_file("CLU-013", "s2").splitlines() if ln.strip()]
        for line in prefill:
            if line != "SystemdCgroup = true":
                self.assertIn(line, default, line)  # the excerpt really is part of the default file

    def test_clu014_admission_plugin_exists(self):
        res = sh(f"kubectl exec -n kube-system kube-apiserver-{CONTROL_PLANE} -- kube-apiserver --help")
        self.assertIn("AlwaysPullImages", res.out)
        manifest = node("cat /etc/kubernetes/manifests/kube-apiserver.yaml").out
        self.assertIn("--enable-admission-plugins=NodeRestriction", manifest)  # the line the game edits

    def test_clu024_clu025_crictl_on_a_node(self):
        sh_cmd = lambda c: node(c.replace("sudo ", ""))  # noqa: E731
        self.assertIn("CONTAINER", sh_cmd(answer("CLU-024", "s1")).out)
        cid = node("crictl ps -a -q | head -1").out.strip()
        self.assertTrue(cid)
        real = lambda sid: answer("CLU-024", sid).replace("3f9c1a2b7e44d", cid)  # noqa: E731
        self.assertTrue(sh_cmd(real("s2")), sh_cmd(real("s2")))        # logs of a real container id
        inspected = sh_cmd(real("s3"))                                 # explain: status.exitCode in the JSON
        self.assertTrue(inspected, inspected)
        self.assertIn('"exitCode"', inspected.out)
        self.assertTrue(sh_cmd(answer("CLU-024", "s4").replace("billing", "pause")))
        cmd = answer("CLU-025", "s2")
        self.assertTrue(sh_cmd(cmd), sh_cmd(cmd))
        self.assertTrue(sh(answer("CLU-025", "s1")))


class NodeProcedures(ClusterTest):
    """kubeadm and node preparation commands, on the nodes of the kind cluster."""

    def test_clu002_drain_upgrade_node_uncordon(self):
        self.cleanup_cmd(f"kubectl uncordon {WORKER}; kubectl delete deploy drain-me")
        self.apply(f"""apiVersion: apps/v1
kind: Deployment
metadata: {{name: drain-me}}
spec:
  replicas: 1
  selector: {{matchLabels: {{app: drain-me}}}}
  template:
    metadata: {{labels: {{app: drain-me}}}}
    spec:
      nodeSelector: {{kubernetes.io/hostname: {WORKER}}}
      containers: [{{name: c, image: "busybox:1.36", command: [sleep, "3600"], volumeMounts: [{{name: v, mountPath: /v}}]}}]
      volumes: [{{name: v, emptyDir: {{}}}}]
""")
        self.ok("kubectl wait --for=condition=Available deploy/drain-me --timeout=90s")
        fix = lambda sid: answer("CLU-002", sid).replace("node01", WORKER)  # noqa: E731
        # explain: drain refuses DaemonSet Pods and emptyDir data unless you accept both
        refused = self.fails(f"kubectl drain {WORKER}", "--ignore-daemonsets")
        self.assertIn("cannot delete", refused.out)
        self.ok(fix("s1"), "drained", timeout=120)
        self.assertIn("SchedulingDisabled", self.ok(f"kubectl get node {WORKER}").out)
        res = node(fix("s2").replace("sudo ", ""), which=WORKER, timeout=180)
        self.assertTrue(res, res)                                  # 'kubeadm upgrade node' on a worker
        self.ok(fix("s3"))
        self.assertNotIn("SchedulingDisabled", self.ok(f"kubectl get node {WORKER}").out)

    def test_clu005_join_command_carries_a_token_and_the_ca_hash(self):
        out = node(answer("CLU-005", "s1").replace("sudo ", "")).out
        self.assertRegex(out, r"kubeadm join \S+:6443 --token \S+ \\?\s*--discovery-token-ca-cert-hash sha256:[0-9a-f]{64}")
        stored = answer("CLU-005", "s2")                                # the game's join uses the same flags
        for flag in ("--token", "--discovery-token-ca-cert-hash sha256:"):
            self.assertIn(flag, stored)
        self.assertIn("Ready", self.answer_ok("CLU-005", "s3").out)

    def test_clu006_config_validates_and_admin_conf_belongs_to_the_admin_group(self):
        cfg = final_file("CLU-006", "s1")
        self.assertTrue(node("cat > /tmp/kubeadm-config.yaml && kubeadm config validate --config /tmp/kubeadm-config.yaml",
                             which=WORKER, stdin=cfg))
        # explain: with --config the file is the only source: kubeadm refuses to mix in such flags
        mixed = node("kubeadm init --config /tmp/kubeadm-config.yaml --pod-network-cidr=10.1.0.0/16 --dry-run",
                     which=WORKER)
        self.assertFalse(mixed)
        self.assertIn("can not mix '--config' with arguments [pod-network-cidr]", mixed.out)
        self.assertIn("10.244.0.0/16", cfg)
        subject = node("grep client-certificate-data /etc/kubernetes/admin.conf | awk '{print $2}' | base64 -d | "
                       "openssl x509 -noout -subject").out
        self.assertIn("kubeadm:cluster-admins", subject)             # explain: admin.conf's group
        sup = node("grep client-certificate-data /etc/kubernetes/super-admin.conf | awk '{print $2}' | base64 -d | "
                   "openssl x509 -noout -subject").out
        self.assertIn("system:masters", sup)                         # ... and the break-glass one

    def test_clu007_upgrade_plan_is_read_only(self):
        before = node("ls -l /etc/kubernetes/manifests").out
        res = node(answer("CLU-007", "s1").replace("sudo ", ""), timeout=180)
        self.assertTrue(res, res)
        self.assertIn("v1.37", res.out)
        self.assertEqual(node("ls -l /etc/kubernetes/manifests").out, before)   # nothing changed

    def test_clu012_node_prerequisites(self):
        run = lambda sid: node(answer("CLU-012", sid), which=WORKER)  # noqa: E731
        self.assertTrue(run("s1"), run("s1"))
        self.assertEqual(node("swapon --show --noheadings | wc -l", which=WORKER).out, "0")
        mods = run("s2")
        if not mods:                                               # a container may be refused kernel modules
            self.skipTest(f"modprobe not allowed in this kind node: {mods.out}")
        loaded = node("lsmod", which=WORKER).out
        self.assertIn("br_netfilter", loaded)
        self.assertTrue(run("s4"), run("s4"))
        self.assertEqual(node("sysctl -n net.ipv4.ip_forward", which=WORKER).out, "1")

    def test_clu023_cni_directory_on_a_node(self):
        out = node("ls /etc/cni/net.d", which=WORKER)
        self.assertTrue(out, out)                                  # the command of CLU-023 s1 / TRB-027 s1
        self.assertEqual(answer("CLU-023", "s1").replace("sudo ", "").split()[0], "ls")


class Admin(ClusterTest):
    def test_clu025_csidrivers_listing_works(self):
        self.ok(answer("CLU-025", "s1"))


if __name__ == "__main__":
    unittest.main()
