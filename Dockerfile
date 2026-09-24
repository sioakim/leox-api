FROM python:3.12-alpine
WORKDIR /app
COPY server.py openapi.json ./
USER 65534:65534
EXPOSE 8000
CMD ["python", "server.py"]
