FROM python@sha256:7e9b0661d175b176a592d3969ccaf702c11b6769a485fa8b0d3769dd07bf1720
ENV OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
COPY wheels /tmp/wheels
COPY requirements.lock /tmp/requirements.lock
RUN python3 -m pip install --no-index --find-links=/tmp/wheels --require-hashes -r /tmp/requirements.lock && rm -rf /tmp/wheels
