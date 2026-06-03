# Reasoning Layer - Staging Environment
# ----------------------------------------
# Production-like setup for pre-release validation.
# Same config as prod, smaller instance sizes.
# Use this to validate Terraform changes before applying to prod.
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
    key          = "staging/terraform.tfstate"
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
      Environment = "staging"
      ManagedBy   = "terraform"
    }
  }
}

module "networking" {
  source = "../../modules/networking"

  name_prefix         = "reasoning-layer-staging"
  vpc_cidr            = "10.2.0.0/16"
  private_subnet_cidr = "10.2.1.0/24"
  public_subnet_cidr  = "10.2.2.0/24"
  availability_zone   = "${var.aws_region}a"

  tags = {
    Project     = "reasoning-layer"
    Environment = "staging"
  }
}

module "elasticsearch" {
  source = "../../modules/elasticsearch"

  name_prefix           = "reasoning-layer-staging"
  vpc_id                = module.networking.vpc_id
  subnet_id             = module.networking.private_subnet_id
  availability_zone     = module.networking.availability_zone
  allowed_cidr          = "10.2.0.0/16"

  ami_id                = var.ami_id
  instance_type         = "t3.medium"  # 2 vCPU, 4GB - mirrors prod shape, smaller
  elasticsearch_version = "8.13.0"
  heap_size             = "2g"
  data_volume_gb        = 50

  tags = {
    Project     = "reasoning-layer"
    Environment = "staging"
  }
}
