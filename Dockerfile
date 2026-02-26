# ---- Stage 1: Build ----

# ==========================================================================
# ──────────────────── ВНУТРЕННИЕ РЕПОЗИТОРИИ (1/3) ────────────────────────
# Если ubi8/python-311 проксируется через внутренний registry (Nexus, Harbor,
# Artifactory), замените имя образа ниже, например:
#   FROM nexus.company.com/docker-proxy/ubi8/python-311:latest AS builder
# ==========================================================================
FROM ubi8/python-311:latest AS builder

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

# UBI8: непривилегированный пользователь UID 1001, GID 0 (OpenShift-совместимо)
RUN useradd -u 1001 -r -g 0 -s /sbin/nologin alla

COPY --from=builder /usr/local/lib/python3.11/site-packages/ \
                    /usr/local/lib/python3.11/site-packages/
COPY --from=builder /usr/local/bin/alla* /usr/local/bin/

WORKDIR /app

COPY --chown=1001:0 knowledge_base/ knowledge_base/

USER 1001

EXPOSE 8090

CMD ["alla-server"]
