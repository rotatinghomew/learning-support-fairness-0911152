FROM python:3.11.2-slim-bullseye@sha256:9ad4ffc502779e5508f7ac1eccab4a22786b80bd53d721d735f6de0840b245a1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "scripts/demo.py"]
