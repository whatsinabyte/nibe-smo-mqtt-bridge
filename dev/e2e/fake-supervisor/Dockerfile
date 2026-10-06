FROM python:3.12-alpine
RUN pip install --no-cache-dir aiohttp==3.10.10
WORKDIR /app
COPY fake_supervisor.py ./
CMD ["python3", "fake_supervisor.py"]
