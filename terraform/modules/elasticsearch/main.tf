# Reasoning Layer - Elasticsearch Module
# ---------------------------------------
# Provisions a single-node Elasticsearch cluster on EC2.
# Designed for the Reasoning Layer's semantic memory node.
#
# Usage:
#   module "elasticsearch" {
#     source        = "../../modules/elasticsearch"
#     instance_type = "t3.medium"
#     vpc_id        = module.networking.vpc_id
#     subnet_id     = module.networking.private_subnet_id
#     allowed_cidr  = "10.0.0.0/16"
#   }

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# ---------------------------------------------------------------------------
# Security Group - only allow ES traffic from within the VPC
# ---------------------------------------------------------------------------

resource "aws_security_group" "elasticsearch" {
  name        = "${var.name_prefix}-es-sg"
  description = "Reasoning Layer Elasticsearch - internal access only"
  vpc_id      = var.vpc_id

  ingress {
    description = "Elasticsearch REST API"
    from_port   = 9200
    to_port     = 9200
    protocol    = "tcp"
    cidr_blocks = [var.allowed_cidr]
  }

  ingress {
    description = "Elasticsearch cluster comms"
    from_port   = 9300
    to_port     = 9300
    protocol    = "tcp"
    cidr_blocks = [var.allowed_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-es-sg"
  })
}

# ---------------------------------------------------------------------------
# IAM Role - EC2 instance profile (SSM access, no SSH needed)
# ---------------------------------------------------------------------------

resource "aws_iam_role" "elasticsearch" {
  name = "${var.name_prefix}-es-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.elasticsearch.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "elasticsearch" {
  name = "${var.name_prefix}-es-profile"
  role = aws_iam_role.elasticsearch.name
}

# ---------------------------------------------------------------------------
# EBS volume for ES data (separate from root - survives instance replacement)
# ---------------------------------------------------------------------------

resource "aws_ebs_volume" "elasticsearch_data" {
  availability_zone = var.availability_zone
  size              = var.data_volume_gb
  type              = "gp3"
  encrypted         = true

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-es-data"
  })
}

# ---------------------------------------------------------------------------
# EC2 instance
# ---------------------------------------------------------------------------

resource "aws_instance" "elasticsearch" {
  ami                    = var.ami_id
  instance_type          = var.instance_type
  subnet_id              = var.subnet_id
  vpc_security_group_ids = [aws_security_group.elasticsearch.id]
  iam_instance_profile   = aws_iam_instance_profile.elasticsearch.name

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
    encrypted   = true
  }

  user_data = templatefile("${path.module}/userdata.sh.tpl", {
    es_version   = var.elasticsearch_version
    data_device  = "/dev/xvdf"
    data_mount   = "/var/lib/elasticsearch"
    heap_size    = var.heap_size
    cluster_name = "${var.name_prefix}-cluster"
  })

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-elasticsearch"
  })

  lifecycle {
    ignore_changes = [ami]
  }
}

resource "aws_volume_attachment" "elasticsearch_data" {
  device_name = "/dev/xvdf"
  volume_id   = aws_ebs_volume.elasticsearch_data.id
  instance_id = aws_instance.elasticsearch.id
}
