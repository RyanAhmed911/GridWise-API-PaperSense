FROM python:3.12-slim

WORKDIR /app

# libgomp1 is required by PuLP's bundled CBC solver binary on Debian slim images.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# GROQ_API_KEY (and optional GROQ_MODEL) must be supplied at runtime, e.g.:
#   docker run -p 8000:8000 -e GROQ_API_KEY=... gridwise-api
# The image never bakes in .env or any secret value.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
