FROM node:20-bookworm-slim

WORKDIR /app

# Download Chrome for Testing ourselves (with retries + mirror fallback) instead of
# puppeteer's postinstall, which is flaky (ECONNRESET) inside docker builds.
ENV PUPPETEER_SKIP_DOWNLOAD=true

RUN apt-get update && apt-get install -y \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libxcomposite1 \
    libxrandr2 \
    libxdamage1 \
    libxkbcommon0 \
    libgbm1 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libasound2 \
    libxshmfence1 \
    libnss3-dev \
    fonts-liberation \
    libappindicator3-1 \
    xdg-utils \
    wget \
    curl \
    unzip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY package.json package-lock.json ./
COPY *.js ./

RUN npm install --production

# Chrome for Testing 142.0.7444.61 (matches puppeteer 24.29.1), npmmirror first, official fallback
RUN set -e; \
    ( curl -fSL --retry 5 --retry-all-errors --connect-timeout 10 -o /tmp/chrome.zip \
        "https://cdn.npmmirror.com/binaries/chrome-for-testing/142.0.7444.61/linux64/chrome-linux64.zip" \
      || curl -fSL --retry 5 --retry-all-errors --connect-timeout 10 -o /tmp/chrome.zip \
        "https://storage.googleapis.com/chrome-for-testing-public/142.0.7444.61/linux64/chrome-linux64.zip" ) \
    && unzip -q /tmp/chrome.zip -d /opt \
    && rm /tmp/chrome.zip \
    && ln -sf /opt/chrome-linux64/chrome /usr/local/bin/chrome

ENV PUPPETEER_EXECUTABLE_PATH=/usr/local/bin/chrome

CMD ["node", "app.js"]
