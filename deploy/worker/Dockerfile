FROM python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ARG SDK_SHA256
ARG DEPENDENCIES_SHA256
LABEL org.chipload.worker-protocol="1" \
      org.chipload.sdk-sha256=$SDK_SHA256 \
      org.chipload.dependencies-sha256=$DEPENDENCIES_SHA256 \
      org.chipload.python-version="3.12.15"

COPY requirements.txt /opt/requirements.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check \
    --require-hashes --only-binary=:all: --no-deps -r /opt/requirements.txt
COPY chipload/ /usr/local/lib/python3.12/site-packages/chipload/
COPY LICENSE /usr/local/share/licenses/chipload/LICENSE

USER 65532:65532
WORKDIR /tmp
ENTRYPOINT ["python", "-I", "-B", "-m", "chipload.worker"]
