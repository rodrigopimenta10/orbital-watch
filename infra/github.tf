# Keyless sign-in for this repository's deploy workflow (Workload Identity Federation). GitHub
# gives each workflow run a short-lived token saying which repository and workflow it is; Google
# Cloud exchanges it for credentials of the deploy service account below. No key is created,
# stored or rotated.
#
# The pool is the portfolio's ("github", created by the portfolio-website repository's Terraform).
# This adds one provider to it, for this repository only.

data "google_iam_workload_identity_pool" "github" {
  project                   = var.project_id
  workload_identity_pool_id = var.workload_identity_pool_id

  lifecycle {
    # Read only to find out, at plan time, that the pool this depends on is there and usable.
    postcondition {
      condition     = self.disabled == false
      error_message = "The workload identity pool is disabled. It is managed by the portfolio-website repository's Terraform."
    }
  }
}

locals {
  # The pool's full name, with the project *number*: principals are only recognised in that form.
  github_pool = "projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${data.google_iam_workload_identity_pool.github.workload_identity_pool_id}"
}

resource "google_iam_workload_identity_pool_provider" "orbital_watch" {
  project                            = var.project_id
  workload_identity_pool_id          = data.google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "orbital-watch"
  display_name                       = "orbital-watch"
  description                        = "Tokens GitHub issues to workflow runs of ${var.github_repository}"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }

  attribute_mapping = {
    "google.subject"         = "assertion.sub"
    "attribute.repository"   = "assertion.repository"
    "attribute.ref"          = "assertion.ref"
    "attribute.workflow_ref" = "assertion.workflow_ref"
  }

  # Tokens from any other repository are refused here, before any service account is considered.
  # The owner is checked by number as well as by name: a name can change hands, the number cannot.
  attribute_condition = "assertion.repository == '${var.github_repository}' && assertion.repository_owner_id == '${var.github_owner_id}'"

  depends_on = [google_project_service.this]
}

# --- The deploy account: only the deploy workflow, only on main.

resource "google_service_account" "deploy" {
  project      = var.project_id
  account_id   = "orbital-watch-deploy"
  display_name = "Orbital Watch deploy (GitHub Actions, main)"
  description  = "Pushes the job image, updates and starts the refresh job, deploys the Hosting site. Usable only by deploy.yml on main."

  depends_on = [google_project_service.this]
}

# A principal in the shared pool is identified by attribute, and both repositories' providers map
# the same attribute names. What keeps the portfolio's workflows from using this account is that
# the workflow_ref below starts with this repository's name, which only a token GitHub issued to
# this repository can carry.
resource "google_service_account_iam_member" "deploy_from_main" {
  service_account_id = google_service_account.deploy.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${local.github_pool}/attribute.workflow_ref/${var.github_repository}/.github/workflows/deploy.yml@refs/heads/main"
}

# 1. Push images, to this one repository.
resource "google_artifact_registry_repository_iam_member" "deploy_pushes_images" {
  project    = google_artifact_registry_repository.orbital_watch.project
  location   = google_artifact_registry_repository.orbital_watch.location
  repository = google_artifact_registry_repository.orbital_watch.name
  role       = "roles/artifactregistry.writer"
  member     = google_service_account.deploy.member
}

# 2. Update and start this one job. Granted on the job, not on the project, so it reaches no
#    other Cloud Run resource -- in particular not the portfolio's status service.
resource "google_cloud_run_v2_job_iam_member" "deploy_updates_job" {
  project  = google_cloud_run_v2_job.refresh.project
  location = google_cloud_run_v2_job.refresh.location
  name     = google_cloud_run_v2_job.refresh.name
  role     = "roles/run.developer"
  member   = google_service_account.deploy.member
}

#    Updating the job re-attaches its runtime identity, so the deployer must be allowed to hand
#    the job that identity, and only that one.
resource "google_service_account_iam_member" "deploy_acts_as_runtime" {
  service_account_id = google_service_account.refresh.name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.deploy.member
}

# 3. Deploy to Firebase Hosting.
#
#    Firebase has no role narrower than "Hosting admin", and it is granted on the project, so it
#    covers the portfolio's Hosting site as well as this one. That is the one permission here
#    wider than it should be, and there is no way to narrow it; the account is usable only from
#    deploy.yml on main of this repository, which is what limits it in practice.
locals {
  hosting_deploy_roles = [
    "roles/firebasehosting.admin",      # create releases on a Hosting site
    "roles/serviceusage.apiKeysViewer", # the Firebase CLI reads the project's web configuration
  ]
}

resource "google_project_iam_member" "deploy_publishes_site" {
  for_each = toset(local.hosting_deploy_roles)

  project = var.project_id
  role    = each.value
  member  = google_service_account.deploy.member
}
