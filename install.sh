#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_USER="${OCI_CLAIMER_USER:-${SUDO_USER:-$(id -un)}}"
SERVICE_GROUP="$(id -gn "${SERVICE_USER}")"

if [[ "${EUID}" -eq 0 && -z "${SUDO_USER:-}" && "${SERVICE_USER}" == "root" ]]; then
  echo "Por seguridad, ejecuta este instalador desde tu usuario normal con sudo." >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "Falta python3. Instálalo primero (Ubuntu: sudo apt update && sudo apt install -y python3 python3-venv)." >&2
  exit 1
fi

sudo install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0750 /opt/oci-a1-claimer
sudo install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0750 /etc/oci-a1-claimer
sudo install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0700 /var/lib/oci-a1-claimer

sudo install -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0750 \
  "${SOURCE_DIR}/oci_a1_claimer.py" /opt/oci-a1-claimer/oci_a1_claimer.py
sudo install -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0644 \
  "${SOURCE_DIR}/requirements.txt" /opt/oci-a1-claimer/requirements.txt

if [[ ! -x /opt/oci-a1-claimer/.venv/bin/python ]]; then
  sudo -u "${SERVICE_USER}" python3 -m venv /opt/oci-a1-claimer/.venv
fi
sudo -u "${SERVICE_USER}" /opt/oci-a1-claimer/.venv/bin/python -m pip install --upgrade pip
sudo -u "${SERVICE_USER}" /opt/oci-a1-claimer/.venv/bin/pip install -r /opt/oci-a1-claimer/requirements.txt

SERVICE_TMP="$(mktemp)"
trap 'rm -f "${SERVICE_TMP}"' EXIT
sed \
  -e "s/__OCI_A1_USER__/${SERVICE_USER}/g" \
  -e "s/__OCI_A1_GROUP__/${SERVICE_GROUP}/g" \
  "${SOURCE_DIR}/systemd/oci-a1-claimer.service" > "${SERVICE_TMP}"
sudo install -o root -g root -m 0644 "${SERVICE_TMP}" \
  /etc/systemd/system/oci-a1-claimer.service

if [[ -f "${SOURCE_DIR}/.env" ]]; then
  sudo install -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0600 \
    "${SOURCE_DIR}/.env" /etc/oci-a1-claimer/claimer.env
elif [[ ! -f /etc/oci-a1-claimer/claimer.env ]]; then
  sudo install -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0600 \
    "${SOURCE_DIR}/.env.example" /etc/oci-a1-claimer/claimer.env
fi

sudo systemctl daemon-reload
echo
echo "Instalación terminada para el usuario ${SERVICE_USER}."
echo "Edita /etc/oci-a1-claimer/claimer.env y después ejecuta:"
echo "  sudo systemctl enable --now oci-a1-claimer"
echo "  sudo journalctl -u oci-a1-claimer -f"
