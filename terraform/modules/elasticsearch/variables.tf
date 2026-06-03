variable "name_prefix" {
  description = "Prefix for all resource names (e.g. 'reasoning-layer-dev')"
  type        = string
}

variable "vpc_id" {
  description = "VPC to deploy into"
  type        = string
}

variable "subnet_id" {
  description = "Private subnet for the ES instance"
  type        = string
}

variable "availability_zone" {
  description = "AZ for the EBS data volume (must match subnet)"
  type        = string
}

variable "allowed_cidr" {
  description = "CIDR block allowed to reach Elasticsearch (e.g. your VPC CIDR)"
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type"
  type        = string
  default     = "t3.medium"  # 2 vCPU, 4GB - enough for dev/staging
}

variable "ami_id" {
  description = "Amazon Linux 2023 AMI ID (region-specific - get from AWS console)"
  type        = string
}

variable "elasticsearch_version" {
  description = "Elasticsearch version to install"
  type        = string
  default     = "8.13.0"
}

variable "heap_size" {
  description = "JVM heap size for Elasticsearch (set to ~50% of instance RAM)"
  type        = string
  default     = "2g"
}

variable "data_volume_gb" {
  description = "Size of the EBS data volume in GB"
  type        = number
  default     = 50
}

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default     = {}
}
