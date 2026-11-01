#!/bin/sh
# Add-ons the questions need and a bare kind cluster lacks: Gateway API CRDs (NET-014..016) and
# metrics-server (kubectl top, HPA). Versions are pinned so runs are repeatable.
cd "$(dirname "$0")" || exit 1
CLUSTER=${CLUSTER:-cka-test}
export KUBECONFIG="$PWD/.kubeconfig"
K=".bin/kubectl --context kind-$CLUSTER"
GATEWAY_API=v1.7.0
METRICS_SERVER=v0.9.1

$K apply --server-side -f "https://github.com/kubernetes-sigs/gateway-api/releases/download/$GATEWAY_API/standard-install.yaml" || exit 1
$K apply -f "https://github.com/kubernetes-sigs/metrics-server/releases/download/$METRICS_SERVER/components.yaml" || exit 1
# kind kubelets use self-signed serving certificates (the same lab-only trick as question TRB-029)
$K -n kube-system patch deploy metrics-server --type=json \
  -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]' || exit 1
$K -n kube-system rollout status deploy metrics-server --timeout=180s
