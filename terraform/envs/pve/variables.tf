variable proxmox_api_url {
  type      = string
  sensitive = true
}

variable proxmox_api_token_id {
  type      = string
  sensitive = true
}

variable proxmox_api_token {
  type      = string
  sensitive = true
}

variable "ansible_pub_key" {
  type      = string
  sensitive = true
}

variable "root_pub_key" {
  type      = string
  sensitive = true
}

variable "vm_configs" {
  type = map(object({
    name           = string
    target_node    = optional(string, "pve")
    vm_state       = optional(string, "started")
    vmid           = number
    memory         = number
    minimum_memory = number
    cores          = number
    ipconfig       = string
    tags           = optional(string, "vm")

    boot_disk = object({
      size    = string
      storage = string
    })

    passthrough_disk = optional(object({
      disk_file = string
    }))
  }))

  default = {

    image_nfs = {
      name           = "image-nfs"
      tags           = "vm,media,images"
      vmid           = 8212
      memory         = 2048
      minimum_memory = 512
      cores          = 2
      boot_disk      = {size = "16G", storage = "local-zfs"}
      passthrough_disk = {disk_file = "bulk-zfs:vm-8512-disk-0"}
      ipconfig       = "ip=192.168.1.212/24,gw=192.168.1.1"
    }

    file_nfs = {
      name           = "file-nfs"
      tags           = "vm,media,files"
      vmid           = 8216
      memory         = 2048
      minimum_memory = 512
      cores          = 2
      boot_disk      = {size = "16G", storage = "local-zfs"}
      passthrough_disk = {disk_file = "bulk-zfs:vm-8516-disk-0"}
      ipconfig       = "ip=192.168.1.216/24,gw=192.168.1.1"
    }

    minio = {
      name           = "minio"
      tags           = "vm,media,objects,s3"
      vmid           = 8220
      memory         = 2048
      minimum_memory = 512
      cores          = 2
      boot_disk      = {size = "16G", storage = "local-zfs"}
      passthrough_disk = {disk_file = "bulk-zfs:vm-8520-disk-0"}
      ipconfig       = "ip=192.168.1.220/24,gw=192.168.1.1"
    }

    local_dns = {
      name           = "local-dns"
      tags           = "vm,network"
      vmid           = 8207
      memory         = 1024
      minimum_memory = 512
      cores          = 2
      boot_disk      = {size = "16G", storage = "local-zfs"}
      ipconfig       = "ip=192.168.1.207/24,gw=192.168.1.1"
    }

    firefly = {
      name           = "firefly"
      tags           = "vm,eco"
      vmid           = 8210
      memory         = 2048
      minimum_memory = 1024
      cores          = 2
      boot_disk      = {size = "16G", storage = "local-zfs"}
      passthrough_disk = {disk_file = "bulk-zfs:vm-8510-disk-0"}
      ipconfig       = "ip=192.168.1.210/24,gw=192.168.1.1"
    }

    video_nfs = {
      name           = "video-nfs"
      tags           = "vm,media,video"
      vmid           = 8218
      memory         = 2048
      minimum_memory = 512
      cores          = 2
      boot_disk      = {size = "16G", storage = "local-zfs"}
      passthrough_disk = {disk_file = "bulk-zfs:vm-8518-disk-0"}
      ipconfig       = "ip=192.168.1.218/24,gw=192.168.1.1"
    }

    music_nfs = {
      name           = "music-nfs"
      tags           = "vm,media,music"
      vmid           = 8221
      memory         = 2048
      minimum_memory = 512
      cores          = 2
      boot_disk      = {size = "16G", storage = "local-zfs"}
      passthrough_disk = {disk_file = "bulk-zfs:vm-8521-disk-0"}
      ipconfig       = "ip=192.168.1.221/24,gw=192.168.1.1"
    }

    k3s_master = {
      name           = "k3s-master"
      tags           = "vm,k3s,cluster,arc"
      vmid           = 8214
      memory         = 2048
      minimum_memory = 1024
      cores          = 2
      boot_disk      = {size = "32G", storage = "local-zfs"}
      ipconfig       = "ip=192.168.1.214/24,gw=192.168.1.1"
    }

    k3s_worker_1 = {
      name           = "k3s-worker-1"
      tags           = "vm,k3s,cluster,arc"
      vmid           = 8215
      memory         = 16384
      minimum_memory = 16384
      cores          = 12
      boot_disk      = {size = "400G", storage = "local-zfs"}
      ipconfig       = "ip=192.168.1.215/24,gw=192.168.1.1"
    }

  }
}
