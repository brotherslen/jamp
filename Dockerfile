# jamp in a container, for running on a NAS or any Docker host.
#
#   docker build -t jamp .
#   docker run --rm -it -v jamp-config:/config \
#       -v /path/to/live-music:/music -v /path/to/jamp-reports:/reports \
#       jamp init --root /music --out-dir /reports
#
# See docs/docker.md.
FROM python:3.11-slim

# ffmpeg for `jamp convert` and `scan --verify-audio`. Debian's build
# includes the Shorten decoder.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY pyproject.toml README.md LICENSE THIRD-PARTY-NOTICES.md ./
COPY jamp ./jamp
RUN pip install --no-cache-dir . \
    && mkdir -p /usr/share/doc/jamp \
    && cp LICENSE THIRD-PARTY-NOTICES.md /usr/share/doc/jamp/ \
    && rm -rf /src

# Your settings, overrides and remembered paths live here; mount a volume on it
# so they survive the container.
ENV JAMP_HOME=/config
VOLUME ["/config"]
WORKDIR /

ENTRYPOINT ["jamp"]
CMD ["doctor"]
