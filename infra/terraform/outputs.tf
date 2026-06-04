# CascadeGuard MCP Proxy Middleware - Terraform Outputs

output "cluster_arn" {
  description = "ECS Cluster ARN"
  value       = aws_ecs_cluster.proxy.arn
}

output "service_name" {
  description = "ECS Service name"
  value       = aws_ecs_service.proxy.name
}

output "nlb_dns_name" {
  description = "Internal NLB DNS name (for VPC consumers)"
  value       = aws_lb.proxy.dns_name
}

output "vpc_endpoint_service_id" {
  description = "VPC Endpoint Service ID for PrivateLink consumers"
  value       = aws_vpc_endpoint_service.proxy.id
}

output "vpc_endpoint_service_name" {
  description = "VPC Endpoint Service Name for cross-account access"
  value       = aws_vpc_endpoint_service.proxy.service_name
}

output "security_group_id" {
  description = "Proxy security group ID"
  value       = aws_security_group.proxy.id
}

output "log_group_name" {
  description = "CloudWatch Log Group name"
  value       = aws_cloudwatch_log_group.proxy.name
}

output "task_definition_arn" {
  description = "ECS Task Definition ARN"
  value       = aws_ecs_task_definition.proxy.arn
}
