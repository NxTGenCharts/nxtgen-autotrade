FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8090
EXPOSE 8090

# IMPORTANT: -w 1 is intentional. In this Phase-1 build the BotManager loop
# runs as an in-process thread; running >1 gunicorn worker would tick every
# bot multiple times in parallel. Phase 2 extracts the bot engine into its
# own worker process (see README "Scaling beyond Phase 1") so the API tier
# can then run with multiple workers safely.
CMD ["gunicorn", "-w", "1", "-k", "gthread", "--threads", "8", "-b", "0.0.0.0:8090", "app:app"]
