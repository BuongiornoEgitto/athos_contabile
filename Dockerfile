FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY bot.py .
COPY transaction_core.py .
COPY transaction_writer.py .
CMD ["python", "bot.py"]
