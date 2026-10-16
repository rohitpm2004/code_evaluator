# Playwright's own image ships the OS libraries Chromium needs (libnss3,
# libatk, etc.) that a plain Node buildpack doesn't have. Version pinned to
# match the resolved "playwright" package version in package-lock.json.
FROM mcr.microsoft.com/playwright:v1.60.0-noble

WORKDIR /app

COPY package*.json requirements.txt ./
RUN npm ci --omit=dev
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip && \
    (pip install --no-cache-dir -r requirements.txt --break-system-packages 2>/dev/null || pip install --no-cache-dir -r requirements.txt) && \
    rm -rf /var/lib/apt/lists/*

COPY . .

ENV NODE_ENV=production
EXPOSE 3000

CMD ["node", "server.js"]
