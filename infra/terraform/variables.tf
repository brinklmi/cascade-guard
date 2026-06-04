# CascadeGuard MCP Proxy Middleware - Terraform Variables

variable "vpc_id" {
  description = "VPC ID where the proxy will be deployed"
  type        = string
}

variable "subnet_ids" {
  description = "Private subnet IDs for ECS tasks (minimum 2 for HA)"
  type        = list(string)
}

variable "container_image" {
  description = "ECR image URI for the MCP proxy container"
  type        = string
}

variable "desired_count" {
  description = "Number of ECS tasks to run"
  type        = number
  default     = 2
}

variable "cpu_units" {
  description = "CPU units for each task (1024 = 1 vCPU)"
  type        = number
  default     = 2048
}

variable "memory_mb" {
  description = "Memory in MB for each task"
  type        = number
  default     = 4096
}

variable "proxy_port" {
  description = "Port the MCP proxy listens on"
  type        = number
  default     = 8080
}

variable "max_velocity" {
  description = "CascadeGuard max delegation velocity"
  type        = number
  default     = 50
}

variable "depth_limit" {
  description = "Maximum delegation chain depth"
  type        = number
  default     = 10
}

variable "fanout_limit" {
  description = "Maximum delegation fanout per agent"
  type        = number
  default     = 20
}

variable "preservation_threshold" {
  description = "Impedance threshold for preservation mode (0.0-1.0)"
  type        = number
  default     = 0.3
}

variable "token_budget" {
  description = "System-wide token budget"
  type        = number
  default     = 1000000
}

variable "drain_timeout_seconds" {
  description = "Graceful shutdown drain timeout in seconds"
  type        = number
  default     = 30
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days"
  type        = number
  default     = 30
}

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default = {
    Project   = "CascadeGuard"
    Component = "MCP-Proxy"
    ManagedBy = "Terraform"
  }
}
