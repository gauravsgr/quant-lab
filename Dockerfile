FROM python:3.11-slim

# Run as non-root for security
RUN useradd -m -u 1000 trader

WORKDIR /app

# Install dependencies before copying code so this layer is cached
# and only rebuilds when requirements-prod.txt changes.
COPY requirements-prod.txt .
RUN pip install --no-cache-dir -r requirements-prod.txt

# Copy application code
COPY --chown=trader:trader . .

# Create runtime directories that are gitignored but needed at runtime
RUN mkdir -p logs backtesting/results data/cache && \
    chown -R trader:trader /app

USER trader

CMD ["python", "main.py", "agent"]
