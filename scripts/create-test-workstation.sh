#!/usr/bin/env bash
# Create a throwaway GCP Cloud Workstation to test the agy plugin end-to-end
# (install -> auth -> use), isolated from your main machine.
#
# ==================================================================
#  COST WARNING - this creates BILLABLE infrastructure:
#   - Workstation CLUSTER control-plane: ~$0.20/hr (~$144/month), billed
#     24/7 for as long as the cluster EXISTS (even with no workstation running).
#   - Running workstation: Compute Engine VM rate + $0.05/vCPU-hr management fee
#     (only while the workstation is running).
#   - Persistent disk: billed 24/7 even while the workstation is stopped.
#   The idle timeout stops the VM when inactive, but the cluster fee + disk keep
#   accruing until you DELETE the cluster (see TEARDOWN at the end).
# ==================================================================
#
# Prereqs (do these first, interactively):
#   gcloud auth login
#   gcloud auth application-default login
#   # billing enabled on the project + Cloud Workstations admin rights
#
# Usage:
#   PROJECT=my-proj REGION=us-central1 ./scripts/create-test-workstation.sh
#
# Overridable env (defaults in parentheses):
#   PROJECT  (gcloud config project)   REGION (us-central1)
#   CLUSTER  (agy-test-cluster)        CONFIG (agy-test-config)
#   WS       (agy-test-ws)             MACHINE (e2-standard-4)   IDLE (1800s)

set -euo pipefail

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
REGION="${REGION:-us-central1}"
CLUSTER="${CLUSTER:-agy-test-cluster}"
CONFIG="${CONFIG:-agy-test-config}"
WS="${WS:-agy-test-ws}"
MACHINE="${MACHINE:-e2-standard-4}"
IDLE="${IDLE:-1800s}"

if [[ -z "${PROJECT}" ]]; then
  echo "ERROR: no project set. Use PROJECT=... or run 'gcloud config set project <id>'." >&2
  exit 1
fi

cat <<BANNER
==================================================================
 Creating a BILLABLE GCP Cloud Workstation:
   project=${PROJECT}  region=${REGION}  machine=${MACHINE}
   cluster=${CLUSTER}  config=${CONFIG}  workstation=${WS}
 The cluster control-plane fee (~\$0.20/hr, ~\$144/mo) accrues 24/7 until
 you DELETE the cluster (see TEARDOWN at the end). Ctrl-C now to abort.
==================================================================
BANNER
sleep 5

gcloud services enable workstations.googleapis.com --project="${PROJECT}"

# True if the given workstations resource already exists (idempotent re-runs).
_ws_exists() { gcloud workstations "$@" --project="${PROJECT}" --region="${REGION}" >/dev/null 2>&1; }

if _ws_exists clusters describe "${CLUSTER}"; then
  echo "cluster ${CLUSTER} already exists - skipping"
else
  echo "creating cluster ${CLUSTER} (can take up to ~20 min)..."
  gcloud workstations clusters create "${CLUSTER}" --project="${PROJECT}" --region="${REGION}"
fi

if _ws_exists configs describe "${CONFIG}" --cluster="${CLUSTER}"; then
  echo "config ${CONFIG} already exists - skipping"
else
  gcloud workstations configs create "${CONFIG}" \
    --project="${PROJECT}" --region="${REGION}" --cluster="${CLUSTER}" \
    --machine-type="${MACHINE}" --idle-timeout="${IDLE}"
fi

if _ws_exists describe "${WS}" --cluster="${CLUSTER}" --config="${CONFIG}"; then
  echo "workstation ${WS} already exists - skipping"
else
  gcloud workstations create "${WS}" \
    --project="${PROJECT}" --region="${REGION}" --cluster="${CLUSTER}" --config="${CONFIG}"
fi

gcloud workstations start "${WS}" \
  --project="${PROJECT}" --region="${REGION}" --cluster="${CLUSTER}" --config="${CONFIG}"

echo
echo "Workstation started. Open it in the browser at:"
gcloud workstations describe "${WS}" \
  --project="${PROJECT}" --region="${REGION}" --cluster="${CLUSTER}" --config="${CONFIG}" \
  --format="value(host)" 2>/dev/null || true
echo "Or SSH: gcloud workstations ssh ${WS} --project=${PROJECT} --region=${REGION} --cluster=${CLUSTER} --config=${CONFIG}"
echo
echo "NEXT: follow scripts/README.md to install Claude Code + agy on the workstation."
echo
echo "TEARDOWN (stops ALL charges - deletes cluster, config, workstation, disks):"
echo "  gcloud workstations clusters delete ${CLUSTER} --project=${PROJECT} --region=${REGION}"
