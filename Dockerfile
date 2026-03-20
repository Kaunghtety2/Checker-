FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt .

RUN PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 pip install --no-cache-dir -r requirements.txt

COPY site_checker_botv7.py .

CMD ["python", "site_checker_botv7.py"]site_checker_botv7
