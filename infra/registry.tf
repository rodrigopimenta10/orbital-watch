# Where the deploy workflow pushes the refresh job's image. The first 0.5 GB is free across the
# project; the cleanup rules keep this repository to a handful of images.
resource "google_artifact_registry_repository" "orbital_watch" {
  project       = var.project_id
  location      = var.region
  repository_id = "orbital-watch"
  description   = "Container images for the Orbital Watch refresh job"
  format        = "DOCKER"

  cleanup_policies {
    id     = "keep-latest-five"
    action = "KEEP"

    most_recent_versions {
      keep_count = 5
    }
  }

  cleanup_policies {
    id     = "delete-the-rest-after-a-week"
    action = "DELETE"

    condition {
      older_than = "604800s" # seven days
    }
  }

  depends_on = [google_project_service.this]
}
