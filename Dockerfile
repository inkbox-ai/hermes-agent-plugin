# syntax=docker/dockerfile:1.7

# Local manual-testing image. Hermes is preinstalled; the Inkbox plugin source
# is staged from the exact local build context so it can be installed after
# entering the container.
ARG HERMES_IMAGE="nousresearch/hermes-agent:main@sha256:a145ed869b9f87a8db26c3fe527aa496a8bd7d0a5c5493f4ab7a3774001803a7"
FROM ${HERMES_IMAGE}

ARG INKBOX_SDK_COMMIT="73f18a2b8c0e9dc6887c5663e6e904d54869927e"

USER root

# Login shells rebuild PATH and can omit Hermes's image-specific bin folders.
# Expose the launcher from a standard system path so interactive shells work.
RUN ln -s /opt/hermes/bin/hermes /usr/local/bin/hermes

# Install plugin dependencies into Hermes's application environment while
# building so the setup wizard is ready immediately after container startup.
RUN /usr/local/bin/uv pip install \
        --python /opt/hermes/.venv/bin/python \
        "git+https://github.com/inkbox-ai/inkbox.git@${INKBOX_SDK_COMMIT}#subdirectory=sdk/python" \
        "aiohttp>=3.9" \
        "segno>=1.5"

# Hermes's plugin installer expects a Git repository. Build one from the local
# checkout after Docker has applied .dockerignore, so the staged source exactly
# matches the files being reviewed and carries no host Git metadata.
COPY . /opt/inkbox-plugin-src
RUN git -C /opt/inkbox-plugin-src init --initial-branch=main && \
    git -C /opt/inkbox-plugin-src config user.name "Inkbox Plugin Build" && \
    git -C /opt/inkbox-plugin-src config user.email "build@localhost" && \
    git -C /opt/inkbox-plugin-src add --all && \
    git -C /opt/inkbox-plugin-src commit --message "Stage plugin source" && \
    chown -R hermes:hermes /opt/inkbox-plugin-src

ENV INKBOX_PLUGIN_SOURCE="/opt/inkbox-plugin-src"

# Keep the container available for `docker exec -it ... bash`.
CMD ["sleep", "infinity"]
