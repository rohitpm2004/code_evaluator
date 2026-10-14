#!/usr/bin/env bash
# ==============================================================================
# AWS EC2 Ubuntu Bootstrap Script for central-evaluator
# Run this once on a fresh Ubuntu 22.04 / 24.04 LTS EC2 instance:
#   chmod +x scripts/setup-ec2.sh && ./scripts/setup-ec2.sh
# ==============================================================================

set -e

echo "🚀 Starting EC2 server provisioning for central-evaluator..."

# 1. Update OS packages
sudo apt-get update -y
sudo apt-get upgrade -y
sudo apt-get install -y curl wget git build-essential ufw software-properties-common apt-transport-https ca-certificates gnupg

# 2. Install Node.js 20 LTS
echo "📦 Installing Node.js 20 LTS..."
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs

# 3. Install PM2 globally
echo "⚙️ Installing PM2 process manager..."
sudo npm install -g pm2
pm2 startup systemd -u $USER --hp /home/$USER | sudo bash || true

# 4. Install Redis Server
echo "🔴 Installing & starting Redis..."
sudo apt-get install -y redis-server
sudo systemctl enable redis-server
sudo systemctl restart redis-server

# 5. Install Docker & Docker Compose
echo "🐳 Installing Docker Engine..."
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update -y
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Allow current user to run Docker without sudo
sudo usermod -aG docker $USER
sudo systemctl enable docker
sudo systemctl restart docker

# 6. Pre-pull Docker sandbox images for instant evaluations
echo "📥 Pre-pulling sandbox Docker images..."
sudo docker pull node:20-slim || true
sudo docker pull python:3.11-alpine || true
if [ -f "docker/Dockerfile.excel" ]; then
  echo "📊 Building Excel isolated sandbox container..."
  sudo docker build -f docker/Dockerfile.excel -t eval-excel-sandbox . || true
fi

# 7. Install Python dependencies and LibreOffice for host evaluation
echo "🐍 Installing Python3, openpyxl, and LibreOffice Calc..."
sudo apt-get install -y python3 python3-pip python3-openpyxl python3-pandas libreoffice-calc fonts-dejavu-core
pip3 install openpyxl pandas --break-system-packages || pip3 install openpyxl pandas || true

# 8. Create application directory
APP_DIR="/home/$USER/central-evaluator"
mkdir -p "$APP_DIR"
mkdir -p "$APP_DIR/logs"

echo "=============================================================================="
echo "🎉 EC2 provisioning completed successfully!"
echo "Node version:   $(node -v)"
echo "NPM version:    $(npm -v)"
echo "Docker version: $(docker --version)"
echo "Redis status:   $(systemctl is-active redis-server)"
echo "App directory:  $APP_DIR"
echo "=============================================================================="
echo "NOTE: Please log out and log back in to apply Docker group permissions:"
echo "      exit"
echo "=============================================================================="
