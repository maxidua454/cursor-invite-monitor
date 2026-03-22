FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor.py .

ENV PYTHONUNBUFFERED=1
ENV PORT=10000

EXPOSE 10000

CMD ["python", "monitor.py"]
