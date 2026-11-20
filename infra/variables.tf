variable "project_id" {
  description = "The Google Cloud project everything here lives in. It exists already and is shared with the portfolio site, whose Terraform (in the portfolio-website repository) owns the Firebase project, the Hosting sites and the workload identity pool. Nothing here creates any of those."
  type        = string
  default     = "portfolio-prod-0p1ht"
}

variable "region" {
  description = "Region of the two buckets, the container registry, the Cloud Run job and the Cloud Scheduler job. One region for all of it: the job's storage calls then never leave it."
  type        = string
  default     = "us-east4"
}

variable "alert_email" {
  description = "Where monitoring alerts are sent. No default on purpose: pass it as TF_VAR_alert_email and never write it to a file."
  type        = string
  sensitive   = true

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alert_email))
    error_message = "alert_email must be an email address."
  }
}

variable "refresh_cadence_minutes" {
  description = "How often Cloud Scheduler starts the refresh job. This one number sets the cron expression, is passed to the job (which derives its freshness thresholds from it), and sizes the 'no successful run' alert. The default is pinned to the code's own default by a test."
  type        = number
  default     = 10

  validation {
    # "*/N" in a cron minute field only yields evenly spaced runs when N divides the hour.
    condition     = contains([5, 6, 10, 12, 15, 20, 30], var.refresh_cadence_minutes)
    error_message = "refresh_cadence_minutes must divide 60 evenly and be between 5 and 30."
  }
}

variable "github_repository" {
  description = "The one GitHub repository allowed to deploy Orbital Watch, as owner/name."
  type        = string
  default     = "rodrigopimenta10/orbital-watch"
}

variable "github_owner_id" {
  description = "Numeric ID of the GitHub account that owns the repository (gh api users/<login> --jq .id). Names can be given up and taken by someone else; this number cannot."
  type        = string
  default     = "11894505"
}

variable "workload_identity_pool_id" {
  description = "The existing workload identity pool GitHub's tokens are exchanged in. It belongs to the portfolio's Terraform; this configuration only adds a provider to it."
  type        = string
  default     = "github"
}

variable "hosting_site_id" {
  description = "The existing Firebase Hosting site the page is deployed to. It belongs to the portfolio's Terraform (google_firebase_hosting_site.orbital_watch there); it is referred to here by name and not created."
  type        = string
  default     = "rodrigopimenta-orbital-watch"
}

variable "custom_domain" {
  description = "The address visitors use. Connected to the Hosting site here; its DNS record is at Cloudflare and is changed by hand (see the dns_records output)."
  type        = string
  default     = "orbital.rodrigopimenta.com"
}

variable "development_origins" {
  description = "Origins a developer's browser may read the published document from, in addition to the site's own. These are what `python3 -m http.server 8000` serves on."
  type        = list(string)
  default     = ["http://localhost:8000", "http://127.0.0.1:8000"]
}

variable "job_image" {
  description = "Image the Cloud Run job is created with. The deploy workflow replaces it on every merge, and Terraform ignores that change, so this matters only between the first apply and the first deploy. The default is Google's sample job, which prints a line and exits 0."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/job:latest"
}
