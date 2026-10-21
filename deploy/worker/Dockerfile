ARG WORKER_BASE=minimal
FROM python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS minimal

FROM minimal AS graphics
# Pin both signed package indexes, including transitive security updates.
RUN rm /etc/apt/sources.list.d/debian.sources && \
    echo 'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20261021T000000Z trixie main' > /etc/apt/sources.list && \
    echo 'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/20261021T000000Z trixie-security main' >> /etc/apt/sources.list && \
    apt-get update && apt-get install --no-install-recommends -y \
        libgl1 libxrender1 libxext6 'libexpat1=2.9.0-1~deb13u1' && \
    rm -rf /var/lib/apt/lists/*

FROM minimal AS shop-wheels
COPY shop-requirements.txt /opt/shop-requirements.txt
RUN python -I -m pip --isolated download --dest /wheels \
    --index-url https://pypi.org/simple --keyring-provider disabled \
    --no-cache-dir --disable-pip-version-check --require-hashes --only-binary=:all: \
    --no-deps --requirement /opt/shop-requirements.txt

FROM ${WORKER_BASE} AS worker

COPY requirements.txt /opt/requirements.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check \
    --require-hashes --only-binary=:all: --no-deps -r /opt/requirements.txt

# Only source/program workers use this interpreter. The SDK service uses the
# core interpreter, whose search path excludes these operator-selected packages.
RUN /usr/local/bin/python -I -m venv --system-site-packages /opt/chipload-shop
COPY shop-requirements.txt /opt/shop-requirements.txt
RUN --network=none --mount=type=bind,from=shop-wheels,source=/wheels,target=/wheels \
    /opt/chipload-shop/bin/python -I -m pip --isolated install \
    --no-index --find-links /wheels --keyring-provider disabled \
    --no-cache-dir --disable-pip-version-check --require-hashes --only-binary=:all: \
    --constraint /opt/requirements.txt --requirement /opt/shop-requirements.txt
# Do not run the shop interpreter again during the build: installed packages can
# contain startup hooks. They first execute under the source container's limits.
COPY chipload/ /usr/local/lib/python3.12/site-packages/chipload/
COPY LICENSE /usr/local/share/licenses/chipload/LICENSE

ARG SDK_SHA256
ARG DEPENDENCIES_SHA256
ARG CORE_DEPENDENCIES_SHA256
LABEL org.chipload.worker-protocol="1" \
      org.chipload.sdk-sha256=$SDK_SHA256 \
      org.chipload.dependencies-sha256=$DEPENDENCIES_SHA256 \
      org.chipload.core-dependencies-sha256=$CORE_DEPENDENCIES_SHA256 \
      org.chipload.python-version="3.12.15"

USER 65532:65532
WORKDIR /tmp
ENTRYPOINT ["/opt/chipload-shop/bin/python", "-I", "-B", "-m", "chipload.worker"]
