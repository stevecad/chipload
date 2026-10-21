ARG ISSUER_BASE
FROM golang:1.26.10-alpine@sha256:9ce231b28998b32e116927c61aa5c406183061d4f3f2d61b292c88e7f6bdf7ca AS builder
ENV GOTOOLCHAIN=local CGO_ENABLED=0
WORKDIR /build
COPY issuer.go.mod go.mod
COPY issuer.go.sum go.sum
RUN sha256sum go.mod go.sum > /build/lock.sha256 && \
    go mod download && go mod verify && sha256sum -c /build/lock.sha256
# Compile the released lego entrypoint with the reviewed dependency lock.
# No toolchain or dependency resolution is allowed during compilation.
RUN --network=none go build -mod=readonly -trimpath -ldflags='-w -s -X main.version=v5.5.2+chipload.1' \
    -o /out/lego github.com/go-acme/lego/v5 && \
    sha256sum -c /build/lock.sha256
RUN --network=none go list -mod=readonly -deps github.com/go-acme/lego/v5 \
    > /out/packages.txt && \
    (grep -E '^golang.org/x/crypto/openpgp($|/)' /out/packages.txt; test "$?" -eq 1) && \
    sha256sum -c /build/lock.sha256
RUN --network=none mkdir -p /notices/go && cp /usr/local/go/LICENSE /notices/go/LICENSE && \
    go list -m -f '{{if .Dir}}{{.Path}} {{.Dir}}{{end}}' all | \
    while read -r module directory; do \
      if [ -n "$directory" ]; then \
        mkdir -p "/notices/$module"; \
        find "$directory" -maxdepth 1 -type f \
          \( -iname 'LICENSE*' -o -iname 'NOTICE*' -o -iname 'COPYING*' \) \
          -exec cp '{}' "/notices/$module/" \;; \
      fi; \
    done

FROM ${ISSUER_BASE}
RUN apk add --no-cache 'libexpat=2.9.0-r0' 'pcre2=10.49-r0' \
    'nghttp2-libs=1.70.0-r0' 'zlib=1.3.2-r1'
COPY --from=builder /out/lego /lego
COPY --from=builder /out/packages.txt /usr/local/share/chipload/issuer-packages.txt
COPY --from=builder /notices/ /usr/local/share/licenses/lego-dependencies/
COPY LICENSE /usr/local/share/licenses/chipload/LICENSE
ARG ACME_SHA256
LABEL org.chipload.acme-role="issuer" org.chipload.acme-sha256=$ACME_SHA256
LABEL org.chipload.issuer-version="v5.5.2+chipload.1"
USER 65532:65532
