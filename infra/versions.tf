terraform {
  required_version = ">= 1.5"

  # State bucket bootstrapped outside terraform (versioned, private, no Object
  # Lock — unlike the datalake, state must stay mutable):
  #   aws s3api create-bucket --bucket reg42-clhear-tfstate --region us-east-1
  backend "s3" {
    bucket = "reg42-clhear-tfstate"
    key    = "clhear/terraform.tfstate"
    region = "us-east-1"
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Project   = "clhear"
      ManagedBy = "terraform"
    }
  }
}
