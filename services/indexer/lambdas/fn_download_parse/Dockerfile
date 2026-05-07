# Lambda container image for fn-download-parse
# Includes system deps for PDF/image parsing
# Build: docker build -t fn-download-parse .

FROM public.ecr.aws/lambda/python:3.12

# System deps for parsers.
# Amazon Linux 2023's Lambda base image does not ship a tesseract package in
# the default repo; cloud image OCR currently returns a placeholder upstream.
RUN dnf install -y \
    poppler-utils \
    && dnf clean all

# Python deps
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Copy app code (parsers need the full app package)
COPY handler.py ${LAMBDA_TASK_ROOT}/handler.py

# Copy the app package for parser imports
# In CI, this is copied from services/indexer/app/
COPY app/ ${LAMBDA_TASK_ROOT}/app/

CMD ["handler.handler"]
