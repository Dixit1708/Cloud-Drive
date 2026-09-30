FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8000 DATA_DIR=/data
VOLUME /data
EXPOSE 8000
CMD gunicorn app:app --workers 2 --timeout 120 --bind 0.0.0.0:$PORT
