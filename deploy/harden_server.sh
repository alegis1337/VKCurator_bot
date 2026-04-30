#!/usr/bin/env bash
# Базовый хардненинг Ubuntu 24.04 LTS для VK Curator Bot.
# Запускается под sudo один раз после первого логина на новом сервере.
# Не запускать вслепую — прочитать и подставить SSH_USER, SSH_PORT.

set -euo pipefail

SSH_USER="${SSH_USER:-ubuntu}"
SSH_PORT="${SSH_PORT:-22}"

echo "[1/7] Обновление пакетов и unattended-upgrades..."
apt update
apt upgrade -y
apt install -y unattended-upgrades fail2ban ufw
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "[2/7] UFW: разрешаем только SSH..."
ufw default deny incoming
ufw default allow outgoing
ufw allow "${SSH_PORT}/tcp" comment 'SSH'
ufw --force enable

echo "[3/7] Fail2ban: защита SSH от перебора..."
cat > /etc/fail2ban/jail.d/sshd.local <<EOF
[sshd]
enabled = true
port = ${SSH_PORT}
maxretry = 5
findtime = 10m
bantime = 1h
EOF
systemctl restart fail2ban

echo "[4/7] SSH: запрет логина по паролю и под root..."
SSHD_CFG=/etc/ssh/sshd_config
cp "$SSHD_CFG" "${SSHD_CFG}.bak.$(date +%s)"
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' "$SSHD_CFG"
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' "$SSHD_CFG"
sed -i 's/^#\?PubkeyAuthentication.*/PubkeyAuthentication yes/' "$SSHD_CFG"
sed -i 's/^#\?ChallengeResponseAuthentication.*/ChallengeResponseAuthentication no/' "$SSHD_CFG"
echo "ВНИМАНИЕ: убедитесь, что SSH-ключ для ${SSH_USER} уже добавлен!"
echo "Перезапуск SSH сейчас не делаем — сделайте вручную после проверки доступа."

echo "[5/7] PostgreSQL: только localhost..."
PG_HBA=$(ls /etc/postgresql/*/main/pg_hba.conf 2>/dev/null | head -n1 || true)
PG_CONF=$(ls /etc/postgresql/*/main/postgresql.conf 2>/dev/null | head -n1 || true)
if [[ -n "$PG_CONF" ]]; then
  sed -i "s/^#\?listen_addresses.*/listen_addresses = 'localhost'/" "$PG_CONF"
  systemctl restart postgresql
fi

echo "[6/7] Файловые права на секреты..."
PROJECT_DIR="/opt/vk-curator-bot"
if [[ -f "${PROJECT_DIR}/.env" ]]; then
  chown "${SSH_USER}:${SSH_USER}" "${PROJECT_DIR}/.env"
  chmod 600 "${PROJECT_DIR}/.env"
fi
if [[ -f "${PROJECT_DIR}/credentials.json" ]]; then
  chown "${SSH_USER}:${SSH_USER}" "${PROJECT_DIR}/credentials.json"
  chmod 600 "${PROJECT_DIR}/credentials.json"
fi

echo "[7/7] Готово. Проверь статус:"
ufw status verbose
systemctl status fail2ban --no-pager -l | head -n 10
echo ""
echo "Финальный шаг (только после проверки SSH-ключа):"
echo "  sudo systemctl restart ssh"
