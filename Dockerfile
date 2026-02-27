# ---- Stage 1: Build ----

# ==========================================================================
# ──────────────────── ВНУТРЕННИЕ РЕПОЗИТОРИИ (1/3) ────────────────────────
# Если ubi8/python-311 проксируется через внутренний registry (Nexus, Harbor,
# Artifactory), замените имя образа ниже, например:
#   FROM nexus.company.com/docker-proxy/ubi8/python-311:latest AS builder
# ==========================================================================
FROM ubi8/python-311:latest AS builder

# UBI8 S2I sets USER 1001 by default; switch to root for build operations.
USER root

# ==========================================================================
# ──────────────────── ВНУТРЕННИЕ РЕПОЗИТОРИИ (2/3) ────────────────────────
# Если yum/dnf-зеркало внутреннее, добавьте .repo-файл перед install:
#   COPY internal.repo /etc/yum.repos.d/internal.repo
# или используйте: --disablerepo='*' --enablerepo=internal-base
#
# Если scikit-learn/scipy доступны как готовые manylinux wheels во внутреннем
# PyPI (см. блок pip ниже), этот RUN-блок можно удалить целиком.
# ==========================================================================
RUN microdnf install -y \
        --setopt=tsflags=nodocs \
        gcc gcc-c++ python3-devel \
    && microdnf clean all

WORKDIR /build

COPY pyproject.toml .
COPY src/ src/

# ==========================================================================
# ──────────────────── ВНУТРЕННИЕ РЕПОЗИТОРИИ (3/3) ────────────────────────
# Если pip должен использовать внутренний PyPI (Nexus / Devpi / Artifactory),
# замените команду ниже на:
#   RUN pip install \
#       --index-url https://nexus.company.com/repository/pypi-proxy/simple/ \
#       --trusted-host nexus.company.com \
#       --no-cache-dir .
# ==========================================================================
RUN pip install --no-cache-dir .

# ---- Stage 2: Runtime ----

# ==========================================================================
# ──────────────────── ВНУТРЕННИЕ РЕПОЗИТОРИИ (1/3) ────────────────────────
# Та же замена образа, что и в Stage 1:
#   FROM nexus.company.com/docker-proxy/ubi8/python-311:latest
# ==========================================================================
FROM ubi8/python-311:latest

# UBI8 S2I already ships user 'default' (UID 1001, GID 0) — no useradd needed.

# UBI8 S2I layout: site-packages and scripts live under /opt/app-root.
# Verify with:
#   docker run --rm ubi8/python-311:latest python3 -c "import site; print(site.getsitepackages())"
#   docker run --rm ubi8/python-311:latest which pip3
# If your image variant uses a different prefix, update both paths accordingly.
COPY --from=builder /opt/app-root/lib/python3.11/site-packages/ \
                    /opt/app-root/lib/python3.11/site-packages/
COPY --from=builder /opt/app-root/bin/alla* /opt/app-root/bin/

WORKDIR /app

COPY --chown=1001:0 knowledge_base/ knowledge_base/
COPY --chown=1001:0 docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

USER 1001

EXPOSE 8090

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["alla-server"]
