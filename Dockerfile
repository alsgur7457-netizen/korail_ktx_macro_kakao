FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Copy src directory while preserving structure
COPY src/ /app/src/

# Set PYTHONPATH so imports work correctly
ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    KAKAO_TOKEN_FILE=/app/data/kakao_token.json \
    KORAIL_DEVICE_FILE=/app/data/korail_device.json

VOLUME ["/app/data"]

ENTRYPOINT ["python", "/app/src/macro.py"]
