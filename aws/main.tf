// MoQ relay perf lab on AWS: one relay host + N load-generator hosts in a
// private VPC and one cluster placement group, sized so the load generators
// cannot become the bottleneck. The AWS counterpart of oci/main.tf, with the
// same outputs, so bin/ drives either the same way.
//
// One root module. An environment, envs/<env>.tfvars (shared, tracked) and/or
// envs/<env>.local.tfvars (one tester's own settings, gitignored), says where
// and how big; a deployment is a named instance of one, with its own Terraform
// workspace, and every resource it creates is named and tagged for it. Drive it
// through ./bin/testbed.sh:
//
//   ./bin/testbed.sh fanout-1 plan --env moqbench-aws
//   ./bin/testbed.sh fanout-1 up -y

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region  = var.region
  profile = var.profile != "" ? var.profile : null
  // A wrong profile fails here, before anything is created.
  allowed_account_ids = var.expected_account_id != "" ? [var.expected_account_id] : null
  default_tags {
    tags = local.tags
  }
}

// Authentication: a profile in the tester's own ~/.aws/config, named in their
// envs/<env>.local.tfvars (or AWS_PROFILE). Keys and SSO tokens stay in ~/.aws;
// nothing in this repo holds one.
variable "profile" {
  type    = string
  default = ""
}
variable "expected_account_id" {
  type        = string
  default     = ""
  description = "If set, refuse to plan in any other account. Catches a wrong profile before it creates anything."
}
variable "region" {
  type    = string
  default = "us-west-2"
}
variable "availability_zone" {
  type        = string
  default     = ""
  description = "Leave empty to use the first AZ in the region."
}
// The environment (envs/<env>.tfvars) this deployment was made from; the
// deployment itself is the Terraform workspace. bin/testbed.sh sets it.
variable "environment" {
  type    = string
  default = ""
}
variable "owner" {
  type        = string
  default     = ""
  description = "Tagged on every resource when set, so a shared account shows whose lab it is."
}

// Who may reach the hosts from outside the VPC: the laptop/VPN egress, plus
// any load generator that is not in this VPC. Benchmark traffic inside the VPC
// is unrestricted regardless.
variable "allowed_cidrs" {
  type = list(string)
  validation {
    condition     = length(var.allowed_cidrs) > 0 && alltrue([for c in var.allowed_cidrs : can(cidrnetmask(c))])
    error_message = "allowed_cidrs must be a non-empty list of IPv4 CIDRs."
  }
}
// The tester's SSH key: the .pub beside it goes onto every host, and the
// drivers use the private key via the ssh_key_file output.
variable "ssh_key_file" {
  type        = string
  description = "Path to your SSH private key, e.g. ~/.ssh/id_ed25519; <path>.pub must exist."
}

// CPU architecture of every host; the instance types must support it.
variable "arch" {
  type    = string
  default = "arm64"
  validation {
    condition     = contains(["arm64", "amd64"], var.arch)
    error_message = "arch must be arm64 or amd64."
  }
}
variable "root_volume_gb" { default = 60 }

// Relay: the system under test. c6gn = Graviton2, 64 cores, 100 Gbps.
variable "relay_instance_type" { default = "c6gn.16xlarge" }
// Relays of one type; more than one for a relay fabric.
variable "relay_count" {
  default = 1
  validation {
    condition     = var.relay_count >= 1
    error_message = "relay_count must be at least 1."
  }
}

// Load generators: enough of them that one client CPU never caps the
// measurement; verify per run via client_box_busy_pct.
variable "client_count" { default = 1 }
variable "client_instance_type" { default = "c6gn.16xlarge" }

// Monitor/probe: a small host off the measured path. 0 where an environment
// does not need one.
variable "probe_count" { default = 0 }
variable "probe_instance_type" { default = "c6g.xlarge" }

locals {
  deployment = terraform.workspace
  tags = merge({ project = "moqbench", deployment = local.deployment, purpose = "perf-bench" },
    var.environment != "" ? { environment = var.environment } : {},
  var.owner != "" ? { owner = var.owner } : {})
  ssh_public_key = trimspace(file(pathexpand("${var.ssh_key_file}.pub")))
  az             = var.availability_zone != "" ? var.availability_zone : data.aws_availability_zones.azs.names[0]
  // Instance-type architecture names differ from the image's.
  ec2_arch = var.arch == "arm64" ? "arm64" : "x86_64"
  // Ports the harness uses: SSH, MoQ relay range, relay admin, node_exporter, iperf3.
  ingress_tcp = [22, 4433, 4533, 4500, 9100, 5201]
  ingress_udp = [443, 4433, 4533, 5201]
  instance_types = toset(concat([var.relay_instance_type],
    var.client_count > 0 ? [var.client_instance_type] : [],
  var.probe_count > 0 ? [var.probe_instance_type] : []))
}

data "aws_availability_zones" "azs" {
  state = "available"
}

data "aws_ssm_parameter" "ubuntu" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/${var.arch}/hvm/ebs-gp3/ami-id"
}

data "aws_ec2_instance_type" "used" {
  for_each      = local.instance_types
  instance_type = each.value
}

// Nothing is created unless every instance type runs the chosen arch.
resource "terraform_data" "arch_guard" {
  input = { arch = var.arch, types = local.instance_types }
  lifecycle {
    precondition {
      condition     = alltrue([for t in data.aws_ec2_instance_type.used : contains(t.supported_architectures, local.ec2_arch)])
      error_message = "Every instance type must support ${local.ec2_arch} (arch = ${var.arch})."
    }
  }
}

resource "aws_vpc" "lab" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_hostnames = true
  tags                 = { Name = "${local.deployment}-vpc" }
  depends_on           = [terraform_data.arch_guard]
}

resource "aws_internet_gateway" "gw" {
  vpc_id = aws_vpc.lab.id
  tags   = { Name = "${local.deployment}-igw" }
}

resource "aws_route_table" "rt" {
  vpc_id = aws_vpc.lab.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.gw.id
  }
  tags = { Name = "${local.deployment}-rt" }
}

resource "aws_subnet" "lab" {
  vpc_id                  = aws_vpc.lab.id
  cidr_block              = "10.42.1.0/24"
  availability_zone       = local.az
  map_public_ip_on_launch = true
  tags                    = { Name = "${local.deployment}-subnet" }
}

resource "aws_route_table_association" "lab" {
  subnet_id      = aws_subnet.lab.id
  route_table_id = aws_route_table.rt.id
}

resource "aws_security_group" "sg" {
  name   = "${local.deployment}-sg"
  vpc_id = aws_vpc.lab.id
  tags   = { Name = "${local.deployment}-sg" }
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.sg.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

// Unrestricted between members: this is where the benchmark traffic lives, and
// per-port rules would only add a variable we do not want to measure.
resource "aws_vpc_security_group_ingress_rule" "members" {
  security_group_id            = aws_security_group.sg.id
  referenced_security_group_id = aws_security_group.sg.id
  ip_protocol                  = "-1"
}

resource "aws_vpc_security_group_ingress_rule" "tcp" {
  for_each          = { for p in setproduct(var.allowed_cidrs, local.ingress_tcp) : "${p[0]}:${p[1]}" => p }
  security_group_id = aws_security_group.sg.id
  cidr_ipv4         = each.value[0]
  ip_protocol       = "tcp"
  from_port         = each.value[1]
  to_port           = each.value[1]
}

resource "aws_vpc_security_group_ingress_rule" "udp" {
  for_each          = { for p in setproduct(var.allowed_cidrs, local.ingress_udp) : "${p[0]}:${p[1]}" => p }
  security_group_id = aws_security_group.sg.id
  cidr_ipv4         = each.value[0]
  ip_protocol       = "udp"
  from_port         = each.value[1]
  to_port           = each.value[1]
}

// Path MTU discovery: ICMP "fragmentation needed" from anywhere.
resource "aws_vpc_security_group_ingress_rule" "pmtu" {
  security_group_id = aws_security_group.sg.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "icmp"
  from_port         = 3
  to_port           = 4
}

resource "aws_placement_group" "lab" {
  name     = "${local.deployment}-pg"
  strategy = "cluster"
}

resource "aws_key_pair" "lab" {
  key_name   = "${local.deployment}-key"
  public_key = local.ssh_public_key
}

// Kernel + limits tuning applied at first boot, identical on every host, so the
// relay and the load generators are never tuned differently by accident.
locals {
  cloud_init = <<-EOT
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
      # The NIC carrying the default route (ens5 on Nitro).
      - ip link set dev "$(ip -o route show default | awk '{print $5; exit}')" txqueuelen 20000 || true
      - apt-get update -q
      - DEBIAN_FRONTEND=noninteractive apt-get install -y -q iperf3 linux-tools-common linux-tools-aws prometheus-node-exporter numactl
      - systemctl enable --now prometheus-node-exporter || true
  EOT
}

moved {
  from = aws_instance.relay
  to   = aws_instance.relay[0]
}

resource "aws_instance" "relay" {
  count                  = var.relay_count
  ami                    = data.aws_ssm_parameter.ubuntu.insecure_value
  instance_type          = var.relay_instance_type
  subnet_id              = aws_subnet.lab.id
  vpc_security_group_ids = [aws_security_group.sg.id]
  placement_group        = aws_placement_group.lab.id
  key_name               = aws_key_pair.lab.key_name
  user_data              = local.cloud_init
  tags                   = { Name = "${local.deployment}-relay-${format("%02d", count.index + 1)}" }

  metadata_options {
    http_tokens = "required"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_gb
    delete_on_termination = true
  }

  // A newer AMI must not replace a host and its provisioned binaries.
  lifecycle {
    ignore_changes = [ami]
  }
}

resource "aws_instance" "client" {
  count                  = var.client_count
  ami                    = data.aws_ssm_parameter.ubuntu.insecure_value
  instance_type          = var.client_instance_type
  subnet_id              = aws_subnet.lab.id
  vpc_security_group_ids = [aws_security_group.sg.id]
  placement_group        = aws_placement_group.lab.id
  key_name               = aws_key_pair.lab.key_name
  user_data              = local.cloud_init
  tags                   = { Name = "${local.deployment}-client-${format("%02d", count.index + 1)}" }

  metadata_options {
    http_tokens = "required"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_gb
    delete_on_termination = true
  }

  lifecycle {
    ignore_changes = [ami]
  }
}

resource "aws_instance" "probe" {
  count                  = var.probe_count
  ami                    = data.aws_ssm_parameter.ubuntu.insecure_value
  instance_type          = var.probe_instance_type
  subnet_id              = aws_subnet.lab.id
  vpc_security_group_ids = [aws_security_group.sg.id]
  placement_group        = aws_placement_group.lab.id
  key_name               = aws_key_pair.lab.key_name
  user_data              = local.cloud_init
  tags                   = { Name = "${local.deployment}-probe-${format("%02d", count.index + 1)}" }

  metadata_options {
    http_tokens = "required"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_gb
    delete_on_termination = true
  }

  lifecycle {
    ignore_changes = [ami]
  }
}

output "relay_public_ip" { value = aws_instance.relay[0].public_ip }
output "relay_private_ip" { value = aws_instance.relay[0].private_ip }
output "relay_public_ips" { value = aws_instance.relay[*].public_ip }
output "relay_private_ips" { value = aws_instance.relay[*].private_ip }
output "client_public_ips" { value = aws_instance.client[*].public_ip }
output "client_private_ips" { value = aws_instance.client[*].private_ip }
output "probe_public_ips" { value = aws_instance.probe[*].public_ip }
output "probe_private_ips" { value = aws_instance.probe[*].private_ip }
output "arch" { value = var.arch }
output "region" { value = var.region }

output "ssh_key_file" {
  description = "Private key the drivers use to reach the hosts."
  value       = pathexpand(var.ssh_key_file)
}

output "instance_ids" {
  description = "For ./bin/testbed.sh stop|start, which power-cycle through the AWS CLI."
  value       = concat(aws_instance.relay[*].id, aws_instance.client[*].id, aws_instance.probe[*].id)
}

output "inventory" {
  description = "Feed this to the sweep driver."
  value = {
    relay   = aws_instance.relay[0].private_ip
    relays  = aws_instance.relay[*].private_ip
    clients = aws_instance.client[*].private_ip
    probes  = aws_instance.probe[*].private_ip
    ssh     = "ssh ubuntu@${aws_instance.relay[0].public_ip}"
  }
}
