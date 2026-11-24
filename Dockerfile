FROM python:3.12-slim

WORKDIR /app
# Expense claims: pdftotext reads a PDF's text layer, tesseract reads scans
RUN apt-get update \
 && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

COPY heimkunnskap/ heimkunnskap/
COPY sql/ sql/

# The worker: apply migrations, then sync once a night at SYNC_AT. `exec`
# makes Python the main process, so `docker stop` reaches it directly.
CMD ["sh", "-c", "exec python -m heimkunnskap.sync"]
