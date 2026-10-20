FROM quay.io/keycloak/keycloak:26.8.1@sha256:79c623a22abacf3fe6c00a5fb30f5b7843d717b234033b80995891c06116d244
ENV KC_DB=postgres KC_HEALTH_ENABLED=true
RUN /opt/keycloak/bin/kc.sh build
LABEL org.chipload.identity-version="26.8.1"
ARG TLS_SHA256
LABEL org.chipload.tls-role="keycloak" org.chipload.tls-sha256=$TLS_SHA256
COPY LICENSE /opt/chipload/LICENSE
COPY --chmod=755 keycloak-entrypoint.sh /opt/chipload/start-keycloak
ENTRYPOINT ["/bin/bash", "/opt/chipload/start-keycloak"]
