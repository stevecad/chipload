FROM caddy:2.11.7@sha256:685ae3d7c34d9b5f8caeddc5d14051db7bbd88ff1f5a2fa0c16bd8ca328fb7af
# The gateway uses unprivileged ports; its executable needs no file capability.
RUN setcap -r /usr/bin/caddy
LABEL org.chipload.gateway-version="2.11.7"
ARG TLS_SHA256
LABEL org.chipload.tls-role="gateway" org.chipload.tls-sha256=$TLS_SHA256
COPY LICENSE /usr/local/share/licenses/chipload/LICENSE
USER 65532:65532
