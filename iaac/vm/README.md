# The VM lab: an exam-like cluster on libvirt

The kind cluster of `iaac/` checks most answers, but a container cannot do everything a CKA task does on
a real node: join a second control plane, lose an etcd member and keep quorum, upgrade the kubelet
package, turn swap off or load kernel modules without touching your own machine. This lab builds what
the exam gives you: **Ubuntu 24.04 nodes, kubeadm v1.37, containerd, Calico**, as code (Ansible +
libvirt); the only manual steps are the host prerequisites below. It comes from CKS Megatrainer's lab,
adapted to the CKA.

| | exam layout (`make vm-up`) | HA layout (`make vm-up HA=1`) |
|---|---|---|
| Nodes | `cp1` 192.168.124.10, `node01` .21; 2 vCPU, 3 GiB each | `lb` .5 (HAProxy, 512 MiB), `cp1` .10, `cp2` .11, `cp3` .12, `node01` .21; 2 GiB each |
| API endpoint | `cp1:6443` | `lb:6443`: kubeadm `controlPlaneEndpoint`, stacked etcd on the 3 control planes |
| RAM | about 6 GiB | about 8.5 GiB |

Both: Ubuntu 24.04 LTS cloud image (dated build, SHA-256 pinned) + cloud-init; kubeadm / kubelet / kubectl
1.37.0 from pkgs.k8s.io (not on hold: the upgrade questions upgrade them); containerd 2.3.4, runc 1.4.3, CNI
plugins v1.10.0, crictl v1.37.0, and etcdctl / etcdutl v3.7.0 on the control planes (all checksum-verified);
Calico v3.33.0; the kind lab's add-ons (`iaac/addons.sh`: Gateway API CRDs, metrics-server). Versions live in
[`group_vars/all.yml`](group_vars/all.yml).

Every libvirt object is named `cka…` (domains `cka-cp1`…, network and storage pool `cka`), so another lab
on the same host (for example CKS Megatrainer's `cp1` / `node01`) is never touched.

## 1. Host prerequisites (once)

A Linux machine with hardware virtualisation (`/dev/kvm`), the free RAM of the layout above, and 20 GiB of
disk. Install libvirt + QEMU and `xorriso` (it builds the cloud-init seed images):

| Host | Command |
|---|---|
| Fedora Workstation | `sudo dnf install libvirt-daemon-kvm libvirt-daemon-config-network libvirt-client qemu-img xorriso` |
| Fedora Silverblue / Kinoite | `sudo rpm-ostree install libvirt-daemon-kvm libvirt-daemon-config-network libvirt-client qemu-img xorriso`, then reboot |
| Ubuntu / Debian | `sudo apt install libvirt-daemon-system libvirt-clients qemu-system-x86 qemu-utils xorriso` |

Then let your user manage system VMs without sudo, and start libvirt:

```sh
# Fedora Silverblue only: the group is defined in /usr/lib/group, copy it to /etc/group first
grep -E '^libvirt:' /usr/lib/group | sudo tee -a /etc/group

sudo usermod -aG libvirt "$USER"          # then log out and back in (or reboot)
sudo systemctl enable --now virtqemud.socket virtnetworkd.socket virtstoraged.socket   # Fedora
sudo systemctl enable --now libvirtd                                                    # Ubuntu / Debian

virsh -c qemu:///system list --all        # must answer without sudo
```

Python 3.9+ with `venv` is the only other requirement: `make vm-venv` installs the pinned Ansible into
`iaac/vm/.venv`, nothing system-wide. `make iaac-tools` fetches the pinned helm into `iaac/.bin`.

## 2. Build, test, destroy

```sh
make vm-up              # exam layout: VMs + kubeadm cluster + Calico + add-ons (about 15 min the first time)
make vm-up HA=1         # HA layout instead (make vm-down first when switching layouts)
make vm-status          # domains and Kubernetes nodes
make vm-test            # every question's answers against the VMs
make vm-test-destructive    # plus the tests that break and repair the control plane
make vm-down            # delete VMs, disks, network (keeps the image cache and the SSH key)
```

- Every step is idempotent: run `make vm-up` again after a failure.
- One test file: `make vm-test ARGS=test_cluster`; one test: `ARGS=test_ha_control_plane.HAControlPlane.test_x`.
- Log in to a node: `ssh -F iaac/vm/.ssh/config cp1` (user `ubuntu`, passwordless sudo).
- kubectl from the host: `KUBECONFIG=iaac/.kubeconfig-vm iaac/.bin/kubectl get nodes` (context `cka-vm`,
  server by IP). Your own `~/.kube/config` is never read or written.

## 3. How it works

| File | Role |
|---|---|
| `site.yml` | runs the steps below in order, then `iaac/addons.sh` with `IAAC_TARGET=vm` |
| `inventory.yml` | the hosts and groups of the chosen layout (`control_plane`, `workers`, `lb`), from `vms` in `group_vars/all.yml` |
| `host.yml`, `host-vm.yml` | on the host: storage pool `cka`, NAT network `cka` with fixed DHCP leases and DNS names, the cloud image as a read-only base volume, one copy-on-write disk and one cloud-init seed per VM, the domains |
| `nodes.yml` | on every Kubernetes node: kernel modules and sysctls, containerd + runc + CNI plugins + crictl, the Kubernetes packages, etcd's client tools on the control planes; HAProxy on `lb` |
| `cluster.yml` | `kubeadm init` on `cp1` (with `--upload-certs` in the HA layout), the other control planes join one at a time with `--control-plane --certificate-key`, `node01` joins, Calico, the admin kubeconfig to `iaac/.kubeconfig-vm` |
| `destroy.yml` | removes everything `host.yml` created, for either layout |

The cluster tests (`iaac/tests`) run unchanged: `IAAC_TARGET=vm` makes `lib.py` reach the nodes over SSH
(`sudo`) instead of `podman exec`, and use `cp1` / `node01`, the names the questions use.

## 4. What only this lab tests

The HA questions for real (CLU-026..030, TRB-036): three etcd members, the load balancer in front of the API
servers, a control plane joining with the certificate key, quorum kept when one member is down. Kernel-level
node preparation (CLU-012: swap, modules, sysctls) without touching your host, a real Calico (NetworkPolicy
without kindnet's limits), and a kubelet that is a real systemd service on a real machine.

## 5. Troubleshooting

- `virsh: error: failed to connect to the hypervisor`: the user is not in group `libvirt` yet (log out and in),
  or the libvirt sockets are not started.
- A node does not answer SSH: `virsh -c qemu:///system console cka-cp1` (leave with `Ctrl+]`).
- Start over: `make vm-down && make vm-up`.
- Resources: stop kind clusters while the VMs run (`make iaac-down`).
