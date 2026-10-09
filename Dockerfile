FROM python:3.12-slim
WORKDIR /app
COPY server.py media.py relay.py usage.py personal.py chat.py operations.py concurrency.py upstream.py backup.py preflight.py /app/
ENV PYTHONUNBUFFERED=1 FASTCLOUD_HOST=0.0.0.0
RUN useradd --system --uid 10001 fastcloud && mkdir -p /data /media /backups && chown fastcloud /data /media /backups
USER fastcloud
VOLUME /data
VOLUME /media
VOLUME /backups
EXPOSE 8080
CMD ["python", "server.py"]
