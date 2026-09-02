FROM python:3.12-alpine
RUN apk add --no-cache openssl
WORKDIR /app
COPY mock_nibe_api.py ./
EXPOSE 8443
CMD ["python3", "mock_nibe_api.py"]
