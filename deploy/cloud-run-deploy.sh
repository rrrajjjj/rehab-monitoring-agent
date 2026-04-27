#!/usr/bin/env bash
set -euo pipefail

# Cloud Run deployment script for CRTV.
# Prerequisites:
#   - gcloud CLI installed and authenticated (gcloud auth login)
#   - A GCP project selected (gcloud config set project <PROJECT_ID>)
#   - APIs enabled: Cloud Run, Cloud Build, Cloud Storage
#     gcloud services enable run.googleapis.com cloudbuild.googleapis.com storage.googleapis.com

PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
REGION="${CRTV_REGION:-us-central1}"
SERVICE_NAME="${CRTV_SERVICE:-crtv}"
BUCKET_NAME="${CRTV_BUCKET:-${PROJECT_ID}-crtv-config}"

echo "Project:  ${PROJECT_ID}"
echo "Region:   ${REGION}"
echo "Service:  ${SERVICE_NAME}"
echo "Bucket:   ${BUCKET_NAME}"
echo ""

# --- 1. Create config bucket (idempotent) ---
if ! gcloud storage buckets describe "gs://${BUCKET_NAME}" &>/dev/null; then
    echo "Creating GCS bucket for chatbot config..."
    gcloud storage buckets create "gs://${BUCKET_NAME}" \
        --location="${REGION}" \
        --uniform-bucket-level-access
    echo "Bucket created."
else
    echo "Bucket gs://${BUCKET_NAME} already exists."
fi

# --- 2. Build and deploy ---
echo ""
echo "Deploying to Cloud Run..."

# Navigate to project root (one level up from deploy/)
cd "$(dirname "$0")/.."

gcloud run deploy "${SERVICE_NAME}" \
    --source=. \
    --region="${REGION}" \
    --port=8001 \
    --memory=1Gi \
    --cpu=1 \
    --min-instances=0 \
    --max-instances=3 \
    --timeout=60 \
    --set-env-vars="CRTV_LLM_PROVIDER=openai,CRTV_OPENAI_MODEL=gpt-5.2,CRTV_OPENAI_REASONING_EFFORT=high,CRTV_OPENAI_TIMEOUT=120,CRTV_CHAT_TIMEOUT=30,CRTV_LOG_LEVEL=INFO" \
    --update-secrets="CRTV_OPENAI_API_KEY=crtv-openai-api-key:latest" \
    --add-volume=name=config-vol,type=cloud-storage,bucket="${BUCKET_NAME}" \
    --add-volume-mount=volume=config-vol,mount-path=/app/chatbot/config \
    --no-allow-unauthenticated

echo ""
echo "=== Deployed ==="
URL=$(gcloud run services describe "${SERVICE_NAME}" --region="${REGION}" --format="value(status.url)")
echo "Service URL: ${URL}"
echo ""
echo "To allow specific users:"
echo "  gcloud run services add-iam-policy-binding ${SERVICE_NAME} \\"
echo "    --region=${REGION} \\"
echo "    --member='user:someone@gmail.com' \\"
echo "    --role='roles/run.invoker'"
echo ""
echo "To map a custom domain:"
echo "  gcloud run domain-mappings create \\"
echo "    --service=${SERVICE_NAME} \\"
echo "    --domain=your-app.yourdomain.com \\"
echo "    --region=${REGION}"
echo ""
echo "Then add the DNS records shown in the output."
