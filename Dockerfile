FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY stocked ./stocked
COPY demo_images ./demo_images
ENV HOST=0.0.0.0 PORT=8095 STOCKED_DATA=/data
VOLUME ["/data"]
EXPOSE 8095
CMD ["python", "-m", "stocked.app"]
