# Runs the whole configuration against mocked providers and checks the properties that keep this
# locked down, cheap, and in step with the code. "apply" here is an apply to the mocks: no
# credentials, no network, and nothing is created anywhere. (Some values are known only after an
# apply, which is why these are not plan-only runs.)
#
#   terraform init -backend=false && terraform test

mock_provider "google" {
  mock_data "google_project" {
    defaults = {
      number = "805279368733"
    }
  }

  mock_data "google_iam_workload_identity_pool" {
    defaults = {
      name     = "projects/805279368733/locations/global/workloadIdentityPools/github"
      disabled = false
    }
  }

  # The mocks invent random strings for computed values; these few are checked for their shape by
  # other resources, so they are given realistic ones.
  mock_resource "google_service_account" {
    defaults = {
      name   = "projects/portfolio-prod-0p1ht/serviceAccounts/mock@portfolio-prod-0p1ht.iam.gserviceaccount.com"
      email  = "mock@portfolio-prod-0p1ht.iam.gserviceaccount.com"
      member = "serviceAccount:mock@portfolio-prod-0p1ht.iam.gserviceaccount.com"
    }
  }
}

mock_provider "google-beta" {}

variables {
  alert_email = "alerts@example.com"
}

run "only_the_published_bucket_is_public" {
  command = apply

  assert {
    condition     = google_storage_bucket.state.public_access_prevention == "enforced"
    error_message = "The state bucket must rule out public access at the bucket, whatever IAM says."
  }

  assert {
    condition     = google_storage_bucket.state.uniform_bucket_level_access && google_storage_bucket.public.uniform_bucket_level_access
    error_message = "Both buckets must use uniform bucket-level access: no per-object ACLs."
  }

  assert {
    condition     = google_storage_bucket_iam_member.public_read.bucket == google_storage_bucket.public.name
    error_message = "The one public binding must be on the public bucket."
  }

  assert {
    condition     = google_storage_bucket_iam_member.public_read.member == "allUsers" && google_storage_bucket_iam_member.public_read.role == "roles/storage.legacyObjectReader"
    error_message = "Anyone may read objects in the public bucket, and nothing more: not list them, not write them."
  }

  assert {
    condition     = google_storage_bucket.state.name != google_storage_bucket.public.name
    error_message = "State and the published document must be in different buckets."
  }
}

run "the_published_bucket_answers_only_the_sites_own_origins" {
  command = apply

  assert {
    condition = toset(google_storage_bucket.public.cors[0].origin) == toset([
      "https://orbital.rodrigopimenta.com",
      "https://rodrigopimenta-orbital-watch.web.app",
      "https://rodrigopimenta-orbital-watch.firebaseapp.com",
      "http://localhost:8000",
      "http://127.0.0.1:8000",
    ])
    error_message = "CORS must list exactly the site's own addresses and a developer's localhost."
  }

  assert {
    condition     = toset(google_storage_bucket.public.cors[0].method) == toset(["GET", "HEAD"])
    error_message = "Browsers may only read the published document."
  }

  assert {
    condition     = contains(google_storage_bucket.public.cors[0].response_header, "Date")
    error_message = "The page reads the server's Date as a floor under the reader's clock; the bucket must expose it."
  }

  assert {
    condition     = length(google_storage_bucket.state.cors) == 0
    error_message = "No browser has any business reading the state bucket."
  }
}

run "state_can_be_recovered_and_does_not_grow_without_bound" {
  command = apply

  assert {
    condition     = google_storage_bucket.state.versioning[0].enabled
    error_message = "The state bucket must keep overwritten versions."
  }

  assert {
    condition     = one(google_storage_bucket.state.lifecycle_rule[0].condition).days_since_noncurrent_time == 7
    error_message = "Old state versions must be deleted after a week."
  }
}

run "the_job_runs_as_itself_with_bucket_access_only" {
  command = apply

  assert {
    condition     = google_storage_bucket_iam_member.refresh_uses_state.role == "roles/storage.objectUser" && google_storage_bucket_iam_member.refresh_publishes.role == "roles/storage.objectUser"
    error_message = "The job gets object access to its two buckets, not bucket administration."
  }

  assert {
    condition = (
      google_storage_bucket_iam_member.refresh_uses_state.bucket == google_storage_bucket.state.name &&
      google_storage_bucket_iam_member.refresh_publishes.bucket == google_storage_bucket.public.name
    )
    error_message = "The job's two grants must be on its two buckets."
  }

  assert {
    condition     = toset(keys(google_project_iam_member.deploy_publishes_site)) == toset(["roles/firebasehosting.admin", "roles/serviceusage.apiKeysViewer"])
    error_message = "Project-level roles changed. Only the deploy account holds any, and only these two; the runtime and scheduler accounts hold none, and nobody gets owner or editor."
  }
}

run "the_job_is_one_task_that_is_not_retried" {
  command = apply

  assert {
    condition     = google_cloud_run_v2_job.refresh.template[0].task_count == 1 && google_cloud_run_v2_job.refresh.template[0].parallelism == 1
    error_message = "One task per execution."
  }

  assert {
    condition     = google_cloud_run_v2_job.refresh.template[0].template[0].max_retries == 0
    error_message = "A failed execution must reach the alert as a failure, not be retried away."
  }

  assert {
    condition     = google_cloud_run_v2_job.refresh.template[0].template[0].timeout == "300s"
    error_message = "The task timeout must stay well under the cadence, so runs cannot pile up."
  }

  assert {
    condition = {
      for item in google_cloud_run_v2_job.refresh.template[0].template[0].containers[0].env : item.name => item.value
      } == {
      ORBITAL_WATCH_STATE_BUCKET    = "portfolio-prod-0p1ht-orbital-watch-state"
      ORBITAL_WATCH_PUBLIC_BUCKET   = "portfolio-prod-0p1ht-orbital-watch-public"
      ORBITAL_WATCH_CADENCE_MINUTES = "10"
    }
    error_message = "The job must be told its two buckets and the cadence it is started at, and nothing else. (No secrets: it has none.)"
  }
}

run "one_cadence_drives_the_schedule_the_job_and_the_alert" {
  command = apply

  variables {
    refresh_cadence_minutes = 15
  }

  assert {
    condition     = google_cloud_scheduler_job.refresh.schedule == "*/15 * * * *"
    error_message = "The cron expression must come from refresh_cadence_minutes."
  }

  assert {
    condition = one([
      for item in google_cloud_run_v2_job.refresh.template[0].template[0].containers[0].env : item.value
      if item.name == "ORBITAL_WATCH_CADENCE_MINUTES"
    ]) == "15"
    error_message = "The job must be told the cadence it is actually started at."
  }

  assert {
    condition     = google_monitoring_alert_policy.no_successful_run.conditions[0].condition_absent[0].duration == "2700s"
    error_message = "The 'no successful run' alert must fire after three cadences."
  }
}

run "the_scheduler_starts_this_job_without_a_key" {
  command = apply

  assert {
    condition     = google_cloud_scheduler_job.refresh.http_target[0].uri == "https://run.googleapis.com/v2/projects/portfolio-prod-0p1ht/locations/us-east4/jobs/orbital-watch-refresh:run"
    error_message = "Scheduler must start the refresh job, in this project and region."
  }

  assert {
    condition     = google_cloud_scheduler_job.refresh.http_target[0].oauth_token[0].service_account_email == google_service_account.scheduler.email
    error_message = "Scheduler must call as its own service account, with a token Google mints per call."
  }

  assert {
    condition     = google_cloud_run_v2_job_iam_member.scheduler_starts_job.role == "roles/run.invoker"
    error_message = "The scheduler account may start the job and nothing else."
  }

  assert {
    condition     = google_cloud_scheduler_job.refresh.retry_config[0].retry_count == 2 && google_cloud_scheduler_job.refresh.retry_config[0].max_retry_duration == "0s"
    error_message = "The start call is retried exactly twice. A non-zero max_retry_duration would make that 'at least twice'."
  }
}

run "only_this_repository_can_deploy_and_only_from_main" {
  command = apply

  assert {
    condition     = google_iam_workload_identity_pool_provider.orbital_watch.attribute_condition == "assertion.repository == 'rodrigopimenta10/orbital-watch' && assertion.repository_owner_id == '11894505'"
    error_message = "The provider must refuse tokens from any other repository, checking the owner by number."
  }

  assert {
    condition     = google_iam_workload_identity_pool_provider.orbital_watch.workload_identity_pool_id == "github"
    error_message = "The provider belongs in the existing pool; this configuration must not create another."
  }

  assert {
    condition     = google_service_account_iam_member.deploy_from_main.member == "principalSet://iam.googleapis.com/projects/805279368733/locations/global/workloadIdentityPools/github/attribute.workflow_ref/rodrigopimenta10/orbital-watch/.github/workflows/deploy.yml@refs/heads/main"
    error_message = "The deploy account must be usable only by deploy.yml on main of this repository."
  }

  assert {
    condition     = google_cloud_run_v2_job_iam_member.deploy_updates_job.role == "roles/run.developer" && google_cloud_run_v2_job_iam_member.deploy_updates_job.name == "orbital-watch-refresh"
    error_message = "The deploy account gets run.developer on the one job, not on the project."
  }

  assert {
    condition     = google_artifact_registry_repository_iam_member.deploy_pushes_images.role == "roles/artifactregistry.writer"
    error_message = "The deploy account may push images to the one repository, not administer it."
  }
}

run "something_independent_watches_the_pipeline" {
  command = apply

  assert {
    condition     = strcontains(google_monitoring_alert_policy.execution_failed.conditions[0].condition_threshold[0].filter, "metric.labels.result = \"failed\"") && google_monitoring_alert_policy.execution_failed.conditions[0].condition_threshold[0].threshold_value == 0
    error_message = "One failed execution must be enough to alert."
  }

  assert {
    condition     = strcontains(google_monitoring_alert_policy.no_successful_run.conditions[0].condition_absent[0].filter, "metric.labels.result = \"succeeded\"") && google_monitoring_alert_policy.no_successful_run.conditions[0].condition_absent[0].duration == "1800s"
    error_message = "Thirty minutes without a successful execution must alert, on the absence of data."
  }

  assert {
    condition = alltrue([
      for policy in concat(
        [google_monitoring_alert_policy.execution_failed, google_monitoring_alert_policy.no_successful_run],
        values(google_monitoring_alert_policy.uptime),
      ) : length(policy.notification_channels) == 1 && policy.notification_channels[0] == google_monitoring_notification_channel.email.id
    ])
    error_message = "Every alert must notify the one email channel. An alert nobody is told about is decoration."
  }

  assert {
    condition     = google_monitoring_uptime_check_config.this["document"].content_matchers[0].matcher == "MATCHES_JSON_PATH" && google_monitoring_uptime_check_config.this["document"].content_matchers[0].json_path_matcher[0].json_path == "$.kind"
    error_message = "The document check must look inside the response, not only at its status."
  }

  assert {
    condition     = google_monitoring_uptime_check_config.this["document"].http_check[0].path == "/portfolio-prod-0p1ht-orbital-watch-public/latest.json"
    error_message = "The document check must fetch what the page fetches."
  }

  assert {
    condition     = google_monitoring_uptime_check_config.this["site"].content_matchers[0].content == "<title>Orbital Watch" && google_monitoring_uptime_check_config.this["site"].monitored_resource[0].labels.host == "orbital.rodrigopimenta.com"
    error_message = "The site check must fetch the public address and recognise the page."
  }
}

# That alert_email has no default at all (so it can never be committed) is checked from the
# repository's Python tests, which read variables.tf; a test here cannot leave a variable unset.
run "the_alert_address_must_be_an_address" {
  command = plan

  variables {
    alert_email = "not-an-address"
  }

  expect_failures = [var.alert_email]
}

run "a_cadence_that_cron_cannot_space_evenly_is_refused" {
  command = plan

  variables {
    refresh_cadence_minutes = 7
  }

  expect_failures = [var.refresh_cadence_minutes]
}

run "the_site_and_pool_are_referred_to_not_created" {
  command = apply

  assert {
    condition     = google_firebase_hosting_custom_domain.orbital.site_id == "rodrigopimenta-orbital-watch" && google_firebase_hosting_custom_domain.orbital.custom_domain == "orbital.rodrigopimenta.com"
    error_message = "The custom domain must be attached to the existing Orbital Watch Hosting site."
  }

  assert {
    condition     = google_firebase_hosting_custom_domain.orbital.wait_dns_verification == false
    error_message = "The apply must not wait on a DNS change that is made by hand afterwards."
  }

  assert {
    condition     = output.repository_variables.LIVE_DOCUMENT_URL == "https://storage.googleapis.com/portfolio-prod-0p1ht-orbital-watch-public/latest.json"
    error_message = "The deploy workflow must be handed the address the job publishes to."
  }

  assert {
    condition     = google_artifact_registry_repository.orbital_watch.cleanup_policies != null && length(google_artifact_registry_repository.orbital_watch.cleanup_policies) == 2
    error_message = "The registry must keep the latest images and delete the rest."
  }
}
