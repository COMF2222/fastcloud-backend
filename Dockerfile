FROM python:3.12-slim
WORKDIR /app
COPY server.py media.py /app/
ENV PYTHONUNBUFFERED=1 FASTCLOUD_HOST=0.0.0.0
RUN useradd --system --uid 10001 fastcloud && mkdir -p /data /media && chown fastcloud /data /media
USER fastcloud
VOLUME /data
VOLUME /media
EXPOSE 8080
CMD ["python", "server.py"]
