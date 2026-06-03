variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "ami_id" {
  description = "Amazon Linux 2023 AMI ID for your region. Get with: aws ec2 describe-images --owners amazon --filters 'Name=name,Values=al2023-ami-*-x86_64' --query 'sort_by(Images, &CreationDate)[-1].ImageId' --output text"
  type        = string
}

variable "es_instance_type" {
  description = "EC2 instance type for Elasticsearch"
  type        = string
  default     = "t3.large"  # 2 vCPU, 8GB - good for prod single-node
}

variable "es_heap_size" {
  description = "JVM heap size - set to ~50% of instance RAM"
  type        = string
  default     = "4g"  # 4g heap for t3.large (8GB RAM)
}

variable "es_data_volume_gb" {
  description = "EBS data volume size in GB"
  type        = number
  default     = 100
}
