# The application image runs no bioinformatics tools -- every stage executes in
# its own container through the host Docker daemon. So this is a plain Python
# image plus the Docker CLI, which is why it builds in seconds and why the
# JAVA_HOME / PERL5LIB / conda-prefix handling the old image needed is gone.
FROM --platform=linux/amd64 python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg \
 && rm -rf /var/lib/apt/lists/*

# Docker CLI only -- no daemon. The worker talks to the host's daemon over the
# mounted socket to start per-tool containers (see docker-compose.yml).
RUN install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg \
      | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
 && chmod a+r /etc/apt/keyrings/docker.gpg \
 && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
    > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends docker-ce-cli \
 && rm -rf /var/lib/apt/lists/*

# Dependencies before source, so a code change does not invalidate this layer.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
