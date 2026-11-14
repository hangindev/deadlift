# The example App's one image, for the delivery tests. It builds in seconds, on Python's standard
# library alone, and stands in for an App's real images: a web server, a health check and a
# migration that keeps its schema version in the App's named volume.
#
# Build arguments:
#   VERSION  What `/version` answers, and the schema version its migration brings the data to.
#   HEALTH   `ok`, or `fail` for an image whose health check never passes.
#   MIGRATE  `ok`, or `fail` for an image whose migration always fails, changing nothing.
#
# Like a real App's migration, it also fails, changing nothing, when the data's schema is newer
# than its own VERSION (the spec's Further Notes on migrations).

ARG PYTHON_IMAGE=python:3.12.12-slim@sha256:f3fa41d74a768c2fce8016b98c191ae8c1bacd8f1152870a3f9f87d350920b7c

FROM ${PYTHON_IMAGE}
WORKDIR /app
# The App runs as a user of its own. A named volume mounted at /data starts as a copy of this
# directory, so that user can write it.
RUN useradd --uid 1000 --no-create-home app && mkdir /data && chown app:app /data
COPY server.py health.py migrate.py ./
ARG VERSION=1 HEALTH=ok MIGRATE=ok
ENV EXAMPLE_VERSION=${VERSION} EXAMPLE_HEALTH=${HEALTH} EXAMPLE_MIGRATE=${MIGRATE} \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER app
CMD ["python3", "server.py"]
