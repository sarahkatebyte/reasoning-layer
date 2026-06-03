output "instance_id" {
  description = "EC2 instance ID"
  value       = aws_instance.elasticsearch.id
}

output "private_ip" {
  description = "Private IP of the Elasticsearch instance"
  value       = aws_instance.elasticsearch.private_ip
}

output "es_endpoint" {
  description = "Elasticsearch endpoint (internal)"
  value       = "http://${aws_instance.elasticsearch.private_ip}:9200"
}

output "security_group_id" {
  description = "Security group ID - add to your app's inbound rules"
  value       = aws_security_group.elasticsearch.id
}
