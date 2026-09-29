# Base GCP resources for the Telegram assistant: APIs, Firestore, artifact
# registry, secrets, service accounts, Workload Identity Federation, Pub/Sub
# topics, Cloud Scheduler jobs and a budget guard. The Cloud Run services
# (assistant-api, assistant-worker) are deployed by GitHub Actions, not here;
# their push subscription is gated on worker_url (set it after the first deploy).
# State is local (terraform.tfstate, git-ignored).

terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

variable "project_id" {
  description = "GCP project id (globally unique)."
  type        = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "firestore_location" {
  description = "Firestore location (nam5 = multi-region US; immutable once set)."
  type        = string
  default     = "nam5"
}

variable "github_repo" {
  description = "owner/name of the repository allowed to deploy."
  type        = string
  default     = "juandsep/botjonh"
}

variable "billing_account" {
  type = string
}

variable "monthly_budget_usd" {
  type    = number
  default = 3
}

variable "timezone" {
  description = "IANA timezone for the scheduler jobs (e.g. America/Panama)."
  type        = string
  default     = "America/Panama"
}

# Set after the first `assistant-worker` deploy, then apply again to create the
# push subscription. Until then it is skipped.
variable "worker_url" {
  description = "URL of the deployed assistant-worker service."
  type        = string
  default     = ""
}

provider "google" {
  project               = var.project_id
  region                = var.region
  user_project_override = true
  billing_project       = var.project_id
}

data "google_project" "this" {}

resource "google_project_service" "apis" {
  for_each = toset([
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "cloudscheduler.googleapis.com",
    "firestore.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "pubsub.googleapis.com",
    "sheets.googleapis.com",
    "calendar-json.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
    "sts.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

# Firestore (native) for operational state.
resource "google_firestore_database" "db" {
  name        = "(default)"
  location_id = var.firestore_location
  type        = "FIRESTORE_NATIVE"
  depends_on  = [google_project_service.apis]
}

# Bucket for the budget-guard function source (no data bucket in this project).
resource "google_storage_bucket" "functions" {
  name                        = "${var.project_id}-functions"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  depends_on                  = [google_project_service.apis]
}

# Artifact Registry: one image repo, two services.
resource "google_artifact_registry_repository" "images" {
  repository_id = "assistant"
  location      = var.region
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]

  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }
  cleanup_policies {
    id     = "delete-old"
    action = "DELETE"
    condition {
      older_than = "2592000s" # 30 days
    }
  }
}

# Secrets. Values are added by hand, never through Terraform, so they stay out
# of state (see README):
#   printf '%s' "$VALUE" | gcloud secrets versions add NAME --data-file=-
locals {
  secrets = [
    "assistant-bot-token",      # Telegram bot token (@BotFather)
    "assistant-webhook-secret", # X-Telegram-Bot-Api-Secret-Token
    "assistant-webhook-path",   # webhook route secret (32 random chars)
    "assistant-deepseek-key",   # DeepSeek API key
  ]
}

resource "google_secret_manager_secret" "secret" {
  for_each  = toset(local.secrets)
  secret_id = each.value
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

# Service accounts.
locals {
  service_accounts = {
    webhook = "Verifies and publishes Telegram updates"
    worker  = "Consumes updates, calls the LLM, writes state"
    deploy  = "GitHub Actions deploys"
  }
}

resource "google_service_account" "sa" {
  for_each     = local.service_accounts
  account_id   = "assistant-${each.key}"
  display_name = each.value
  depends_on   = [google_project_service.apis]
}

# Pub/Sub topics.
resource "google_pubsub_topic" "updates" {
  name       = "assistant-updates"
  depends_on = [google_project_service.apis]
}

resource "google_pubsub_topic" "cron" {
  name       = "assistant-cron"
  depends_on = [google_project_service.apis]
}

# The webhook service only publishes updates.
resource "google_pubsub_topic_iam_member" "webhook_publishes_updates" {
  topic  = google_pubsub_topic.updates.name
  role   = "roles/pubsub.publisher"
  member = google_service_account.sa["webhook"].member
}

# The worker service runs as its own account and is triggered by push.
resource "google_project_iam_member" "worker" {
  for_each = toset([
    "roles/datastore.user", # Firestore native mode
  ])
  project = var.project_id
  role    = each.value
  member  = google_service_account.sa["worker"].member
}

resource "google_secret_manager_secret_iam_member" "worker_reads_secrets" {
  for_each  = toset(["assistant-bot-token", "assistant-deepseek-key"])
  secret_id = google_secret_manager_secret.secret[each.value].id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.sa["worker"].member
}

# The webhook deduplicates by update_id and consumes invite codes in Firestore,
# and reads only the two secrets that authenticate Telegram.
resource "google_project_iam_member" "webhook_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = google_service_account.sa["webhook"].member
}

resource "google_secret_manager_secret_iam_member" "webhook_reads_secrets" {
  for_each  = toset(["assistant-webhook-secret", "assistant-webhook-path"])
  secret_id = google_secret_manager_secret.secret[each.value].id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.sa["webhook"].member
}

# Weekly backup of Firestore and the ledger as JSON. The worker only creates
# objects; versioning plus a 90-day lifecycle keep old copies bounded.
resource "google_storage_bucket" "backup" {
  name                        = "${var.project_id}-backup"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  depends_on                  = [google_project_service.apis]

  versioning {
    enabled = true
  }
  lifecycle_rule {
    condition {
      age = 90
    }
    action {
      type = "Delete"
    }
  }
}

resource "google_storage_bucket_iam_member" "worker_writes_backup" {
  bucket = google_storage_bucket.backup.name
  role   = "roles/storage.objectCreator"
  member = google_service_account.sa["worker"].member
}

# Pub/Sub signs push requests with the worker account's OIDC token, so that
# account must be allowed to invoke the worker service.
resource "google_project_iam_member" "worker_invokes_run" {
  project = var.project_id
  role    = "roles/run.invoker"
  member  = google_service_account.sa["worker"].member
}

# Deploy may build images and deploy both services as the runtime accounts.
resource "google_project_iam_member" "deploy" {
  for_each = toset(["roles/run.admin", "roles/artifactregistry.writer"])
  project  = var.project_id
  role     = each.value
  member   = google_service_account.sa["deploy"].member
}

resource "google_service_account_iam_member" "deploy_acts_as" {
  for_each           = { for k in ["webhook", "worker"] : k => google_service_account.sa[k] }
  service_account_id = each.value.name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.sa["deploy"].member
}

# Push subscription from both topics to the worker, authenticated with OIDC.
# Gated on worker_url: create it after the first deploy.
resource "google_pubsub_subscription" "updates_push" {
  count = var.worker_url == "" ? 0 : 1
  name  = "assistant-updates-push"
  topic = google_pubsub_topic.updates.name

  ack_deadline_seconds = 60
  # A failing message is dropped after 10 minutes instead of 7 days.
  message_retention_duration = "600s"
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }
  push_config {
    push_endpoint = "${var.worker_url}/push"
    oidc_token {
      service_account_email = google_service_account.sa["worker"].email
    }
  }
}

resource "google_pubsub_subscription" "cron_push" {
  count = var.worker_url == "" ? 0 : 1
  name  = "assistant-cron-push"
  topic = google_pubsub_topic.cron.name

  ack_deadline_seconds = 120
  # A failing message is dropped after 10 minutes instead of 7 days.
  message_retention_duration = "600s"
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }
  push_config {
    push_endpoint = "${var.worker_url}/push"
    oidc_token {
      service_account_email = google_service_account.sa["worker"].email
    }
  }
}

# Cloud Scheduler publishes directly to the cron topic (no HTTP endpoints).
locals {
  jobs = {
    digest  = { schedule = "30 7 * * *", label = "morning digest" }
    checkin = { schedule = "0 21 * * *", label = "end-of-day checkin" }
    weekly  = { schedule = "0 19 * * 0", label = "weekly review" }
  }
}

resource "google_cloud_scheduler_job" "job" {
  for_each    = local.jobs
  name        = "assistant-${each.key}"
  description = each.value.label
  schedule    = each.value.schedule
  time_zone   = var.timezone

  pubsub_target {
    topic_name = google_pubsub_topic.cron.id
    data       = base64encode("{\"job\":\"${each.key}\"}")
  }
  depends_on = [google_project_service.apis]
}

# The Cloud Scheduler service agent needs publish permission on the cron topic.

resource "google_pubsub_topic_iam_member" "scheduler_publishes_cron" {
  topic  = google_pubsub_topic.cron.name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-cloudscheduler.iam.gserviceaccount.com"
}

# GitHub Workload Identity Federation: no service account keys.
resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github"
  depends_on                = [google_project_service.apis]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-oidc"
  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
  }
  attribute_condition = "assertion.repository == '${var.github_repo}'"
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "github_impersonates_deploy" {
  service_account_id = google_service_account.sa["deploy"].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repo}"
}

# Monthly budget that unlinks billing once spend reaches it. Shared module from
# portfolio-infra, pinned to a commit.
module "budget_guard" {
  source          = "git::https://github.com/juandsep/portfolio-infra.git//modules/budget-guard?ref=a46ece80773fa45aeeb0bd3109b2268ca05949ce"
  project_id      = var.project_id
  region          = var.region
  billing_account = var.billing_account
  amount_usd      = var.monthly_budget_usd
  source_bucket   = google_storage_bucket.functions.name
}

# Values for the GitHub repository variables (see README).
output "github_variables" {
  value = {
    GCP_PROJECT_ID    = var.project_id
    GCP_REGION        = var.region
    GCP_ARTIFACT_REPO = google_artifact_registry_repository.images.repository_id
    GCP_WIF_PROVIDER  = google_iam_workload_identity_pool_provider.github.name
    GCP_DEPLOY_SA     = google_service_account.sa["deploy"].email
    GCP_WEBHOOK_SA    = google_service_account.sa["webhook"].email
    GCP_WORKER_SA     = google_service_account.sa["worker"].email
    BACKUP_BUCKET     = google_storage_bucket.backup.name
  }
}

# Hand these to portfolio-infra so it can grant MLflow access.
output "mlflow_clients" {
  value = {
    worker = google_service_account.sa["worker"].email
  }
}
