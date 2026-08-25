# Persisted so deploy_webui.sh / unscoped applies do not destroy the workers.
worker_image               = "730649732189.dkr.ecr.us-east-1.amazonaws.com/clhear-workers:latest"
existing_vpc_id            = "vpc-058e97e7bc4fc7ceb"
existing_private_subnet_ids = ["subnet-0a5312ab914fbdc56", "subnet-0eb0ad6df81f6108a"]
worker_assign_public_ip    = true
schedules_enabled          = true
