# Persisted so deploy_webui.sh / unscoped applies do not destroy the workers
# or roll the explorer back to an older zip.
worker_image                = "730649732189.dkr.ecr.us-east-1.amazonaws.com/clhear-workers:latest"
existing_vpc_id             = "vpc-058e97e7bc4fc7ceb"
existing_private_subnet_ids = ["subnet-0a5312ab914fbdc56", "subnet-0eb0ad6df81f6108a"]
worker_assign_public_ip     = true
schedules_enabled           = true
clhear_hostname             = "clhear.reg42.ai"
webui_db_key                = "webui/clhear-latest.db"
webui_zip_key               = "webui/webui-20260831T075538Z.zip"
webui_zip_sha256            = "CHNvfgCVn8iU816qcJ4wVxmNrh4KdTfCi3+ANJ0S1mk="
