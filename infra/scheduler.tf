# The one schedule in the system. Cloud Scheduler starts the job by calling the Cloud Run API;
# there is no second timer anywhere whose punctuality the first has to agree with.

# The identity Scheduler makes that call as. It may start this one job and do nothing else.
resource "google_service_account" "scheduler" {
  project      = var.project_id
  account_id   = "orbital-watch-scheduler"
  display_name = "Orbital Watch scheduler"
  description  = "Identity Cloud Scheduler uses to start the Cloud Run job orbital-watch-refresh. May run that job; nothing else."

  depends_on = [google_project_service.this]
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_starts_job" {
  project  = google_cloud_run_v2_job.refresh.project
  location = google_cloud_run_v2_job.refresh.location
  name     = google_cloud_run_v2_job.refresh.name
  role     = "roles/run.invoker"
  member   = google_service_account.scheduler.member
}

resource "google_cloud_scheduler_job" "refresh" {
  project     = var.project_id
  region      = var.region
  name        = "orbital-watch-refresh"
  description = "Starts the Orbital Watch refresh job every ${var.refresh_cadence_minutes} minutes."

  schedule  = "*/${var.refresh_cadence_minutes} * * * *"
  time_zone = "Etc/UTC"

  # The call only *starts* an execution and returns; it does not wait for the job. A minute is
  # ample, and anything slower is treated as a failed attempt.
  attempt_deadline = "60s"

  # What is retried here is that start call, not the job. If the Cloud Run API refuses or times
  # out, try twice more, 30 and then 60 seconds later, and then stop. With the 60-second deadline
  # on each attempt that is over in under five minutes, before the next scheduled start, so a
  # retry never overlaps the next tick. If a start call succeeded but its answer was lost, a retry
  # starts a second execution alongside the first; the job's conditional writes make that
  # harmless.
  #
  # max_retry_duration is "0s", which means "no time limit", on purpose. Cloud Scheduler keeps
  # retrying until *both* the count and the duration are used up, so any non-zero duration would
  # turn "two retries" into "at least two retries".
  retry_config {
    retry_count          = 2
    min_backoff_duration = "30s"
    max_backoff_duration = "60s"
    max_doublings        = 1
    max_retry_duration   = "0s"
  }

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.refresh.name}:run"

    # A short-lived token for the scheduler's service account, minted by Google for each call. No
    # key exists.
    oauth_token {
      service_account_email = google_service_account.scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [
    google_project_service.this,
    google_cloud_run_v2_job_iam_member.scheduler_starts_job,
  ]
}
