# The refresh job: a container that runs one cycle (fetch what is due, propagate, evaluate health,
# publish one document) and exits. Nothing listens for requests; between runs nothing is running.

# Its own identity, allowed to read and write objects in its two buckets and nothing else. It
# holds no project-level role.
resource "google_service_account" "refresh" {
  project      = var.project_id
  account_id   = "orbital-watch-refresh"
  display_name = "Orbital Watch refresh job (runtime)"
  description  = "Identity the Cloud Run job orbital-watch-refresh runs as. Object access to its two buckets only."

  depends_on = [google_project_service.this]
}

# Object user: read, create, and replace objects. Replacing an object counts as deleting the old
# one, which is why the narrower "object creator" is not enough.
resource "google_storage_bucket_iam_member" "refresh_uses_state" {
  bucket = google_storage_bucket.state.name
  role   = "roles/storage.objectUser"
  member = google_service_account.refresh.member
}

resource "google_storage_bucket_iam_member" "refresh_publishes" {
  bucket = google_storage_bucket.public.name
  role   = "roles/storage.objectUser"
  member = google_service_account.refresh.member
}

resource "google_cloud_run_v2_job" "refresh" {
  project  = var.project_id
  name     = "orbital-watch-refresh"
  location = var.region

  template {
    # One task, never two at once. Overlapping executions are still possible (a manual run during
    # a scheduled one) and the job tolerates them: every write to either bucket is conditional on
    # the object not having changed since it was read.
    task_count  = 1
    parallelism = 1

    template {
      service_account = google_service_account.refresh.email

      # No retry. A failed execution means the job could not publish at all, and that should reach
      # the alert as a failure, not be absorbed. The next scheduled run is the retry, and it is
      # minutes away.
      max_retries = 0

      # The job gives itself 180 seconds from start to published document. This is the hard stop
      # above that, and it is still well under the cadence, so runs do not pile up.
      timeout = "300s"

      containers {
        image = var.job_image

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }

        env {
          name  = "ORBITAL_WATCH_STATE_BUCKET"
          value = google_storage_bucket.state.name
        }

        env {
          name  = "ORBITAL_WATCH_PUBLIC_BUCKET"
          value = google_storage_bucket.public.name
        }

        # The cadence the job is actually started at, so the thresholds it derives and publishes
        # are about the real schedule and not a number repeated in the code.
        env {
          name  = "ORBITAL_WATCH_CADENCE_MINUTES"
          value = tostring(var.refresh_cadence_minutes)
        }
      }
    }
  }

  lifecycle {
    # The deploy workflow owns which image runs: it builds one on every merge and points the job
    # at it. Terraform owns everything else about the job.
    ignore_changes = [
      client,
      client_version,
      template[0].template[0].containers[0].image,
    ]
  }

  depends_on = [
    google_project_service.this,
    google_storage_bucket_iam_member.refresh_uses_state,
    google_storage_bucket_iam_member.refresh_publishes,
  ]
}
