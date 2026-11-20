terraform {
  required_version = ">= 1.17.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.12"
    }
    # The Firebase Hosting custom domain exists only in google-beta.
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 8.12"
    }
  }

  # State in the same Cloud Storage bucket the portfolio's Terraform uses, under its own prefix,
  # so the two configurations never read or lock each other's state. The bucket is named at init
  # time because its name contains the project ID:
  #   terraform init -backend-config="bucket=<project-id>-tfstate"
  backend "gcs" {
    prefix = "orbital-watch"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region

  # API calls are billed to and quota-checked against this project, not the caller's default one.
  billing_project       = var.project_id
  user_project_override = true
}

provider "google-beta" {
  project = var.project_id
  region  = var.region

  billing_project       = var.project_id
  user_project_override = true
}
