FROM python:3.12-slim
COPY server.py protocol.py registries.py /opt/gateway/
RUN chmod 0555 /opt/gateway && chmod 0444 /opt/gateway/*.py
USER 65534:65534
ENTRYPOINT ["python3", "-I", "/opt/gateway/server.py"]
