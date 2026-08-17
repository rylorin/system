#!/bin/bash
set -x
PORT=8080
CONTAINER=amnezia-wg-easy
docker stop "${CONTAINER}"
docker rm -f "${CONTAINER}"
docker run -d \
   --name "${CONTAINER}" \
   --cap-add NET_ADMIN \
   --cap-add SYS_MODULE \
   -p 8443:8443/udp \
   --network system_dmz_network \
   -v amnezia_data:/etc/amnezia/amneziawg \
   -e "PORT=${PORT}" \
   -e "WG_HOST=amnezia.lorin.top" \
   -e "WG_INTERFACE=wg1" \
   -e "WG_PATH=/etc/amnezia/amneziawg" \
   --device "/dev/net/tun:/dev/net/tun" \
   --sysctl net.ipv4.ip_forward=1 \
   --sysctl net.ipv6.conf.all.forwarding=1 \
   --sysctl net.ipv4.conf.all.src_valid_mark=1 \
  --restart unless-stopped \
   rylorin/amnezia-wg-easy:latest \
#   -p "10.0.4.0:${PORT}:${PORT}/tcp" \
#   -e "DEBUG=Server,Server:*,WireGuard" \
