#!/usr/bin/env bash
set -euo pipefail

# One-time setup: store the OpenAI API key in Secret Manager.
# Cloud Run reads it as an env var at runtime — never visible in deploy commands or logs.
#
# Usage: bash deploy/setup-secrets.sh <your-openai-api-key>

if [ $# -lt 1 ]; then
    echo "Usage: $0 <openai-api-key>"
    exit 1
fi

API_KEY="$1"

echo "Enabling Secret Manager API..."
gcloud services enable secretmanager.googleapis.com

echo "Creating secret 'crtv-openai-api-key'..."
echo -n "${API_KEY}" | gcloud secrets create crtv-openai-api-key \
    --data-file=- \
    --replication-policy=automatic 2>/dev/null \
|| echo -n "${API_KEY}" | gcloud secrets versions add crtv-openai-api-key --data-file=-

# Grant Cloud Run's service account access
PROJECT_NUMBER=$(gcloud projects describe "$(gcloud config get-value project)" --format="value(projectNumber)")
SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

gcloud secrets add-iam-policy-binding crtv-openai-api-key \
    --member="serviceAccount:${SA}" \
    --role="roles/secretmanager.secretAccessor" \
    --quiet

echo ""
echo "Done. The secret is stored securely and will be injected into Cloud Run at deploy time."
