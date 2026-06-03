output "vpc_id" {
  value = aws_vpc.main.id
}

output "private_subnet_id" {
  value = aws_subnet.private.id
}

output "public_subnet_id" {
  value = aws_subnet.public.id
}

output "availability_zone" {
  value = var.availability_zone
}

output "vpc_cidr" {
  value = var.vpc_cidr
}
