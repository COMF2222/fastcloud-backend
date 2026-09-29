FROM python:3.12-slim
WORKDIR /app
COPY server.py /app/server.py
ENV PYTHONUNBUFFERED=1 FASTCLOUD_HOST=0.0.0.0
RUN useradd --system --uid 10001 fastcloud && mkdir /data && chown fastcloud /data
USER fastcloud
VOLUME /data
EXPOSE 8080
CMD ["python", "server.py"]
