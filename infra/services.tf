# The APIs this configuration uses. They stay enabled if this is destroyed: turning an API off can
# break things that were not created here, and this project is shared with the portfolio site.
locals {
  services = [
    "artifactregistry.googleapis.com",     # the container registry
    "cloudresourcemanager.googleapis.com", # reading the project, IAM on it
    "cloudscheduler.googleapis.com",       # the schedule that starts the job
    "firebasehosting.googleapis.com",      # the custom domain on the Hosting site
    "iam.googleapis.com",                  # service accounts, the workload identity provider
    "iamcredentials.googleapis.com",       # short-lived credentials for GitHub Actions and Scheduler
    "monitoring.googleapis.com",           # alerts and uptime checks
    "run.googleapis.com",                  # the refresh job
    "serviceusage.googleapis.com",         # enabling the others
    "storage.googleapis.com",              # the state bucket and the public bucket
    "sts.googleapis.com",                  # exchanging GitHub's token for a Google one
  ]
}

resource "google_project_service" "this" {
  for_each = toset(local.services)

  project = var.project_id
  service = each.value

  disable_on_destroy = false
}

data "google_project" "this" {
  project_id = var.project_id
}
