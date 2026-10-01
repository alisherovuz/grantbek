FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONUNBUFFERED=1
# The database lives on a Railway volume mounted at /app/data, so it survives redeploys.
# On first start the bot imports config/messages.html (the channel history) by itself.
CMD ["python", "-m", "edugrants_agent", "bot"]
