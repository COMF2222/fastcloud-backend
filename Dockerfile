FROM rust:1.97.1-slim AS lyrics
RUN apt-get update && apt-get install -y --no-install-recommends cmake pkg-config && rm -rf /var/lib/apt/lists/*
WORKDIR /build
ENV CARGO_BUILD_JOBS=2
COPY lyrics-worker /build
RUN cargo build --release --locked

FROM python:3.12-slim
WORKDIR /app
COPY server.py media.py recordings.py relay.py usage.py personal.py chat.py operations.py concurrency.py upstream.py backup.py preflight.py mobile.py pairing.py mobile_updates.py /app/
COPY --from=lyrics /build/target/release/fastcloud-lyrics-worker /app/fastcloud-lyrics-worker
ENV PYTHONUNBUFFERED=1 FASTCLOUD_HOST=0.0.0.0
RUN useradd --system --uid 10001 fastcloud && mkdir -p /data /media /backups && chown fastcloud /data /media /backups
USER fastcloud
VOLUME /data
VOLUME /media
VOLUME /backups
EXPOSE 8080
CMD ["python", "server.py"]
