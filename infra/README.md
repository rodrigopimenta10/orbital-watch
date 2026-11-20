# Infrastructure (Terraform)

Terraform for the Google Cloud side of Orbital Watch: a Cloud Run job started by Cloud Scheduler,
two Cloud Storage buckets, a container registry, monitoring, keyless sign-in for this
repository's deploy workflow, and the custom domain on Firebase Hosting.

**Status: written and checked against mocked providers; not yet applied.** `terraform fmt`,
`terraform validate` and `terraform test` run in CI on every push (`.github/workflows/ci.yml`).
Nothing in CI signs in to Google Cloud or reads the state. Applying is done by hand, from this
folder, with the steps below.

The project is `portfolio-prod-0p1ht`, shared with the portfolio site. Three things this depends
on are created by the portfolio's Terraform (in the `portfolio-website` repository) and are only
referred to here, never created or changed:

| Existing thing | Referred to by |
|---|---|
| The Firebase project and the Hosting site `rodrigopimenta-orbital-watch` | `var.hosting_site_id` |
| The workload identity pool `github` | `var.workload_identity_pool_id` (read as a data source) |
| The Terraform state bucket `portfolio-prod-0p1ht-tfstate` | named at `terraform init`; this configuration uses the prefix `orbital-watch` |

Run every command below from this folder (`infra/`).

## What it creates

38 resources.

| Resource | What it is |
|---|---|
| `google_project_service.this` (11) | Turns on the APIs used here. Left on if this is destroyed |
| `google_storage_bucket.state` | Private bucket for the job's per-source state. Public access prevented at the bucket; versioned, old versions deleted after 7 days |
| `google_storage_bucket.public` | Holds one object, `latest.json`. CORS limited to the site's origins and localhost |
| `google_storage_bucket_iam_member.public_read` | Anyone may read objects in the public bucket (and not list them) |
| `google_artifact_registry_repository.orbital_watch` | Docker repository for the job image; keeps the latest five, deletes the rest after a week |
| `google_service_account.refresh` | The identity the job runs as |
| `google_storage_bucket_iam_member.refresh_uses_state`, `.refresh_publishes` | Object access for that identity on its two buckets. It holds no project-level role |
| `google_cloud_run_v2_job.refresh` | `orbital-watch-refresh`: 1 vCPU, 512 MiB, one task, no retries, 300 s timeout |
| `google_service_account.scheduler` | The identity Cloud Scheduler starts the job as |
| `google_cloud_run_v2_job_iam_member.scheduler_starts_job` | It may run that one job |
| `google_cloud_scheduler_job.refresh` | `*/10 * * * *` UTC; the start call is retried twice |
| `google_monitoring_notification_channel.email` | The address alerts go to |
| `google_monitoring_alert_policy.execution_failed` | A run of the job exited non-zero |
| `google_monitoring_alert_policy.no_successful_run` | No run has succeeded for 30 minutes (metric absence) |
| `google_monitoring_uptime_check_config.this` (2) | The published document (must be JSON whose `kind` is `orbital-watch.latest`) and the site (must contain the page's title) |
| `google_monitoring_alert_policy.uptime` (2) | An alert on each of those checks |
| `google_iam_workload_identity_pool_provider.orbital_watch` | Accepts GitHub's tokens only from `rodrigopimenta10/orbital-watch`, checked by owner ID as well as name |
| `google_service_account.deploy` | The identity the deploy workflow uses |
| `google_service_account_iam_member.deploy_from_main` | Usable only by `deploy.yml` on `main` |
| `google_artifact_registry_repository_iam_member.deploy_pushes_images` | Deploy may push images to the one repository |
| `google_cloud_run_v2_job_iam_member.deploy_updates_job` | Deploy may update and start the one job |
| `google_service_account_iam_member.deploy_acts_as_runtime` | Deploy may give the job its runtime identity, and only that one |
| `google_project_iam_member.deploy_publishes_site` (2) | Hosting admin and API keys viewer, on the project (see "Wider than it should be") |
| `google_firebase_hosting_custom_domain.orbital` | `orbital.rodrigopimenta.com` on the existing Hosting site |

### Left out on purpose

- **No service account keys.** Scheduler calls with a token Google mints per call; GitHub Actions
  signs in through Workload Identity Federation.
- **No secrets and no Secret Manager.** The job reads public URLs and its own two buckets.
- **No load balancer, no Cloud CDN, no always-on service.** The page is on Firebase Hosting; the
  document is read straight from the bucket. Between runs nothing is running.
- **No retry on the job.** A failed run should reach the alert. The next scheduled run is the
  retry.
- **No reserved outbound address.** See "Risks" in the repository README: Celestrak keeps its
  rate limit per requesting address, and Cloud Run's are shared.

### Wider than it should be

`roles/firebasehosting.admin` is the narrowest role that can publish a Hosting release, and it can
only be granted on the project. So the Orbital Watch deploy account could also publish to the
portfolio's Hosting site. It is usable only by `deploy.yml` on `main` of this repository, which is
what limits it in practice. Everything else the account holds is scoped to a single resource.

## Cost

About **$0.50 to $1 a month** at list prices, and plausibly nothing: most of it falls inside free
allowances that are shared with whatever else the billing account runs.

The usage figures below follow from the configuration. The unit prices are list prices as I
understand them and were **not** re-read from Google's pricing pages when this was written; check
them before relying on the totals.

| Item | Usage a month | Roughly |
|---|---|---|
| Cloud Run job | 4,320 runs of about 15 s at 1 vCPU and 512 MiB: about 65,000 vCPU-seconds and 32,000 GiB-seconds | Inside the monthly free allowance if nothing else uses it; about $1.20 otherwise |
| Cloud Scheduler | 1 job | Free (the first three jobs on a billing account are); $0.10 otherwise |
| Cloud Storage operations | About 23,000 writes (four space-weather state objects and the document on every run, three Celestrak state objects twelve times a day) and 35,000 reads by the job | About $0.13 |
| Cloud Storage data | A few megabytes including a week of old state versions | Under $0.01 |
| Data transfer out | The document is about 100 KB. Uptime checks fetch it about 8,600 times a month (0.9 GB); visitors add to that | About $0.10 plus visitors, if not covered by the free allowance |
| Artifact Registry | Five images of about 350 MB uncompressed, less compressed | Free under 0.5 GB stored, then about $0.10 per GB |
| Cloud Monitoring | 4 alert policies, 2 uptime checks, 1 email channel | Free at this volume |
| Firebase Hosting | A site of well under 1 MB | Free |
| Workload Identity Federation, IAM | | Free |

The portfolio's Terraform already sets a budget alert on this project at $10 a month.

## Variables

| Variable | Default | Notes |
|---|---|---|
| `project_id` | `portfolio-prod-0p1ht` | Exists already |
| `region` | `us-east4` | Buckets, registry, job and schedule all in one region |
| `alert_email` | none | **Never write it to a file.** Pass as `TF_VAR_alert_email` |
| `refresh_cadence_minutes` | `10` | Sets the cron, is passed to the job, and sizes the "no successful run" alert. Must divide 60. A test pins the default to the code's own |
| `github_repository` | `rodrigopimenta10/orbital-watch` | |
| `github_owner_id` | `11894505` | The account's numeric ID |
| `workload_identity_pool_id` | `github` | Existing; owned by the portfolio's Terraform |
| `hosting_site_id` | `rodrigopimenta-orbital-watch` | Existing; owned by the portfolio's Terraform. Must match `.firebaserc` |
| `custom_domain` | `orbital.rodrigopimenta.com` | |
| `development_origins` | `http://localhost:8000`, `http://127.0.0.1:8000` | Extra origins allowed to read the document in a browser |
| `job_image` | Google's sample job image | Only until the first deploy |

`.gitignore` refuses `*.tfvars`, so the value without a default cannot be committed by accident.
It does end up in the Terraform state, which is why the state lives in a private bucket.

## Check it without touching Google Cloud

```sh
terraform fmt -check -recursive
terraform init -backend=false
terraform validate
terraform test          # applies to mocked providers: no credentials, no network, nothing created
```

`tests/invariants.tftest.hcl` checks, among other things: only the published bucket is public and
only for reading objects; CORS lists exactly the site's origins; the job has object access to its
two buckets and no project role; one cadence drives the cron, the job's environment and the
alert; the scheduler and the deploy account use tokens, not keys; only `deploy.yml` on `main` of
this repository can deploy; every alert notifies the channel; the uptime checks look at content.

## Bootstrap and first apply

This needs your own Google account with Owner on the project. The state bucket already exists.

```sh
PROJECT=portfolio-prod-0p1ht

# 1. Sign in as yourself. Terraform uses these credentials; no key file is created.
gcloud auth application-default login
gcloud auth application-default set-quota-project "$PROJECT"

# 2. The one value that is never written down. `read -s` keeps it out of shell history.
read -rs "TF_VAR_alert_email?Alert email: " && export TF_VAR_alert_email

# 3. Plan, read the plan, apply exactly that plan.
terraform init -backend-config="bucket=$PROJECT-tfstate"
terraform plan -out plan.out      # expect: 38 to add, 0 to change, 0 to destroy
terraform apply plan.out
rm plan.out                       # a saved plan holds the sensitive value
```

(`read -rs "VAR?prompt"` is zsh. In bash: `read -rsp "Alert email: " TF_VAR_alert_email`.)

Things to watch on the first apply:

- **The plan should add and nothing else.** If it proposes to change or destroy anything, stop:
  this configuration shares a project with the portfolio and should not touch what is there.
- **The email channel may be created unverified.** If Google sends a verification message, the
  alerts do not notify anyone until it is confirmed. Check the inbox.
- **Expect alert emails until the first deploy.** Between this apply and the first deploy the
  schedule runs Google's sample image, which succeeds without publishing anything, so the
  "published document" check fails. That is the check working. Do the next section promptly.
- **The public bucket binding can be refused.** If the project or its organisation enforces
  public access prevention, `google_storage_bucket_iam_member.public_read` fails; the policy has
  to allow this one bucket.

## After the apply

1. Hand the deploy workflow its settings. Each is a name, not a secret.

   ```sh
   terraform output -json repository_variables \
     | jq -r 'to_entries[] | "\(.key)\t\(.value)"' \
     | while IFS=$'\t' read -r name value; do
         gh variable set "$name" --repo rodrigopimenta10/orbital-watch --body "$value"
       done
   ```

   The deploy workflow does nothing until `GCP_PROJECT_ID` exists.

2. Deploy: push to `main`, or run the `deploy` workflow by hand. It builds and pushes the image,
   points the job at it, starts one run, waits for the document that run publishes, and deploys
   the site with that document baked in.

3. Check it at `https://rodrigopimenta-orbital-watch.web.app` (also `terraform output
   hosting_default_url`). The page should say it is showing the live document.

4. Move the domain. `terraform output dns_records` lists what Firebase Hosting wants for
   `orbital.rodrigopimenta.com`. At Cloudflare, replace the existing `orbital` record (today a
   proxied CNAME to the Pages project) with the record or records listed as `desired`, and set
   them to **DNS only** (grey cloud): Firebase issues the certificate itself and cannot do so
   through Cloudflare's proxy. Then:

   ```sh
   terraform apply -refresh-only     # reads the domain's new state; changes nothing
   terraform output custom_domain_state
   ```

   The certificate can take from minutes to a few hours. Until it is issued the old site keeps
   answering for visitors whose resolvers still hold the old record.

5. Retire the old system once the new address serves the new page: delete the Cloudflare Pages
   project, the `orbital-watch-refresh` Worker, and its deploy hook and secrets.

## Changing the cadence

Change `refresh_cadence_minutes` and apply. The cron expression, the job's environment and the
"no successful run" alert all follow. The page needs nothing: it reads the schedule from the
document.

## Taking it down

`google_cloud_run_v2_job` has deletion protection on by default. To destroy, first set
`deletion_protection = false` on it and apply, then `terraform destroy`. Both buckets must be
emptied first (`gcloud storage rm --recursive`), since neither sets `force_destroy`. The APIs stay
enabled, and the state bucket, the Hosting site and the identity pool are untouched: they are not
this configuration's.
