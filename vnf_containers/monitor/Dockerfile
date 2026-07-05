FROM ubuntu:22.04

RUN apt-get update -qq && \
    apt-get install -y -qq \
    python3 \
    python3-pip \
    tcpdump \
    iproute2 && \
    pip3 install scapy && \
    rm -rf /var/lib/apt/lists/*

COPY monitor.py /app/monitor.py

CMD ["python3", "-u", "/app/monitor.py"]
