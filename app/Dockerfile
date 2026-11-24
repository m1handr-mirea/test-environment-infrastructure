FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
USER 65534:65534
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "main:app"]
