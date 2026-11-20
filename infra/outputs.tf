# What the deploy workflow needs, as GitHub repository variables. None of them is a secret: each
# is a name, and a name is useless without a token GitHub issues only to this repository's
# workflow runs.

output "repository_variables" {
  description = "Set these as Actions variables on the repository (gh variable set NAME --body VALUE)."
  value = {
    GCP_PROJECT_ID                 = var.project_id
    GCP_REGION                     = var.region
    GCP_WORKLOAD_IDENTITY_PROVIDER = google_iam_workload_identity_pool_provider.orbital_watch.name
    GCP_DEPLOY_SERVICE_ACCOUNT     = google_service_account.deploy.email
    IMAGE_REPOSITORY               = "${google_artifact_registry_repository.orbital_watch.location}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.orbital_watch.repository_id}"
    REFRESH_JOB                    = google_cloud_run_v2_job.refresh.name
    LIVE_DOCUMENT_URL              = local.document_url
    FIREBASE_HOSTING_SITE          = var.hosting_site_id
  }
}

output "live_document_url" {
  description = "Where the job publishes and the page reads."
  value       = local.document_url
}

output "hosting_default_url" {
  description = "Where the site is served whether or not the custom domain is connected."
  value       = "https://${var.hosting_site_id}.web.app"
}

output "dns_records" {
  description = "The DNS records Firebase Hosting wants for the custom domain, as it reports them. Create the 'desired' ones at Cloudflare (DNS only, not proxied) and remove any it lists under 'discovered' that conflict."
  value       = google_firebase_hosting_custom_domain.orbital.required_dns_updates
}

output "custom_domain_state" {
  description = "How far the custom domain has got: ownership of the name, whether DNS points at Hosting, and the certificate."
  value = {
    ownership = google_firebase_hosting_custom_domain.orbital.ownership_state
    host      = google_firebase_hosting_custom_domain.orbital.host_state
    cert      = google_firebase_hosting_custom_domain.orbital.cert
  }
}
