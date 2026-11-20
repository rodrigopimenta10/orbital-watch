# The address visitors use, connected to the existing Hosting site.
#
# The site itself (rodrigopimenta-orbital-watch) is created by the portfolio's Terraform and is
# only named here. What this adds is the custom domain on it. Firebase then says which DNS
# records it needs (the dns_records output); the zone is at Cloudflare and that record is changed
# by hand, once.
resource "google_firebase_hosting_custom_domain" "orbital" {
  provider = google-beta

  project       = var.project_id
  site_id       = var.hosting_site_id
  custom_domain = var.custom_domain

  # Do not hold the apply open until DNS has been changed and verified. That happens later, by
  # hand, and until it does the domain simply reports that it is waiting.
  wait_dns_verification = false

  depends_on = [google_project_service.this]
}
