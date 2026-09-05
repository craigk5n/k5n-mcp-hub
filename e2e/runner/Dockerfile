FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "httpx>=0.27" "pyjwt>=2.8"
COPY run_e2e.py .
CMD ["python", "run_e2e.py"]
