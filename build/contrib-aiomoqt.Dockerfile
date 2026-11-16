# aiomoqt lanes: pub_media and sub_media. AIOMOQT is any pip requirement: a release such as
# aiomoqt==0.15.0rc1, or git+https://github.com/<owner>/aiomoqt@<branch>.
FROM python:3.12-bookworm
ARG AIOMOQT=aiomoqt==0.15.0rc1
ARG AIOPQUIC=aiopquic==0.8.0rc1
RUN pip install --no-cache-dir --pre "${AIOPQUIC}" "${AIOMOQT}" \
    && python -m aiomoqt.versions > /etc/aiomoqt-versions.txt 2>&1; cat /etc/aiomoqt-versions.txt
LABEL org.moqbench.aiomoqt="${AIOMOQT}" org.moqbench.aiopquic="${AIOPQUIC}"
