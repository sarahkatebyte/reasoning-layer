# Reasoning Layer - Production Environment
# ------------------------------------------
# Wires networking + elasticsearch modules into a production deployment.
#
# Prerequisites:
#   1. Create an S3 bucket for state:
#      aws s3api create-bucket --bucket reasoning-layer-tfstate --region us-east-1
#   2. Enable versioning on the bucket:
#      aws s3api put-bucket-versioning --bucket reasoning-layer-tfstate \
#        --versioning-configuration Status=Enabled
#   3. Get your Amazon Linux 2023 AMI ID for your region:
#      aws ec2 describe-images --owners amazon \
#        --filters "Name=name,Values=al2023-ami-*-x86_64" \
#        --query 'sort_by(Images, &CreationDate)[-1].ImageId' \
#        --output text
#   4. Fill in terraform.tfvars (copy from terraform.tfvars.example)
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

  # Remote state - prevents drift, enables team use
  backend "s3" {
    bucket         = "reasoning-layer-tfstate"
    key            = "prod/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    use_lockfile   = true  # native S3 locking (TF 1.10+), no DynamoDB needed
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "reasoning-layer"
      Environment = "prod"
      ManagedBy   = "terraform"
    }
  }
}

# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

module "networking" {
  source = "../../modules/networking"

  name_prefix         = "reasoning-layer-prod"
  vpc_cidr            = "10.0.0.0/16"
  private_subnet_cidr = "10.0.1.0/24"
  public_subnet_cidr  = "10.0.2.0/24"
  availability_zone   = "${var.aws_region}a"

  tags = {
    Project     = "reasoning-layer"
    Environment = "prod"
  }
}

# ---------------------------------------------------------------------------
# Elasticsearch
# ---------------------------------------------------------------------------

module "elasticsearch" {
  source = "../../modules/elasticsearch"

  name_prefix           = "reasoning-layer-prod"
  vpc_id                = module.networking.vpc_id
  subnet_id             = module.networking.private_subnet_id
  availability_zone     = module.networking.availability_zone
  allowed_cidr          = "10.0.0.0/16"   # VPC-only access

  ami_id                = var.ami_id
  instance_type         = var.es_instance_type
  elasticsearch_version = "8.13.0"
  heap_size             = var.es_heap_size
  data_volume_gb        = var.es_data_volume_gb

  tags = {
    Project     = "reasoning-layer"
    Environment = "prod"
  }
}
