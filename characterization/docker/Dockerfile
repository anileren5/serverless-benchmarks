# Measurement image for local SeBS characterization.
# Bundles the SeBS pypapi fork (PAPI 5.5.1) and the Python packages the suite imports.
FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CFLAGS="-fPIC -fcommon"

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        linux-libc-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY pypapi /opt/pypapi

WORKDIR /opt/pypapi/papi/src
RUN ./configure --with-perf-events && make -j"$(nproc)" libpapi.a \
    && test -f libpapi.a

WORKDIR /opt/pypapi
RUN pip install --no-cache-dir "cffi>=1.0" && python setup.py install

RUN pip install --no-cache-dir \
        "numpy==1.24.0" \
        "pillow==10.3.0" \
        "jinja2>=2.10.3" \
        "python-igraph" \
        "minio" \
        "boto3"

RUN pip install --no-cache-dir \
        "https://download.pytorch.org/whl/cpu/torch-2.0.0%2Bcpu-cp311-cp311-linux_x86_64.whl" \
    && pip install --no-cache-dir --no-deps "torchvision==0.15.1"

RUN pip install --no-cache-dir "squiggle==0.3.1" "requests" \
    && pip install --no-cache-dir "numpy==1.24.0"

RUN apt-get update && apt-get install -y --no-install-recommends linux-perf \
    && rm -rf /var/lib/apt/lists/*

COPY measure.py /opt/measure.py

WORKDIR /work
ENTRYPOINT ["python", "/opt/measure.py"]
