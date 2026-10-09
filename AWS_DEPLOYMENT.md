# AWS EC2 Deployment & GitHub Actions Auto-Deploy Guide

This guide walks you through pushing `central-evaluator` to your personal GitHub repository and setting up **zero-downtime auto-deployment** to an AWS EC2 instance using GitHub Actions.

---

## 1. Push Code to Your Personal GitHub Account

Currently, your local repository may be linked to an existing remote. Follow these steps in your terminal to push it to your own GitHub account:

1. Go to [GitHub](https://github.com/new) and click **"New repository"**.
   - **Repository name:** `central-evaluator`
   - **Visibility:** Private (recommended) or Public.
   - **Do NOT initialize** with README, .gitignore, or license (we already have them).
   - Click **"Create repository"**.

2. In your terminal (inside `c:\Users\kirsa\OneDrive\Desktop\code guru oct\central-evaluator`), run:

```bash
# 1. Switch to or create the main branch
git branch -M main

# 2. Add your personal GitHub repository as remote (replace YOUR_USERNAME)
git remote set-url origin https://github.com/YOUR_USERNAME/central-evaluator.git
# Or if you want to add a fresh remote:
# git remote add my-origin https://github.com/YOUR_USERNAME/central-evaluator.git

# 3. Stage all changes including the new GitHub Actions workflow
git add .

# 4. Commit all verified bugfixes & CI/CD workflow
git commit -m "feat: production evaluators, bugfixes, and EC2 auto-deploy workflow"

# 5. Push to your GitHub repository
git push -u origin main
```

---

## 2. Launching Your AWS EC2 Instance

1. Open the [AWS EC2 Console](https://console.aws.amazon.com/ec2).
2. Click **"Launch Instance"**:
   - **Name:** `central-evaluator-prod`
   - **OS (AMI):** **Ubuntu 24.04 LTS** or **Ubuntu 22.04 LTS** (64-bit x86).
   - **Instance Type:** `t3.medium` (2 vCPU, 4 GiB RAM) or `t3.large` (recommended for handling concurrent Docker builds and Playwright browsers).
   - **Key Pair:** Create a new key pair (e.g. `central-eval-key.pem`) and download it.
   - **Storage:** 20–30 GiB gp3 (sufficient for Docker images and Node dependencies).
   - **Network / Security Group:** Allow the following inbound ports:
     - **SSH (Port 22):** Anywhere (`0.0.0.0/0`) or Your IP.
     - **Custom TCP (Port 3000):** Anywhere (`0.0.0.0/0`) — used for LMS webhook & evaluation API.
     - **HTTP (Port 80) & HTTPS (Port 443):** If you plan to attach an Nginx reverse proxy / domain.

3. Click **"Launch Instance"**.
4. *(Optional but Recommended)* Allocate an **Elastic IP** in AWS EC2 and associate it with this instance so the public IP never changes upon reboot.

---

## 3. Provision the EC2 Server (One-Time Setup)

Connect to your EC2 instance via SSH:

```bash
# On Windows PowerShell or terminal:
ssh -i "path/to/central-eval-key.pem" ubuntu@<YOUR_EC2_PUBLIC_IP>
```

Once connected, run the automated provisioning script to install Node.js 20, Docker, Redis, PM2, and dependencies:

```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/central-evaluator.git /home/ubuntu/central-evaluator

# Run the automated setup script
cd /home/ubuntu/central-evaluator
chmod +x scripts/setup-ec2.sh
./scripts/setup-ec2.sh

# Log out and log back in so Docker permissions take effect
exit
```

---

## 4. Configure GitHub Actions Secrets

In your GitHub repository:
1. Navigate to **Settings** > **Secrets and variables** > **Actions**.
2. Click **"New repository secret"** and add the following secrets:

| Secret Name | Value | Description |
|---|---|---|
| `EC2_HOST` | `18.xxx.xxx.xxx` | Public IPv4 address or Elastic IP of your EC2 instance |
| `EC2_USER` | `ubuntu` | Default Ubuntu username |
| `EC2_SSH_KEY` | `-----BEGIN RSA PRIVATE KEY-----...` | Entire contents of your downloaded `.pem` key file |
| `EC2_APP_DIR` | `/home/ubuntu/central-evaluator` | Path to the repository on EC2 |
| `ENV_FILE` | *(See below)* | Your production `.env` variables |

### Example `ENV_FILE` Secret Content:
```env
PORT=3000
NODE_ENV=production
BASE_URL=http://<YOUR_EC2_PUBLIC_IP>:3000

# AI Provider Keys
GROQ_API_KEY=gsk_...
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_MODEL=deepseek-v4-flash

# Redis Connection (localhost since redis-server runs on the EC2 instance)
REDIS_HOST=localhost
REDIS_PORT=6379

# Execution & Sandbox
SANDBOX_MODE=docker
DEFAULT_EXPECTED_URL=http://localhost:3000
```

---

## 5. How Auto-Deploy Works

Every time you push code to the `main` branch:

1. **GitHub Actions triggers automatically** via [deploy.yml](file:///.github/workflows/deploy.yml).
2. It securely connects to your EC2 instance via SSH.
3. Pulls latest commits (`git reset --hard origin/main`).
4. Updates `.env` from your secret.
5. Installs updated dependencies (`npm ci --omit=dev`).
6. Updates Playwright Chromium and pre-pulls Docker sandbox images (`node:18-alpine`, `python:3.11-alpine`).
7. Reloads the server zero-downtime using PM2 (`pm2 reload central-evaluator`).

You can also trigger a manual deploy at any time by clicking **"Run workflow"** under the **Actions** tab in GitHub!
