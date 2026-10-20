FROM docker:29.9.1-cli@sha256:6fae2214c3121a28d5aa639d09032f61310b7c9c75ffbad1025f261a1b621800 AS docker-cli

FROM python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS base
ENV PYTHONUNBUFFERED=1
WORKDIR /tmp

FROM base AS api
COPY api-requirements.txt /opt/requirements.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check \
    --require-hashes --only-binary=:all: --no-deps -r /opt/requirements.txt
COPY chipload/ /usr/local/lib/python3.12/site-packages/chipload/
COPY LICENSE /usr/local/share/licenses/chipload/LICENSE
ARG SDK_SHA256
ARG DEPENDENCIES_SHA256
LABEL org.chipload.runtime-role="api" \
      org.chipload.runtime-protocol="1" \
      org.chipload.sdk-sha256=$SDK_SHA256 \
      org.chipload.dependencies-sha256=$DEPENDENCIES_SHA256
USER 65532:65532
ENTRYPOINT ["python", "-I", "-B", "-m", "chipload.server"]
CMD ["--config", "/etc/chipload/settings.json", "--bind", "0.0.0.0", "--port", "8088"]

FROM base AS broker
COPY broker-requirements.txt /opt/requirements.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check \
    --require-hashes --only-binary=:all: --no-deps -r /opt/requirements.txt
COPY chipload/ /usr/local/lib/python3.12/site-packages/chipload/
COPY LICENSE /usr/local/share/licenses/chipload/LICENSE
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY licenses/ /usr/local/share/licenses/docker-cli/
ARG SDK_SHA256
ARG DEPENDENCIES_SHA256
LABEL org.chipload.runtime-role="broker" \
      org.chipload.runtime-protocol="1" \
      org.chipload.sdk-sha256=$SDK_SHA256 \
      org.chipload.dependencies-sha256=$DEPENDENCIES_SHA256
USER 65532:65532
ENTRYPOINT ["python", "-I", "-B", "-m", "chipload.broker"]
