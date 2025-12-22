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

  vm_state     = "stopped"
  vmid         = each.value.vmid
  clone        = "nosys-data"
  full_clone   = true
  force_create = true

  start_at_node_boot = false
  automatic_reboot   = false
  protection         = false

  os_type = "ubuntu"
  scsihw  = "virtio-scsi-single"
  bios    = "seabios"

  memory  = 16
  balloon = 16

  cpu {
    type    = "host"
    cores   = 1
    sockets = 1
  }

  ipconfig0 = ""

  network {
    id     = 0
    model  = "virtio"
    bridge = "vmbr0"
  }

  disk {
    type    = "disk"
    slot    = "scsi2"
    size    = each.value.data_disk.size
    storage = each.value.data_disk.storage
  }
}
