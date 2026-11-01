"""Every manifest a player ends up with after the correct edit step must be accepted by the
real API server (schema-checked, strict: an unknown or misspelled field fails)."""
import unittest

from lib import ClusterTest, QUESTIONS, final_file, node, sh, t

# Short excerpts of a bigger file (no volumes / selector): cannot be validated alone. Their one
# changed line is checked in test_troubleshooting.py instead.
EXCERPTS = {"TRB-019/s3", "TRB-029/s3"}


def edit_steps():
    for q in QUESTIONS:
        for sid, s in q["steps"].items():
            if s["type"] == "edit":
                final = final_file(q["id"], sid)
                if "apiVersion:" in final and "kind:" in final:
                    yield q["id"], sid, s.get("filename", ""), final


class CorrectFilesAreValid(ClusterTest):
    def test_every_correct_final_manifest_is_accepted(self):
        checked = 0
        for qid, sid, filename, final in edit_steps():
            with self.subTest(step=f"{qid}/{sid} {filename}"):
                if f"{qid}/{sid}" in EXCERPTS:
                    continue
                if "kustomize.config.k8s.io" in final:
                    continue  # needs a directory: see test_cluster.py
                if "kubeadm.k8s.io" in final:
                    res = node("cat > /tmp/kc.yaml && kubeadm config validate --config /tmp/kc.yaml",
                               stdin=final)
                    self.assertTrue(res, f"{qid}/{sid}\n{res}")
                    checked += 1
                    continue
                # static Pod manifests of the control plane exist already as mirror Pods: schema only
                mode = "client" if "/etc/kubernetes/manifests" in filename else "server"
                res = sh(f"kubectl apply --dry-run={mode} --validate=strict -f -", stdin=final)
                if "no matches for kind" in res.out:
                    continue  # a CRD the game's scenario installs itself (Database, Installation...)
                self.assertTrue(res, f"{qid}/{sid} {filename}\n{res}")
                checked += 1
        self.assertGreater(checked, 35)


if __name__ == "__main__":
    unittest.main()
