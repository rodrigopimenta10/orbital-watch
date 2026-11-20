# Something independent watches the pipeline.
#
# The page already shows a visitor when the pipeline has gone quiet. That is for visitors. These
# alerts are for the owner, and none of them depends on the job running correctly -- which is the
# point: the two earlier outages were both found by someone looking at the page.
#
#   1. An execution failed           the job ran and could not publish.
#   2. No execution has succeeded    the job is not running at all (Scheduler stopped, the image
#                                    is missing, a permission was removed).
#   3. The published document        the bucket, its public access, or the document's shape.
#   4. The site                      Hosting, the custom domain, the certificate.
#
# The job exits non-zero only when it could not publish. An upstream outage is not a failed
# execution -- the document reports it and the page shows it -- so alert 1 does not fire every
# time NOAA or Celestrak has a bad hour.

locals {
  cadence_seconds = var.refresh_cadence_minutes * 60

  # Three runs in a row missing. The page uses the same figure, derived from the same cadence, to
  # tell visitors the pipeline is silent.
  silence_limit_seconds = 3 * local.cadence_seconds

  job_metric_filter = join(" AND ", [
    "resource.type = \"cloud_run_job\"",
    "resource.labels.job_name = \"${google_cloud_run_v2_job.refresh.name}\"",
    "metric.type = \"run.googleapis.com/job/completed_execution_count\"",
  ])

  document_url = "https://storage.googleapis.com/${google_storage_bucket.public.name}/latest.json"
}

resource "google_monitoring_notification_channel" "email" {
  project      = var.project_id
  display_name = "Orbital Watch alerts"
  type         = "email"

  labels = {
    email_address = var.alert_email
  }

  depends_on = [google_project_service.this]
}

# --- 1. An execution failed.

resource "google_monitoring_alert_policy" "execution_failed" {
  project      = var.project_id
  display_name = "Orbital Watch: a refresh run failed"
  combiner     = "OR"
  severity     = "ERROR"

  conditions {
    display_name = "At least one failed execution of ${google_cloud_run_v2_job.refresh.name}"

    condition_threshold {
      filter          = "${local.job_metric_filter} AND metric.labels.result = \"failed\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period   = "${local.cadence_seconds}s"
        per_series_aligner = "ALIGN_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  alert_strategy {
    # Close by itself half an hour after failures stop.
    auto_close = "1800s"
  }

  notification_channels = [google_monitoring_notification_channel.email.id]

  documentation {
    mime_type = "text/markdown"
    subject   = "Orbital Watch: a refresh run failed"
    content   = <<-EOT
      A run of the Cloud Run job `${google_cloud_run_v2_job.refresh.name}` exited non-zero.

      The job exits non-zero **only when it could not publish** — upstream sources being down do
      not fail it. So the page is now showing an older document, and will say so after
      ${local.silence_limit_seconds / 60} minutes.

      Exit codes: `1` could not write to the public bucket, `2` a bug in the job, `3` the document
      failed validation and was withheld.

      Look at the execution's logs first:
      `gcloud run jobs executions list --job ${google_cloud_run_v2_job.refresh.name} --region ${var.region} --project ${var.project_id}`
    EOT
  }
}

# --- 2. No execution has succeeded for three cadences.

resource "google_monitoring_alert_policy" "no_successful_run" {
  project      = var.project_id
  display_name = "Orbital Watch: no successful refresh run for ${local.silence_limit_seconds / 60} minutes"
  combiner     = "OR"
  severity     = "ERROR"

  conditions {
    display_name = "No successful execution of ${google_cloud_run_v2_job.refresh.name} for ${local.silence_limit_seconds / 60} minutes"

    # Fires on the *absence* of data: no successful execution reported at all. A threshold on a
    # count cannot do this, because when nothing runs there is no count to compare.
    condition_absent {
      filter   = "${local.job_metric_filter} AND metric.labels.result = \"succeeded\""
      duration = "${local.silence_limit_seconds}s"

      aggregations {
        alignment_period   = "${local.cadence_seconds}s"
        per_series_aligner = "ALIGN_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.id]

  documentation {
    mime_type = "text/markdown"
    subject   = "Orbital Watch: the refresh job has stopped succeeding"
    content   = <<-EOT
      No execution of `${google_cloud_run_v2_job.refresh.name}` has succeeded for
      ${local.silence_limit_seconds / 60} minutes. It is scheduled every
      ${var.refresh_cadence_minutes} minutes.

      If the "a refresh run failed" alert is also firing, the job runs and fails: start there.
      If it is not, the job is not being started. Check the schedule:
      `gcloud scheduler jobs describe ${google_cloud_scheduler_job.refresh.name} --location ${var.region} --project ${var.project_id}`
      (state should be ENABLED, and the last attempt should not carry an error status).
    EOT
  }
}

# --- 3 and 4. Uptime checks that look at what came back, not only at the status.

locals {
  uptime_checks = {
    document = {
      display_name = "Orbital Watch: published document"
      host         = "storage.googleapis.com"
      path         = "/${google_storage_bucket.public.name}/latest.json"
      # The document is about 100 KB and every check downloads it from three regions, so it is
      # checked every fifteen minutes; more often would mostly add to the transfer bill. The two
      # job alerts above notice a stopped pipeline sooner than this could.
      period      = "900s"
      what_failed = "The published document at ${local.document_url} is unreachable, not public, or not an Orbital Watch document. The page falls back to the copy shipped with the site and says so. Check the bucket's public read binding and the job's recent executions."
    }
    site = {
      display_name = "Orbital Watch: site"
      host         = var.custom_domain
      path         = "/"
      period       = "300s"
      what_failed  = "https://${var.custom_domain}/ is unreachable or is not serving the Orbital Watch page. Check the custom domain's state in Firebase Hosting, its DNS record at Cloudflare, and the last deploy."
    }
  }
}

resource "google_monitoring_uptime_check_config" "this" {
  for_each = local.uptime_checks

  project      = var.project_id
  display_name = each.value.display_name
  period       = each.value.period
  timeout      = "10s"

  # Three regions, so one checker's bad route is not an outage.
  selected_regions = ["USA_VIRGINIA", "USA_OREGON", "EUROPE"]

  http_check {
    path           = each.value.path
    port           = 443
    use_ssl        = true
    validate_ssl   = true
    request_method = "GET"

    accepted_response_status_codes {
      status_class = "STATUS_CLASS_2XX"
    }
  }

  # A 200 proves little: an error page or a default landing page can be a 200. Each check also
  # requires the response to be the thing it is supposed to be.
  dynamic "content_matchers" {
    # The document must be JSON whose "kind" names it. The job writes that field for this purpose.
    for_each = each.key == "document" ? [1] : []
    content {
      matcher = "MATCHES_JSON_PATH"
      content = "\"orbital-watch.latest\""

      json_path_matcher {
        json_path    = "$.kind"
        json_matcher = "EXACT_MATCH"
      }
    }
  }

  dynamic "content_matchers" {
    # The site must be this page, identified by its title.
    for_each = each.key == "site" ? [1] : []
    content {
      matcher = "CONTAINS_STRING"
      content = "<title>Orbital Watch"
    }
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = each.value.host
    }
  }

  depends_on = [google_project_service.this]
}

resource "google_monitoring_alert_policy" "uptime" {
  for_each = local.uptime_checks

  project      = var.project_id
  display_name = "${each.value.display_name} is failing its check"
  combiner     = "OR"
  severity     = "ERROR"

  conditions {
    display_name = "${each.value.display_name}: failing from more than one region"

    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"uptime_url\"",
        "metric.type = \"monitoring.googleapis.com/uptime_check/check_passed\"",
        "metric.labels.check_id = \"${google_monitoring_uptime_check_config.this[each.key].uptime_check_id}\"",
      ])

      # Count the regions whose most recent check failed, and alert when more than one has and
      # it has stayed that way for five minutes. One region failing alone is a bad route, not an
      # outage. (This is the shape of the policy the Cloud console writes for an uptime check.)
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "300s"

      aggregations {
        alignment_period     = "1200s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.*"]
      }

      trigger {
        count = 1
      }
    }
  }

  alert_strategy {
    auto_close = "1800s"
  }

  notification_channels = [google_monitoring_notification_channel.email.id]

  documentation {
    mime_type = "text/markdown"
    subject   = "${each.value.display_name} is failing its check"
    content   = each.value.what_failed
  }
}
