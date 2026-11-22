#!/usr/bin/env bash
# Idempotently configure a monthly, project-scoped GCP billing budget.
set -euo pipefail

PROJECT="${PROJECT:?set PROJECT to the GCP project id}"
BUDGET_AMOUNT_USD="${BUDGET_AMOUNT_USD:-25}"
BUDGET_DISPLAY_NAME="${BUDGET_DISPLAY_NAME:-Metis monthly project budget}"

if [[ ! "$BUDGET_AMOUNT_USD" =~ ^[0-9]+([.][0-9]{1,2})?$ ]] || [ "$BUDGET_AMOUNT_USD" = "0" ]; then
  echo "BUDGET_AMOUNT_USD must be a positive USD amount" >&2
  exit 2
fi

g() { gcloud --quiet --project "$PROJECT" "$@"; }

g services enable billingbudgets.googleapis.com monitoring.googleapis.com

BILLING_ACCOUNT="${BILLING_ACCOUNT:-$(g billing projects describe "$PROJECT" --format='value(billingAccountName.basename())')}"
if [ -z "$BILLING_ACCOUNT" ]; then
  echo "project $PROJECT has no linked billing account" >&2
  exit 1
fi

channel=""
if [ -n "${NOTIFY_EMAIL:-}" ]; then
  api="https://monitoring.googleapis.com/v3/projects/$PROJECT/notificationChannels"
  auth="Authorization: Bearer $(gcloud auth print-access-token)"
  json_name() { python3 -c 'import json,sys; d=json.load(sys.stdin); print((d.get("notificationChannels") or [d])[0].get("name",""))'; }
  channel="$(curl -fsS -H "$auth" -G "$api" \
    --data-urlencode "filter=type=\"email\" AND labels.email_address=\"$NOTIFY_EMAIL\"" | json_name)"
  if [ -z "$channel" ]; then
    channel="$(curl -fsS -H "$auth" -H 'Content-Type: application/json' -X POST "$api" \
      -d "$(python3 -c 'import json,sys; print(json.dumps({"type":"email","displayName":"Metis alerts ("+sys.argv[1]+")","labels":{"email_address":sys.argv[1]}}))' "$NOTIFY_EMAIL")" \
      | json_name)"
  fi
  [ -n "$channel" ] || { echo "could not create notification channel" >&2; exit 1; }
fi

budget="$(g billing budgets list --billing-account="$BILLING_ACCOUNT" \
  --filter="displayName=\"$BUDGET_DISPLAY_NAME\"" --format='value(name)' --limit=1)"
channel_args=()
if [ -n "$channel" ]; then
  channel_args+=(--notifications-rule-monitoring-notification-channels="$channel")
fi

if [ -z "$budget" ]; then
  budget="$(g billing budgets create \
    --billing-account="$BILLING_ACCOUNT" \
    --display-name="$BUDGET_DISPLAY_NAME" \
    --budget-amount="${BUDGET_AMOUNT_USD}USD" \
    --calendar-period=month \
    --filter-projects="projects/$PROJECT" \
    --threshold-rule=percent=0.50 \
    --threshold-rule=percent=0.80 \
    --threshold-rule=percent=1.00 \
    --threshold-rule=percent=1.00,basis=forecasted-spend \
    "${channel_args[@]}" \
    --format='value(name)')"
else
  g billing budgets update "$budget" \
    --billing-account="$BILLING_ACCOUNT" \
    --display-name="$BUDGET_DISPLAY_NAME" \
    --budget-amount="${BUDGET_AMOUNT_USD}USD" \
    --calendar-period=month \
    --filter-projects="projects/$PROJECT" \
    --clear-threshold-rules \
    --add-threshold-rule=percent=50 \
    --add-threshold-rule=percent=80 \
    --add-threshold-rule=percent=100 \
    --add-threshold-rule=percent=100,basis=forecasted-spend \
    "${channel_args[@]}" >/dev/null
fi

g billing budgets describe "$budget" --billing-account="$BILLING_ACCOUNT" \
  --format='yaml(name,displayName,amount,budgetFilter,thresholdRules,notificationsRule)'

echo "Budget alerts notify the configured recipients; they do not cap or disable spending."
