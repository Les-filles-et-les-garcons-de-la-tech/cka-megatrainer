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

# --- ADDONS=full (VM lab): the controllers some answers need to really work (iaac/vm/README.md) ---------
# MetalLB (LoadBalancer IPs from the lab network), Envoy Gateway (Gateway API), Traefik (Ingress),
# csi-driver-host-path (a CSI driver that can grow volumes). All pinned. tests/test_controllers.py uses them.
[ "${ADDONS:-}" = full ] || exit 0
[ "${IAAC_TARGET:-kind}" = vm ] || { echo "ADDONS=full needs the VM lab (IAAC_TARGET=vm)" >&2; exit 1; }
METALLB=v0.16.2
ENVOY_GATEWAY=v1.10.0
TRAEFIK_CHART=42.4.0
CSI_HOSTPATH=v1.19.0
SNAPSHOTTER=v8.7.0
LB_POOL=192.168.124.200-192.168.124.220
export HELM_CONFIG_HOME="$PWD/.helm/config" HELM_CACHE_HOME="$PWD/.helm/cache" HELM_DATA_HOME="$PWD/.helm/data"
H=".bin/helm --kube-context cka-vm"

$K apply -f "https://raw.githubusercontent.com/metallb/metallb/$METALLB/config/manifests/metallb-native.yaml" || exit 1
$K -n metallb-system rollout status deploy controller --timeout=240s || exit 1
$K -n metallb-system wait --for=condition=Ready pod -l component=controller --timeout=120s || exit 1
for i in $(seq 30); do   # the address-pool webhook answers a little after the controller is Ready
  $K apply -f - <<POOL && break
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata: {name: lab, namespace: metallb-system}
spec: {addresses: ["$LB_POOL"]}
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata: {name: lab, namespace: metallb-system}
spec: {ipAddressPools: [lab]}
POOL
  sleep 5
done

# Envoy Gateway: its own CRDs only; the Gateway API CRDs stay the pinned standard v1.7.0 installed above
$H template eg-crds oci://docker.io/envoyproxy/gateway-crds-helm --version "$ENVOY_GATEWAY" \
   --set crds.gatewayAPI.enabled=false --set crds.envoyGateway.enabled=true | $K apply --server-side -f - || exit 1
$H status eg -n envoy-gateway-system >/dev/null 2>&1 ||
  $H install eg oci://docker.io/envoyproxy/gateway-helm --version "$ENVOY_GATEWAY" -n envoy-gateway-system \
     --create-namespace --skip-crds --wait --timeout 10m || exit 1
$K apply -f - <<'GWC' || exit 1
apiVersion: gateway.networking.k8s.io/v1
kind: GatewayClass
metadata: {name: eg}
spec: {controllerName: gateway.envoyproxy.io/gatewayclass-controller}
GWC

$H repo add traefik https://traefik.github.io/charts >/dev/null 2>&1; $H repo update traefik >/dev/null
$H status traefik -n traefik >/dev/null 2>&1 ||
  $H install traefik traefik/traefik --version "$TRAEFIK_CHART" -n traefik --create-namespace \
     --set ingressClass.name=traefik --set providers.kubernetesGateway.enabled=false --wait --timeout 10m || exit 1

# csi-driver-host-path: snapshot CRDs + controller first (its deploy script expects them)
SNAP="https://raw.githubusercontent.com/kubernetes-csi/external-snapshotter/$SNAPSHOTTER"
for f in client/config/crd/snapshot.storage.k8s.io_volumesnapshotclasses.yaml \
         client/config/crd/snapshot.storage.k8s.io_volumesnapshotcontents.yaml \
         client/config/crd/snapshot.storage.k8s.io_volumesnapshots.yaml \
         deploy/kubernetes/snapshot-controller/rbac-snapshot-controller.yaml \
         deploy/kubernetes/snapshot-controller/setup-snapshot-controller.yaml; do
  $K apply -f "$SNAP/$f" || exit 1
done
if ! $K get csidriver hostpath.csi.k8s.io >/dev/null 2>&1; then
  src="$PWD/vm/.cache/csi-driver-host-path-$CSI_HOSTPATH"
  [ -d "$src" ] || { mkdir -p "$src" && curl -fsSL "https://github.com/kubernetes-csi/csi-driver-host-path/archive/refs/tags/$CSI_HOSTPATH.tar.gz" |
                     tar -xz -C "$src" --strip-components=1; } || exit 1
  ( cd "$src" && PATH="$(cd ../../../.bin && pwd):$PATH" ./deploy/kubernetes-latest/deploy.sh ) || exit 1
fi
$K apply -f - <<'SC' || exit 1
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata: {name: csi-hostpath-sc}
provisioner: hostpath.csi.k8s.io
reclaimPolicy: Delete
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
SC
echo "ADDONS=full ready"
