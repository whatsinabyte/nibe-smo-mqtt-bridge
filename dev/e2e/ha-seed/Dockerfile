FROM python:3.12-alpine
# websocket-client: creating a long-lived access token is only possible over
# HA's WebSocket API (see _long_lived_token in seed_ha.py).
RUN pip install --no-cache-dir websocket-client==1.8.0
WORKDIR /app
COPY seed_ha.py ./
CMD ["python3", "seed_ha.py"]
