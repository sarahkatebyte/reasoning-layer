variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "ami_id" {
  description = "Amazon Linux 2023 AMI ID. Get with: aws ec2 describe-images --owners amazon --filters 'Name=name,Values=al2023-ami-*-x86_64' --query 'sort_by(Images, &CreationDate)[-1].ImageId' --output text"
  type        = string
}
