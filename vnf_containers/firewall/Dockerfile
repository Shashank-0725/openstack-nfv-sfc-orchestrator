FROM ubuntu:22.04

RUN apt-get update -qq && \
    apt-get install -y -qq \
    iptables \
    iproute2 \
    procps \
    python3 && \
    update-alternatives --set iptables /usr/sbin/iptables-legacy && \
    update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy && \
    rm -rf /var/lib/apt/lists/*

COPY firewall.py /app/firewall.py

CMD ["python3", "/app/firewall.py"]
