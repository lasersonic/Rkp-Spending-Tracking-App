#!/usr/bin/env bash
# Deploy the bot to Cloud Run. Made for Google Cloud Shell (gcloud is built in).
#
#   bash scripts/deploy.sh
#
# Run it in the project folder, next to .env, sa.json and drive_token.json.
# Safe to re-run: it adds new secret versions and redeploys. To update only the
# allowlist after you learn your LINE user ID, edit .env and run it again.
set -euo pipefail

[ -f .env ] || { echo "No .env here. cd into the project folder first." >&2; exit 1; }

# Read .env without executing it (values may contain + / =).
declare -A ENV
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%$'\r'}"
  [[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
  ENV["${line%%=*}"]="${line#*=}"
done < .env

need() { [ -n "${ENV[$1]:-}" ] || { echo "Missing $1 in .env" >&2; exit 1; }; }
for k in LINE_CHANNEL_SECRET LINE_CHANNEL_ACCESS_TOKEN GOOGLE_SA_JSON GOOGLE_DRIVE_TOKEN_JSON SHEET_ID DRIVE_FOLDER_ID; do need "$k"; done
SA_FILE="${ENV[GOOGLE_SA_JSON]}"
TOKEN_FILE="${ENV[GOOGLE_DRIVE_TOKEN_JSON]}"
[ -f "$SA_FILE" ] || { echo "Missing $SA_FILE" >&2; exit 1; }
[ -f "$TOKEN_FILE" ] || { echo "Missing $TOKEN_FILE" >&2; exit 1; }

PROJECT="${PROJECT:-$(sed -n 's/.*"project_id": *"\([^"]*\)".*/\1/p' "$SA_FILE")}"
REGION="${REGION:-asia-southeast1}"
SERVICE="${SERVICE:-expense-bot}"
echo "==> Project $PROJECT, region $REGION, service $SERVICE"
gcloud config set project "$PROJECT" --quiet

echo "==> Enabling APIs (first time takes a minute)"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com sheets.googleapis.com drive.googleapis.com vision.googleapis.com --quiet

NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
RUNTIME_SA="${NUMBER}-compute@developer.gserviceaccount.com"
# Newer projects build with the compute service account, which needs this role.
gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$RUNTIME_SA" \
  --role=roles/run.builder --condition=None --quiet >/dev/null

put_secret() {  # name, then the value on stdin
  gcloud secrets describe "$1" >/dev/null 2>&1 || gcloud secrets create "$1" --replication-policy=automatic --quiet >/dev/null
  gcloud secrets versions add "$1" --data-file=- --quiet >/dev/null
  gcloud secrets add-iam-policy-binding "$1" --member="serviceAccount:$RUNTIME_SA" \
    --role=roles/secretmanager.secretAccessor --quiet >/dev/null
  echo "    $1"
}
echo "==> Storing secrets in Secret Manager"
for k in LINE_CHANNEL_SECRET LINE_CHANNEL_ACCESS_TOKEN; do printf '%s' "${ENV[$k]}" | put_secret "$k"; done
put_secret GOOGLE_SA_JSON < "$SA_FILE"
put_secret GOOGLE_DRIVE_TOKEN_JSON < "$TOKEN_FILE"

# Plain settings go in as env vars (a YAML file, so commas in the allowlist are fine).
ENV_FILE="$(mktemp)"
trap 'rm -f "$ENV_FILE"' EXIT
{
  echo "TZ: \"${ENV[TZ]:-Asia/Bangkok}\""
  echo "SHEET_ID: \"${ENV[SHEET_ID]}\""
  echo "DRIVE_FOLDER_ID: \"${ENV[DRIVE_FOLDER_ID]}\""
  echo "ALLOWED_USER_IDS: \"${ENV[ALLOWED_USER_IDS]:-}\""
  echo "OWNER_USER_ID: \"${ENV[OWNER_USER_ID]:-}\""
} > "$ENV_FILE"

echo "==> Building and deploying (3-5 minutes)"
gcloud run deploy "$SERVICE" --source . --region "$REGION" --quiet \
  --allow-unauthenticated --min-instances 0 --max-instances 1 --no-cpu-throttling \
  --memory 512Mi --timeout 300 \
  --env-vars-file "$ENV_FILE" \
  --set-secrets "LINE_CHANNEL_SECRET=LINE_CHANNEL_SECRET:latest,LINE_CHANNEL_ACCESS_TOKEN=LINE_CHANNEL_ACCESS_TOKEN:latest,GOOGLE_SA_JSON=GOOGLE_SA_JSON:latest,GOOGLE_DRIVE_TOKEN_JSON=GOOGLE_DRIVE_TOKEN_JSON:latest"

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
echo "==> Service is up: $URL"

echo "==> Pointing the LINE webhook at $URL/callback"
curl -sS -X PUT https://api.line.me/v2/bot/channel/webhook/endpoint \
  -H "Authorization: Bearer ${ENV[LINE_CHANNEL_ACCESS_TOKEN]}" -H "Content-Type: application/json" \
  -d "{\"endpoint\":\"$URL/callback\"}" && echo

cat <<EOF

Done. Last steps, by hand:
  1. LINE Developers console > your channel > Messaging API: turn "Use webhook" ON, press Verify.
  2. LINE Official Account Manager > Response settings: turn auto-reply messages OFF.
  3. Message the bot. It replies with your LINE user ID. Put it in .env as
     ALLOWED_USER_IDS=U..., then run this script again.

Optional full check:  pip install -q -r requirements.txt && python3 -m scripts.check_setup --service-url $URL
EOF
