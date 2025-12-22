
data "template_file" "vm" {
  for_each = var.vm_configs
  template = file("${path.module}/user_data/cloudinit-ansible.yaml.tmpl")
  vars = {
    hostname                = each.value.name
    ansible_ssh_public_key  = var.ansible_pub_key
    root_ssh_public_key     = var.root_pub_key
  }
}

resource "local_file" "vm" {
  for_each = var.vm_configs
  content  = data.template_file.vm[each.key].rendered
  filename = "${path.module}/snippets/cloudinit-ansible-${each.value.vmid}.yaml"
}

resource "null_resource" "upload_cloudinit" {
  for_each = var.vm_configs
  depends_on = [local_file.vm]
  provisioner "file" {
    source      = local_file.vm[each.key].filename
    destination = "/var/lib/vz/snippets/cloudinit-ansible-${each.value.vmid}.yaml"
    connection {
      type        = "ssh"
      user        = "root"
      host        = "192.168.1.200"
      agent       = false
      private_key = file("/ssh/id_ed25519_proxmox_root")
    }
  }
}

resource "proxmox_vm_qemu" "vm" {
  for_each = var.vm_configs

  lifecycle {
    ignore_changes = [
      disk,
      startup_shutdown
    ]
  }

  name        = each.value.name
  target_node = each.value.target_node
  tags        = each.value.tags

  vm_state   = each.value.vm_state
  vmid       = each.value.vmid
  clone      = "ubuntu-24-cloudinit"

  full_clone         = true
  force_create       = true
  automatic_reboot   = true
  start_at_node_boot = true
  protection         = false

  os_type  = "cloud-init"
  scsihw   = "virtio-scsi-single"
  bios     = "seabios"
  bootdisk = "scsi0"

  memory  = each.value.memory
  balloon = each.value.minimum_memory

  cpu {
    type    = "host"
    cores   = each.value.cores
    sockets = 1
  }

  serial {
    id   = 0
    type = "socket"
  }

  vga {
    type   = "serial0"
    memory = 16
  }

  ipconfig0 = each.value.ipconfig

  network {
    id     = 0
    model  = "virtio"
    bridge = "vmbr0"
  }

  ci_wait   = 30
  ciupgrade = true
  cicustom  = "user=local:snippets/cloudinit-ansible-${each.value.vmid}.yaml"

  disk {
    type    = "cloudinit"
    slot    = "ide2"
    storage = "local-zfs"
  }

  disk {
    type    = "disk"
    slot    = "scsi0"
    size    = each.value.boot_disk.size
    storage = each.value.boot_disk.storage
  }

  dynamic "disk" {
    for_each = each.value.passthrough_disk != null ? [each.value.passthrough_disk] : []
    content {
      type        = "disk"
      slot        = "scsi2"
      disk_file   = each.value.passthrough_disk.disk_file
      passthrough = true
    }
  }
}
