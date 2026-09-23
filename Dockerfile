# Bundles the SignalIQ dashboard + a working Chromium into one image, so
# deploying to a server is "docker build" + "docker run" - no manual
# python/playwright/mitmproxy install steps on the server itself.
#
# Base image already has Python + Playwright + Chromium preinstalled and
# working, which is the fiddly part to get right on a bare server.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

COPY requirements-dashboard.txt .
RUN pip install --no-cache-dir -r requirements-dashboard.txt

COPY . .
RUN mkdir -p /app/sessions

EXPOSE 8787
ENV HOST=0.0.0.0
ENV PORT=8787

CMD ["python", "-m", "dashboard.server"]