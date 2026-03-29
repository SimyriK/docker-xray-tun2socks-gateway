FROM ghcr.io/xtls/xray-core:latest AS xray

FROM ghcr.io/xjasonlyu/tun2socks:latest
RUN apk add --no-cache iptables ip6tables iproute2
COPY --from=xray /usr/local/bin/xray /usr/local/bin/xray
COPY scripts/gateway_entrypoint.sh /gateway_entrypoint.sh
RUN chmod +x /gateway_entrypoint.sh
ENTRYPOINT ["/gateway_entrypoint.sh"]
