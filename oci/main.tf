// MoQ relay perf lab on OCI: one relay host + N load-generator hosts on a
// private VCN, sized so the load generators cannot become the bottleneck.
//
// On the Linode pair this replaces, the client saturated before the relay did,
// which invalidated every high-end latency number. Here the client fleet is deliberately over-provisioned.
//
// One root module. An environment, envs/<env>.tfvars (shared, tracked) and/or
// envs/<env>.local.tfvars (one tester's own settings, gitignored), says where
// and how big; a deployment is a named instance of one, with its own Terraform
// workspace, and every resource it creates is named and tagged for it. Drive it
// through ./bin/testbed.sh, which keeps a deployment and its environment
// together:
//
//   ./bin/testbed.sh fanout-1 plan --env moqbench-testbed
//   ./bin/testbed.sh fanout-1 up -y
//
// Bare metal BM.Standard.E6.256 (256 cores, 200 Gbps) is the "no excuses"
// option.

terraform {
  required_version = ">= 1.5"
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "~> 6.0"
    }
  }
}

provider "oci" {
  tenancy_ocid        = var.tenancy_ocid
  region              = var.region
  auth                = var.auth
  config_file_profile = var.config_file_profile
}

// Authentication: a profile in the tester's own ~/.oci/config, named in their
// envs/<env>.local.tfvars (or exported as TF_VAR_*). Keys and session tokens
// stay in ~/.oci; nothing in this repo holds one.
variable "config_file_profile" {
  type    = string
  default = "DEFAULT"
}
variable "auth" {
  type    = string
  default = "APIKey"
  validation {
    condition     = contains(["APIKey", "SecurityToken"], var.auth)
    error_message = "auth must be APIKey or SecurityToken."
  }
}
variable "tenancy_ocid" {
  type        = string
  description = "Tenancy the credentials authenticate against."
}

// Deployment target. Usually the tenancy the credentials belong to;
// target_tenancy_ocid deploys into another one.
variable "target_tenancy_ocid" {
  type    = string
  default = ""
}
variable "expected_tenancy_name" {
  type        = string
  default     = ""
  description = "If set, refuse to plan unless the target tenancy has this name. Catches a wrong profile before it creates anything."
}
variable "compartment_ocid" { type = string }
variable "region" { type = string }
// The environment (envs/<env>.tfvars) this deployment was made from; the
// deployment itself is the Terraform workspace. bin/testbed.sh sets it.
variable "environment" {
  type    = string
  default = ""
}
variable "owner" {
  type        = string
  default     = ""
  description = "Tagged on every resource when set, so shared compartments show whose lab it is."
}

// Who may reach the hosts from outside the VCN: the laptop/VPN egress, plus
// any load generator that is not in this VCN (cross-region runs). Benchmark
// traffic inside the VCN is unrestricted regardless.
variable "allowed_cidrs" {
  type = list(string)
  validation {
    condition     = length(var.allowed_cidrs) > 0 && alltrue([for c in var.allowed_cidrs : can(cidrnetmask(c))])
    error_message = "allowed_cidrs must be a non-empty list of IPv4 CIDRs."
  }
}
// The tester's SSH key: the .pub beside it goes onto every host, and the
// drivers (testbed-provision.sh, relay-load.sh, ...) use the private key via the ssh_key_file
// output, so it is configured in exactly one place.
variable "ssh_key_file" {
  type        = string
  description = "Path to your SSH private key, e.g. ~/.ssh/id_ed25519; <path>.pub must exist."
}

variable "availability_domain" {
  type        = string
  default     = ""
  description = "Leave empty to use the first AD in the region."
}

// Networking launch type: VFIO (SR-IOV) or PARAVIRTUALIZED. Empty keeps the
// image's default.
variable "network_type" {
  type    = string
  default = ""
  validation {
    condition     = contains(["", "VFIO", "PARAVIRTUALIZED"], var.network_type)
    error_message = "network_type must be VFIO or PARAVIRTUALIZED."
  }
}

// Relay: the system under test. x86 by choice, so the existing x86-64/glibc-2.35
// binaries run unmodified and the comparison isn't confounded by an ISA change.
//
// 1 OCPU = 1 physical core = 2 vCPU on x86, 1 Gbps/OCPU. 48 OCPU matches
// c7gn.12xlarge's 48 *physical* cores (Graviton3E has no SMT, so its 48 vCPU
// are 48 cores — an x86 match needs 48 OCPU, not 24).
//
// Network is 1 Gbps/OCPU (cap 99), so cores buy headroom: 48 OCPU = 48 Gbps,
// 64 = 64, 94 = 94. Size against the relay's expected per-core
// throughput, not against a smaller testbed's ceiling.
variable "relay_shape" {
  type    = string
  default = "VM.Standard.E6.Flex"
}
variable "relay_ocpus" { default = 94 } // 94 Gbps, the VM ceiling
variable "relay_memory_gb" { default = 188 }

// Load generators: several hosts so one client CPU never caps the measurement.
// One load generator, matching the reference 2-host topology: the publisher
// lives on the relay host, so the only measured network path is the
// relay->subscriber fan-out. Sized large enough that the client cannot cap the
// measurement (the 8-core Linode client did, invalidating every
// high-subscriber latency number) — verify per run via client_box_busy_pct.
variable "client_count" { default = 1 } // publisher is co-located on the relay host
variable "client_shape" {
  type    = string
  default = "VM.Standard.E6.Flex"
}
variable "client_ocpus" { default = 32 }
variable "client_memory_gb" { default = 64 }

// Monitor/probe: a small host off the measured path that carries every load
// tool. Effective-capacity runs keep the publisher on the relay host; this is
// for the other tests — publishing from off-box, probing a relay under load,
// watching the fleet — without borrowing cores from the relay or the load
// generators. 0 where an environment does not need one.
variable "probe_count" { default = 0 }
variable "probe_shape" {
  type    = string
  default = "VM.Standard.E5.Flex"
}
variable "probe_ocpus" { default = 4 }
variable "probe_memory_gb" { default = 8 }

// Ubuntu 22.04 to match the existing toolchain (glibc 2.35 binaries).
variable "image_ocid" {
  type        = string
  description = "Canonical-Ubuntu-22.04 image OCID for your region"
}

locals {
  target_tenancy = var.target_tenancy_ocid != "" ? var.target_tenancy_ocid : var.tenancy_ocid
  // Every resource is named and tagged for its deployment, so several can share
  // a compartment and the console shows which one a resource belongs to.
  deployment = terraform.workspace
  tags = merge({ project = "moqbench", deployment = local.deployment },
    var.environment != "" ? { environment = var.environment } : {},
  var.owner != "" ? { owner = var.owner } : {})
  // VCN DNS label: letters and digits only, at most 15.
  dns_label      = substr(replace(lower(local.deployment), "/[^a-z0-9]/", ""), 0, 15)
  ssh_public_key = trimspace(file(pathexpand("${var.ssh_key_file}.pub")))
  ad             = var.availability_domain != "" ? var.availability_domain : data.oci_identity_availability_domains.ads.availability_domains[0].name
  // Ports the harness uses: SSH, MoQ relay range, relay admin, node_exporter.
  ingress_tcp = [22, 4433, 4533, 4500, 9100, 5201]
  ingress_udp = [443, 4433, 4533, 5201]
}

data "oci_identity_availability_domains" "ads" {
  compartment_id = local.target_tenancy
}

data "oci_identity_tenancy" "target" {
  tenancy_id = local.target_tenancy
}

// Nothing is created until the target checks out. With expected_tenancy_name
// set, the security list is never opened to the Internet: the operator's /32 is
// all the access a run needs.
resource "terraform_data" "target_guard" {
  input = { tenancy = local.target_tenancy, compartment = var.compartment_ocid, region = var.region }
  lifecycle {
    precondition {
      condition     = var.expected_tenancy_name == "" || data.oci_identity_tenancy.target.name == var.expected_tenancy_name
      error_message = "Target tenancy is not ${var.expected_tenancy_name}. Check config_file_profile and target_tenancy_ocid for this environment."
    }
    precondition {
      condition     = var.expected_tenancy_name == "" || !contains([for c in var.allowed_cidrs : split("/", c)[1]], "0")
      error_message = "allowed_cidrs may not contain a /0 when expected_tenancy_name is set."
    }
  }
}

resource "oci_core_vcn" "lab" {
  compartment_id = var.compartment_ocid
  cidr_blocks    = ["10.42.0.0/16"]
  display_name   = "${local.deployment}-vcn"
  dns_label      = local.dns_label
  freeform_tags  = local.tags
  depends_on     = [terraform_data.target_guard]
}

resource "oci_core_internet_gateway" "gw" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.lab.id
  display_name   = "${local.deployment}-igw"
}

resource "oci_core_route_table" "rt" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.lab.id
  display_name   = "${local.deployment}-rt"
  route_rules {
    destination       = "0.0.0.0/0"
    network_entity_id = oci_core_internet_gateway.gw.id
  }
}

resource "oci_core_security_list" "sl" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.lab.id
  display_name   = "${local.deployment}-sl"

  egress_security_rules {
    destination = "0.0.0.0/0"
    protocol    = "all"
  }

  // Unrestricted inside the VCN: this is where the benchmark traffic lives, and
  // per-port rules would only add a variable we do not want to measure.
  // Stateless in both directions: a full connection-tracking table drops packets.
  egress_security_rules {
    destination = "10.42.0.0/16"
    protocol    = "all"
    stateless   = true
  }
  ingress_security_rules {
    source    = "10.42.0.0/16"
    protocol  = "all"
    stateless = true
  }

  dynamic "ingress_security_rules" {
    for_each = setproduct(var.allowed_cidrs, local.ingress_tcp)
    content {
      source   = ingress_security_rules.value[0]
      protocol = "6" // TCP
      tcp_options {
        min = ingress_security_rules.value[1]
        max = ingress_security_rules.value[1]
      }
    }
  }

  dynamic "ingress_security_rules" {
    for_each = setproduct(var.allowed_cidrs, local.ingress_udp)
    content {
      source   = ingress_security_rules.value[0]
      protocol = "17" // UDP
      udp_options {
        min = ingress_security_rules.value[1]
        max = ingress_security_rules.value[1]
      }
    }
  }

  ingress_security_rules {
    source   = "0.0.0.0/0"
    protocol = "1" // ICMP
    icmp_options {
      type = 3
      code = 4
    }
  }
}

resource "oci_core_subnet" "lab" {
  compartment_id    = var.compartment_ocid
  vcn_id            = oci_core_vcn.lab.id
  cidr_block        = "10.42.1.0/24"
  display_name      = "${local.deployment}-subnet"
  dns_label         = "lab"
  route_table_id    = oci_core_route_table.rt.id
  security_list_ids = [oci_core_security_list.sl.id]
}

// Kernel + limits tuning applied at first boot, identical on every host, so the
// relay and the load generators are never tuned differently by accident.
locals {
  cloud_init = base64encode(<<-EOT
    #cloud-config
    write_files:
      - path: /etc/sysctl.d/99-quic-relay-tuning.conf
        content: |
          net.core.rmem_max = 67108864
          net.core.wmem_max = 67108864
          net.core.rmem_default = 8388608
          net.core.wmem_default = 8388608
          net.ipv4.udp_mem = 1048576 1572864 2097152
          net.ipv4.udp_rmem_min = 262144
          net.ipv4.udp_wmem_min = 262144
          net.core.netdev_max_backlog = 250000
          net.core.netdev_budget = 1200
          net.core.netdev_budget_usecs = 12000
          net.core.optmem_max = 1048576
      - path: /etc/security/limits.d/99-perf.conf
        content: |
          * soft nofile 1048576
          * hard nofile 1048576
          root soft nofile 1048576
          root hard nofile 1048576
    runcmd:
      - sysctl -p /etc/sysctl.d/99-quic-relay-tuning.conf
      - ip link set dev ens3 txqueuelen 20000 || true
      # OCI images default to a restrictive iptables policy; the VCN security
      # list is the real boundary here.
      - iptables -F INPUT || true
      - netfilter-persistent save || true
      - apt-get update -q
      - DEBIAN_FRONTEND=noninteractive apt-get install -y -q iperf3 linux-tools-common linux-tools-generic prometheus-node-exporter numactl
      - systemctl enable --now prometheus-node-exporter || true
  EOT
  )
}

resource "oci_core_instance" "relay" {
  compartment_id      = var.compartment_ocid
  availability_domain = local.ad
  display_name        = "${local.deployment}-relay-01"
  shape               = var.relay_shape
  freeform_tags       = local.tags

  // Keep legacy IMDSv1 endpoints disabled; this workload does not need them.
  // This also supports environments whose policies require secure metadata access.
  instance_options {
    are_legacy_imds_endpoints_disabled = true
  }

  dynamic "launch_options" {
    for_each = var.network_type != "" ? [var.network_type] : []
    content {
      network_type = launch_options.value
    }
  }

  shape_config {
    ocpus         = var.relay_ocpus
    memory_in_gbs = var.relay_memory_gb
  }

  create_vnic_details {
    subnet_id        = oci_core_subnet.lab.id
    assign_public_ip = true
    hostname_label   = "moq-relay-01"
  }

  source_details {
    source_type             = "image"
    source_id               = var.image_ocid
    boot_volume_size_in_gbs = 100
  }

  metadata = {
    ssh_authorized_keys = local.ssh_public_key
    user_data           = local.cloud_init
  }
}

resource "oci_core_instance" "client" {
  count               = var.client_count
  compartment_id      = var.compartment_ocid
  availability_domain = local.ad
  display_name        = "${local.deployment}-client-${format("%02d", count.index + 1)}"
  shape               = var.client_shape
  freeform_tags       = local.tags

  // Keep legacy IMDSv1 endpoints disabled; this workload does not need them.
  // This also supports environments whose policies require secure metadata access.
  instance_options {
    are_legacy_imds_endpoints_disabled = true
  }

  dynamic "launch_options" {
    for_each = var.network_type != "" ? [var.network_type] : []
    content {
      network_type = launch_options.value
    }
  }

  shape_config {
    ocpus         = var.client_ocpus
    memory_in_gbs = var.client_memory_gb
  }

  create_vnic_details {
    subnet_id        = oci_core_subnet.lab.id
    assign_public_ip = true
    hostname_label   = "moq-client-${format("%02d", count.index + 1)}"
  }

  source_details {
    source_type             = "image"
    source_id               = var.image_ocid
    boot_volume_size_in_gbs = 60
  }

  metadata = {
    ssh_authorized_keys = local.ssh_public_key
    user_data           = local.cloud_init
  }
}

resource "oci_core_instance" "probe" {
  count               = var.probe_count
  compartment_id      = var.compartment_ocid
  availability_domain = local.ad
  display_name        = "${local.deployment}-probe-${format("%02d", count.index + 1)}"
  shape               = var.probe_shape
  freeform_tags       = local.tags

  instance_options {
    are_legacy_imds_endpoints_disabled = true
  }

  dynamic "launch_options" {
    for_each = var.network_type != "" ? [var.network_type] : []
    content {
      network_type = launch_options.value
    }
  }

  shape_config {
    ocpus         = var.probe_ocpus
    memory_in_gbs = var.probe_memory_gb
  }

  create_vnic_details {
    subnet_id        = oci_core_subnet.lab.id
    assign_public_ip = true
    hostname_label   = "moq-probe-${format("%02d", count.index + 1)}"
  }

  source_details {
    source_type             = "image"
    source_id               = var.image_ocid
    boot_volume_size_in_gbs = 60
  }

  metadata = {
    ssh_authorized_keys = local.ssh_public_key
    user_data           = local.cloud_init
  }
}

output "relay_public_ip" { value = oci_core_instance.relay.public_ip }
output "relay_private_ip" { value = oci_core_instance.relay.private_ip }
output "client_public_ips" { value = oci_core_instance.client[*].public_ip }
output "client_private_ips" { value = oci_core_instance.client[*].private_ip }
output "probe_public_ips" { value = oci_core_instance.probe[*].public_ip }
output "probe_private_ips" { value = oci_core_instance.probe[*].private_ip }

output "ssh_key_file" {
  description = "Private key the drivers use to reach the hosts."
  value       = pathexpand(var.ssh_key_file)
}

output "instance_ids" {
  description = "For ./bin/testbed.sh stop|start, which power-cycle through the OCI CLI."
  value       = concat([oci_core_instance.relay.id], oci_core_instance.client[*].id, oci_core_instance.probe[*].id)
}

output "inventory" {
  description = "Feed this to the sweep driver."
  value = {
    relay   = oci_core_instance.relay.private_ip
    clients = oci_core_instance.client[*].private_ip
    probes  = oci_core_instance.probe[*].private_ip
    ssh     = "ssh ubuntu@${oci_core_instance.relay.public_ip}"
  }
}
