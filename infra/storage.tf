# Two buckets, with opposite jobs.
#
#   state   private. What the job remembers between runs: per source, the last good payload and
#           when upstream was last asked. This is what makes "ask Celestrak at most once every two
#           hours" a fact about the system rather than about a schedule.
#   public  world-readable. Exactly one object, latest.json, which the page fetches.
#
# They are separate buckets rather than two prefixes in one so that "public" is a property of a
# whole bucket: there is no object-level setting that could expose state by mistake.

locals {
  state_bucket  = "${var.project_id}-orbital-watch-state"
  public_bucket = "${var.project_id}-orbital-watch-public"

  # Every origin a browser may read the published document from.
  site_origins = concat(
    [
      "https://${var.custom_domain}",
      "https://${var.hosting_site_id}.web.app",
      "https://${var.hosting_site_id}.firebaseapp.com",
    ],
    var.development_origins,
  )
}

resource "google_storage_bucket" "state" {
  project  = var.project_id
  name     = local.state_bucket
  location = var.region

  # Access is decided by IAM on the bucket alone, and public access is ruled out at the bucket
  # itself, whatever IAM says later.
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  # The job rewrites these objects every few minutes. Versioning keeps what was overwritten, so a
  # state object damaged by a bad release can be read back or restored; the rule below keeps that
  # history to a week, which is a few megabytes.
  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      with_state                 = "ARCHIVED"
      days_since_noncurrent_time = 7
    }
    action {
      type = "Delete"
    }
  }

  # Versioning already covers recovery. Soft delete on top would keep, and bill for, a second copy
  # of every overwritten object.
  soft_delete_policy {
    retention_duration_seconds = 0
  }

  depends_on = [google_project_service.this]
}

resource "google_storage_bucket" "public" {
  project  = var.project_id
  name     = local.public_bucket
  location = var.region

  uniform_bucket_level_access = true
  # "inherited" rather than "enforced": this is the one bucket that is meant to be read by anyone.
  public_access_prevention = "inherited"

  # The page is on another origin (Firebase Hosting), so the browser will only hand it the
  # document if the bucket says that origin may read it. GET and HEAD, from the site's own
  # addresses and a developer's localhost, and nothing else.
  cors {
    origin          = local.site_origins
    method          = ["GET", "HEAD"]
    response_header = ["Content-Type", "Cache-Control", "ETag", "Last-Modified"]
    max_age_seconds = 3600
  }

  # Nothing here is worth recovering: the one object is rewritten every few minutes from state.
  soft_delete_policy {
    retention_duration_seconds = 0
  }

  depends_on = [google_project_service.this]
}

# Anyone may read objects in the public bucket. The legacy reader role is used because it grants
# reading an object and nothing more; the usual "object viewer" would also let anyone list the
# bucket's contents.
resource "google_storage_bucket_iam_member" "public_read" {
  bucket = google_storage_bucket.public.name
  role   = "roles/storage.legacyObjectReader"
  member = "allUsers"
}
