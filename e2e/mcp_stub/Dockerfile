FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn>=0.30" "pyjwt[crypto]>=2.8" "httpx>=0.27"
COPY server.py .
EXPOSE 9100
CMD ["python", "server.py"]
