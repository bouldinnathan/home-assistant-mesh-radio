ARG HA_VERSION=stable
FROM ghcr.io/home-assistant/home-assistant:${HA_VERSION}

COPY custom_components/meshnet /config/custom_components/meshnet
