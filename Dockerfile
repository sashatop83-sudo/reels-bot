FROM debian:bookworm-slim AS tgapi-build

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates git cmake g++ make zlib1g-dev libssl-dev gperf \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone --recursive --depth 1 --branch v9.5 https://github.com/tdlib/telegram-bot-api.git .

RUN mkdir build && cd build \
    && cmake -DCMAKE_BUILD_TYPE=Release .. \
    && cmake --build . --target telegram-bot-api -j2 \
    && strip telegram-bot-api

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fontconfig ca-certificates libssl3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=tgapi-build /src/build/telegram-bot-api /usr/local/bin/telegram-bot-api

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x start.sh

CMD ["./start.sh"]
