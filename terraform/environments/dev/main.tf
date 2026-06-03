# Reasoning Layer - Dev Environment
# ------------------------------------
# Lightweight single-node setup for local development and experimentation.
# Use docker compose instead if you just want to run it locally.
# This is for when you want a real AWS env to test against.
#
# Deploy:
#   terraform init
#   terraform plan
#   terraform apply

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    bucket       = "reasoning-layer-tfstate"
    key          = "dev/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "reasoning-layer"
      Environment = "dev"
      ManagedBy   = "terraform"
    }
  }
}

module "networking" {
  source = "../../modules/networking"

  name_prefix         = "reasoning-layer-dev"
  vpc_cidr            = "10.1.0.0/16"
  private_subnet_cidr = "10.1.1.0/24"
  public_subnet_cidr  = "10.1.2.0/24"
  availability_zone   = "${var.aws_region}a"

  tags = {
    Project     = "reasoning-layer"
    Environment = "dev"
  }
}

module "elasticsearch" {
  source = "../../modules/elasticsearch"

  name_prefix           = "reasoning-layer-dev"
  vpc_id                = module.networking.vpc_id
  subnet_id             = module.networking.private_subnet_id
  availability_zone     = module.networking.availability_zone
  allowed_cidr          = "10.1.0.0/16"

  ami_id                = var.ami_id
  instance_type         = "t3.small"   # 2 vCPU, 2GB - cheapest viable ES node
  elasticsearch_version = "8.13.0"
  heap_size             = "1g"
  data_volume_gb        = 20

  tags = {
    Project     = "reasoning-layer"
    Environment = "dev"
  }
}
