FROM scratch AS source
ADD --checksum=sha256:c7d5fa39816bf5f7b1ad2edbc7a53c3f7e8a124ac4f6e2c8ff8366282d4f771f https://codeload.github.com/openbao/openbao/tar.gz/a5db72cef75c24b920ade02065b18dd8eb666bac /source.tar.gz

# The upstream UI requires Node 20. It is only used in this isolated build stage.
FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS ui
COPY --from=source /source.tar.gz /tmp/source.tar.gz
RUN mkdir /source && tar -xzf /tmp/source.tar.gz -C /source --strip-components=1
ADD --checksum=sha256:98cd5718dbd8c4b2689156493b9596cecf6b64b1a98d4a087e82135a175b40eb https://registry.npmjs.org/pnpm/-/pnpm-10.34.4.tgz /tmp/pnpm.tgz
RUN mkdir /opt/pnpm && tar -xzf /tmp/pnpm.tgz -C /opt/pnpm --strip-components=1
ENV CI=true NODE_OPTIONS=--max-old-space-size=3072
WORKDIR /source/ui
RUN sha256sum pnpm-lock.yaml > /tmp/ui-lock.sha256 && \
    node /opt/pnpm/bin/pnpm.cjs fetch --frozen-lockfile --ignore-scripts && \
    sha256sum -c /tmp/ui-lock.sha256
# Ember's in-repo addons import transitive packages from the hoisted layout.
RUN --network=none node /opt/pnpm/bin/pnpm.cjs install --offline --frozen-lockfile --shamefully-hoist --ignore-scripts && \
    sha256sum -c /tmp/ui-lock.sha256
RUN --network=none node /opt/pnpm/bin/pnpm.cjs run build && \
    sha256sum -c /tmp/ui-lock.sha256
RUN --network=none mkdir -p /notices/ui && \
    find node_modules -type f \( -iname 'LICENSE*' -o -iname 'NOTICE*' -o -iname 'COPYING*' \) | \
    while IFS= read -r file; do \
      destination="/notices/ui/${file#node_modules/}"; \
      mkdir -p "$(dirname "$destination")"; cp "$file" "$destination"; \
    done

FROM golang:1.27.3-alpine@sha256:f2c872bcee949b7be9c490efd02b48a8a2fbf947cdf533f1c58525ec9d8170fd AS builder
ENV GOTOOLCHAIN=local CGO_ENABLED=0 GOMAXPROCS=2
COPY --from=source /source.tar.gz /tmp/source.tar.gz
RUN mkdir /source && tar -xzf /tmp/source.tar.gz -C /source --strip-components=1
WORKDIR /source
COPY openbao.go.mod go.mod
COPY openbao.go.sum go.sum
RUN sha256sum go.mod go.sum > /tmp/go-lock.sha256 && \
    go mod download && go mod verify && sha256sum -c /tmp/go-lock.sha256
# Retain the upstream module version for vulnerability scanners. Only the
# committed dependency locks may differ from the verified release source.
RUN apk add --no-cache 'git=2.54.0-r0' && \
    git init && \
    git fetch --depth=1 --no-tags https://github.com/openbao/openbao.git a5db72cef75c24b920ade02065b18dd8eb666bac && \
    git reset --mixed FETCH_HEAD && \
    test "$(git rev-parse HEAD)" = a5db72cef75c24b920ade02065b18dd8eb666bac && \
    git tag v2.7.1 && \
    (git diff --name-only | grep -Ev '^(go.mod|go.sum)$'; test "$?" -eq 1) && \
    test -z "$(git ls-files --others --exclude-standard)" && \
    sha256sum -c /tmp/go-lock.sha256
COPY --from=ui /source/internal/http/web_ui/ /source/internal/http/web_ui/
RUN --network=none go build -mod=readonly -tags=ui -trimpath -buildvcs=true -p 2 \
    -ldflags='-w -s -X github.com/openbao/openbao/v2/internal/version.fullVersion=2.7.1 -X github.com/openbao/openbao/v2/internal/version.GitCommit=a5db72cef75c24b920ade02065b18dd8eb666bac -X github.com/openbao/openbao/v2/internal/version.CommitDate=2026-10-01T13:32:54Z -X github.com/openbao/openbao/v2/internal/version.VersionMetadata=chipload.1' \
    -o /out/bao . && sha256sum -c /tmp/go-lock.sha256 && \
    go version -m /out/bao > /out/build-info.txt && \
    grep -F 'github.com/openbao/openbao/v2' /out/build-info.txt | grep -F 'v2.7.1+dirty'
RUN --network=none go list -mod=readonly -tags=ui -deps . > /out/packages.txt && \
    (grep -E '^golang.org/x/crypto/openpgp($|/)' /out/packages.txt; test "$?" -eq 1) && \
    sha256sum -c /tmp/go-lock.sha256
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
# Ship the corresponding upstream source, including the changed module locks.
RUN --network=none tar --exclude='./.git' --exclude='./internal/http/web_ui' -czf /out/openbao-source.tar.gz .

FROM ghcr.io/openbao/openbao:2.7.1@sha256:6d2b93856e3fcf7b18ad855a0b51eaba474dc8b79cf554379ea32034797d2acf
USER root
RUN apk add --no-cache 'zlib=1.3.2-r1'
COPY --from=builder --chmod=0555 /out/bao /usr/bin/bao
COPY --from=builder /out/packages.txt /usr/local/share/chipload/openbao-packages.txt
COPY --from=builder /out/build-info.txt /usr/local/share/chipload/openbao-build-info.txt
COPY --from=builder /out/openbao-source.tar.gz /usr/local/share/chipload/openbao-source.tar.gz
COPY --from=builder /notices/ /usr/local/share/licenses/openbao-dependencies/
COPY --from=ui /notices/ /usr/local/share/licenses/openbao-dependencies/
COPY LICENSE /usr/local/share/licenses/chipload/LICENSE
ARG SECRETS_SHA256
LABEL org.chipload.secrets-role="openbao" org.chipload.secrets-sha256=$SECRETS_SHA256
LABEL org.chipload.openbao-version="2.7.1+chipload.1"
USER openbao
