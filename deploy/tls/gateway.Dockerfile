FROM golang:1.26.10-alpine@sha256:9ce231b28998b32e116927c61aa5c406183061d4f3f2d61b292c88e7f6bdf7ca AS builder
ENV GOTOOLCHAIN=local CGO_ENABLED=0
WORKDIR /build
COPY gateway.go.mod go.mod
COPY gateway.go.sum go.sum
RUN sha256sum go.mod go.sum > /build/lock.sha256 && \
    go mod download && go mod verify && sha256sum -c /build/lock.sha256
# Compile the released Caddy entrypoint with the reviewed dependency lock.
# No toolchain or dependency resolution is allowed during compilation.
RUN --network=none go build -mod=readonly -trimpath -ldflags='-w -s' \
    -o /out/caddy github.com/caddyserver/caddy/v2/cmd/caddy && \
    sha256sum -c /build/lock.sha256
# x/crypto is used, but its deprecated OpenPGP packages must stay unlinked.
RUN --network=none go list -mod=readonly -deps github.com/caddyserver/caddy/v2/cmd/caddy \
    > /out/packages.txt && \
    (grep -E '^golang.org/x/crypto/openpgp($|/)' /out/packages.txt; test "$?" -eq 1)
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

FROM caddy:2.11.7@sha256:685ae3d7c34d9b5f8caeddc5d14051db7bbd88ff1f5a2fa0c16bd8ca328fb7af
# The gateway uses unprivileged ports; its executable needs no file capability.
RUN apk add --no-cache 'zlib=1.3.2-r1' && setcap -r /usr/bin/caddy
COPY --from=builder /out/caddy /usr/bin/caddy
COPY --from=builder /out/packages.txt /usr/local/share/chipload/gateway-packages.txt
COPY --from=builder /notices/ /usr/local/share/licenses/caddy-dependencies/
LABEL org.chipload.gateway-version="2.11.7"
ARG TLS_SHA256
LABEL org.chipload.tls-role="gateway" org.chipload.tls-sha256=$TLS_SHA256
COPY LICENSE /usr/local/share/licenses/chipload/LICENSE
USER 65532:65532
