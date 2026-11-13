# The example App's one image, for the delivery tests. It builds in seconds, from an image this
# repo's tests already use, and stands in for an App's real images: a web server, a health check
# and a migration that keeps its schema version in the App's named volume.
#
# Build arguments:
#   VERSION  What `/version` answers, and the schema version its migration brings the data to.
#   HEALTH   `ok`, or `fail` for an image whose health check never passes.
#   MIGRATE  `ok`, or `fail` for an image whose migration always fails, changing nothing.
#
# Like a real App's migration, it also fails, changing nothing, when the data's schema is newer
# than its own VERSION (the spec's Further Notes on migrations).

ARG NODE_IMAGE=node:24.21.0-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20

FROM ${NODE_IMAGE}
WORKDIR /app
# A named volume mounted here starts as a copy of this directory, so the App's user can write it.
RUN mkdir /data && chown node:node /data
COPY server.ts health.ts migrate.ts ./
ARG VERSION=1 HEALTH=ok MIGRATE=ok
ENV EXAMPLE_VERSION=${VERSION} EXAMPLE_HEALTH=${HEALTH} EXAMPLE_MIGRATE=${MIGRATE}
USER node
CMD ["node", "server.ts"]
