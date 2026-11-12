#!/bin/sh
# Add-ons the questions need and a bare cluster lacks: Gateway API CRDs (NET-014..016, NET-023) and
# metrics-server (kubectl top, HPA). Versions are pinned so runs are repeatable.
#   iaac/addons.sh                  into the kind cluster (iaac/cluster.sh up runs it)
#   IAAC_TARGET=vm iaac/addons.sh   into the libvirt VM cluster (iaac/vm, make vm-up runs it)
# When GitHub release downloads fail (it happens: HTTP 504), the same versions come from their git tags.
cd "$(dirname "$0")" || exit 1
if [ "${IAAC_TARGET:-kind}" = vm ]; then
  export KUBECONFIG="$PWD/.kubeconfig-vm"
  K=".bin/kubectl --context cka-vm"
else
  CLUSTER=${CLUSTER:-cka-test}
  export KUBECONFIG="$PWD/.kubeconfig"
  K=".bin/kubectl --context kind-$CLUSTER"
fi
GATEWAY_API=v1.7.0
METRICS_SERVER=v0.9.1
LOCAL_PATH=v0.0.34

if [ "${IAAC_TARGET:-kind}" = vm ]; then
  # kind ships rancher's local-path provisioner as the default class 'standard'; give the VM cluster
  # the same, so the storage tests see the same class on both targets
  $K apply -f "https://raw.githubusercontent.com/rancher/local-path-provisioner/$LOCAL_PATH/deploy/local-path-storage.yaml" || exit 1
  $K apply -f - <<'SC' || exit 1
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: standard
  annotations: {storageclass.kubernetes.io/is-default-class: "true"}
provisioner: rancher.io/local-path
reclaimPolicy: Delete
volumeBindingMode: WaitForFirstConsumer
SC
  $K -n local-path-storage rollout status deploy local-path-provisioner --timeout=180s || exit 1
fi

$K apply --server-side -f "https://github.com/kubernetes-sigs/gateway-api/releases/download/$GATEWAY_API/standard-install.yaml" ||
  $K apply --server-side -k "https://github.com/kubernetes-sigs/gateway-api/config/crd?ref=$GATEWAY_API" || exit 1
$K apply -f "https://github.com/kubernetes-sigs/metrics-server/releases/download/$METRICS_SERVER/components.yaml" ||
  $K apply -k "https://github.com/kubernetes-sigs/metrics-server/manifests/overlays/release?ref=$METRICS_SERVER" || exit 1
# kubeadm kubelets use self-signed serving certificates (the same lab-only trick as question TRB-029)
if ! $K -n kube-system get deploy metrics-server -o jsonpath='{.spec.template.spec.containers[0].args}' |
     grep -q -- --kubelet-insecure-tls; then
  $K -n kube-system patch deploy metrics-server --type=json \
    -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]' || exit 1
fi
$K -n kube-system rollout status deploy metrics-server --timeout=180s
