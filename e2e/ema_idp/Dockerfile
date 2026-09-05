FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn>=0.30" "pyjwt[crypto]>=2.8" "python-multipart>=0.0.9"
COPY server.py .
EXPOSE 9200
CMD ["python", "server.py"]
