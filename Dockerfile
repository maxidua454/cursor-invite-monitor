FROM python:3.11-slim

# Chrome + Xvfb dependencies
RUN apt-get update && apt-get install -y \
    wget gnupg unzip curl \
    xvfb xauth dbus-x11 \
    libnss3 libatk-bridge2.0-0 libdrm2 libxcomposite1 \
    libxdamage1 libxrandr2 libgbm1 libasound2 \
    libpangocairo-1.0-0 libgtk-3-0 libxshmfence1 \
    libx11-xcb1 libxcb1 libxext6 libxfixes3 \
    libxi6 libxrender1 libxtst6 libxss1 \
    fonts-liberation xdg-utils ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Google Chrome
RUN wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && dpkg -i google-chrome-stable_current_amd64.deb || apt-get -f install -y \
    && rm google-chrome-stable_current_amd64.deb

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor.py .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENV PYTHONUNBUFFERED=1
ENV PORT=10000
ENV RENDER=true
ENV DISPLAY=:99

EXPOSE 10000

# Use entrypoint that starts Xvfb first
ENTRYPOINT ["./entrypoint.sh"]
