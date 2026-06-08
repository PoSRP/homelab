variable proxmox_api_url {
  type = string
}

variable proxmox_api_token_id {
  type = string
}

variable proxmox_api_token {
  type = string
}

variable "vm_configs" {
  type = map(object({
    name        = string
    target_node = optional(string, "pve")
    vmid        = number
    tags        = optional(string, "data")
    data_disk   = object({
      size    = string
      storage = optional(string, "bulk-zfs")
    })
  }))

  default = {

    image_nfs_data = {
      name      = "image-nfs-data"
      tags      = "data,media,images"
      vmid      = 8512
      data_disk = {size = "512G"}
    }

    file_nfs_data = {
      name      = "file-nfs-data"
      tags      = "data,media,files"
      vmid      = 8516
      data_disk = {size = "512G"}
    }

    minio_data = {
      name      = "minio-data"
      tags      = "data,media,objects,s3"
      vmid      = 8520
      data_disk = {size = "512G"}
    }

    firefly_data = {
      name      = "firefly-data"
      tags      = "data,eco"
      vmid      = 8510
      data_disk = {size = "128G"}
    }

    video_nfs_data = {
      name      = "video-nfs-data"
      tags      = "data,media,video"
      vmid      = 8518
      data_disk = {size = "512G"}
    }

    music_nfs_data = {
      name      = "music-nfs-data"
      tags      = "data,media,music"
      vmid      = 8521
      data_disk = {size = "512G"}
    }

    grafana_data = {
      name      = "grafana-data"
      tags      = "data,timeseries,visualization"
      vmid      = 8508
      data_disk = {size = "256G"}
    }

  }
}
