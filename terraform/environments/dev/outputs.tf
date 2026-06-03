output "es_endpoint" {
  description = "Elasticsearch endpoint - set as ES_HOST in your app"
  value       = module.elasticsearch.es_endpoint
}

output "es_instance_id" {
  description = "EC2 instance ID (use for SSM session: aws ssm start-session --target <id>)"
  value       = module.elasticsearch.instance_id
}

output "vpc_id" {
  value = module.networking.vpc_id
}
