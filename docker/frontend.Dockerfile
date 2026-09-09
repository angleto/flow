# Mycelium frontend — production image (Vite SPA on nginx).
# Build from the Mycelium repo root:
#   docker build -f docker/frontend.Dockerfile \
#     -t ghcr.io/angleto/mycelium/frontend:<tag> .
#
# nginx serves the static bundle and reverse-proxies /api → the backend
# Service, stripping /api exactly like the Vite dev proxy. One origin,
# no CORS.
FROM node:22-alpine AS build
WORKDIR /web
RUN corepack enable
# .npmrc carries the supply-chain min-release-age policy; without it
# pnpm v11 applies a default 24h cutoff and a same-day dependency
# upgrade (e.g. a TipTap patch) silently fails the install.
COPY web/package.json web/pnpm-lock.yaml web/.npmrc ./
RUN pnpm install --frozen-lockfile
# Named inputs rather than the directory. `COPY web/ ./` copies whatever
# a developer's tree holds beside the sources: a node_modules built for
# the HOST, laid straight over the one just installed a line above (in CI
# the checkout has none, so this fails only on the machine of whoever
# runs the build by hand), an earlier dist/, and caches whose every
# change invalidated this layer. A file the build needs and nobody named
# fails it, which is the direction the mistake should go.
COPY web/index.html web/vite.config.ts ./
COPY web/tsconfig.json web/tsconfig.app.json web/tsconfig.node.json ./
COPY web/src/ ./src/
COPY web/public/ ./public/
# Only the one the build runs; the other three scripts are gates.
COPY web/scripts/assert-build-identity.mjs ./scripts/
# Bundle identity. The backend takes these same three arguments in its
# RUNTIME stage, because it reads them from the environment when it
# answers /api/buildinfo. The SPA cannot: its identity is baked into the
# bundle and into the /version.json the running app polls to notice a
# deploy, so the values must reach the stage that runs `pnpm build`. They
# were declared only in the nginx stage below, so the bundle never saw
# them and 2.3.9 shipped `{"buildId":"dev-<clock>"}` to production while
# the workflow was passing them correctly. Declared after the dependency
# layers, so a new commit invalidates the bundle layer only.
ARG MYCELIUM_VERSION=
ARG MYCELIUM_GIT_SHA=
ARG MYCELIUM_BUILD_AT=
ENV MYCELIUM_VERSION=${MYCELIUM_VERSION} \
    MYCELIUM_GIT_SHA=${MYCELIUM_GIT_SHA} \
    MYCELIUM_BUILD_AT=${MYCELIUM_BUILD_AT}
# The assert is what stops this from recurring: an image whose bundle
# cannot name its release fails here instead of reaching production with
# a placeholder. A developer's plain `pnpm build` keeps the fallback.
RUN pnpm build && node scripts/assert-build-identity.mjs dist/version.json

# The browser extension package this deployment serves, so its settings
# page can offer a download instead of a build recipe.
#
# It is a SEPARATE stage and an OPTIONAL one. The origin is compiled into
# the package -- host_permissions and externally_connectable are static
# manifest declarations -- so a package is for exactly one deployment,
# and an image that carried somebody else's would hand every installer an
# extension talking to the wrong place. MYCELIUM_EXTENSION_ORIGIN
# therefore has no default here either: passed, this image serves a
# package; omitted, it serves none and the settings page falls back to
# the build instructions. The frontend image stays deployment-neutral
# unless somebody says otherwise.
FROM node:22-alpine AS extension
WORKDIR /extension
# pnpm 10, pinned to the major the `extension` CI job installs this same
# lockfile with. Not a preference: the extension package carries no
# .npmrc, and pnpm 11 applies a 24h minimum-release-age by default, so a
# toolchain dependency published the same day would fail this build and
# not that one. `zip` is not in the base image and build.mjs --zip shells
# out to it.
RUN npm install -g pnpm@10 && apk add --no-cache zip
COPY extension/package.json extension/pnpm-lock.yaml ./
# No runtime dependency at all: this is the dev toolchain (vite, tsc).
RUN pnpm install --frozen-lockfile
# Named inputs rather than the directory. `COPY extension/ ./` also
# brings whatever a developer's tree happens to hold: an earlier
# dist/, which the copy step below then picked up beside the archive it
# had just built and served as a second, stale download; and a
# node_modules built for the host, laid over the one just installed. A
# file this build needs and does not name fails it, which is the
# direction the mistake should go.
COPY extension/src/ ./src/
COPY extension/scripts/ ./scripts/
COPY extension/icons/ ./icons/
COPY extension/_locales/ ./_locales/
COPY extension/popup.html extension/sidepanel.html extension/vite.config.ts ./
COPY extension/tsconfig.base.json extension/tsconfig.json ./
COPY extension/tsconfig.node.json extension/tsconfig.sw.json extension/tsconfig.ui.json ./
# The barrel the panel compiles against. Deep imports are refused by the
# extension's lint config, so this directory is the whole seam.
COPY web/src/shared/ /web/src/shared/
ARG MYCELIUM_EXTENSION_ORIGIN=
# There is no .git in this context, so the version cannot be derived the
# way a developer's build derives it: the release tag is passed in, and
# without it the package would call itself 0.0.0.
ARG MYCELIUM_VERSION=
RUN mkdir -p /package && \
    if [ -n "$MYCELIUM_EXTENSION_ORIGIN" ]; then \
      MYCELIUM_EXTENSION_ORIGIN="$MYCELIUM_EXTENSION_ORIGIN" \
      MYCELIUM_VERSION="$MYCELIUM_VERSION" pnpm zip && \
      cp dist/*.zip dist/release.json /package/; \
    else \
      echo "no MYCELIUM_EXTENSION_ORIGIN: this image serves no extension package"; \
    fi

FROM nginx:1.27-alpine
LABEL org.opencontainers.image.source="https://github.com/angleto/mycelium"
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"
# Re-declared: ARG does not cross stages. Here they name the IMAGE, so
# `docker inspect` answers the same question the bundle does.
ARG MYCELIUM_VERSION=dev
ARG MYCELIUM_GIT_SHA=
ARG MYCELIUM_BUILD_AT=
LABEL org.opencontainers.image.version="${MYCELIUM_VERSION}"
LABEL org.opencontainers.image.revision="${MYCELIUM_GIT_SHA}"
LABEL org.opencontainers.image.created="${MYCELIUM_BUILD_AT}"
# Non-root: listen on 8080 (see nginx.conf), matches the Service
# targetPort and the pod's containerPort.
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
# Empty when the stage above was not given an origin, and then
# /extension/release.json is a 404 the settings page reads as "no
# package" -- the same answer a development server gives.
COPY --from=extension /package/ /usr/share/nginx/html/extension/
EXPOSE 8080
