import os
import json
import requests
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
SHEETS_URL = os.environ.get("SHEETS_URL")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ALLOWED_GROUP_ID = None

SYSTEM_PROMPT = """Sei Athos, un agente AI contabile specializzato per agenzie di viaggi ed escursioni.
Sei preciso, professionale e cordiale. Rispondi SEMPRE in italiano e in modo conciso.
"""

def get_sheet_data():
    try:
        response = requests.get(SHEETS_URL, timeout=10)
        rows = response.json()
        if len(rows) <= 1:
            return "Nessuna transazione ancora registrata."
        headers = rows[0]
        transactions = []
        for row in rows[1:]:
            if any(row):
                t = dict(zip(headers, row))
                transactions.append(t)
        entrate = sum(float(t.get("importo", 0)) for t in transactions if t.get("tipo") == "entrata")
        uscite = sum(float(t.get("importo", 0)) for t in transactions if t.get("tipo") == "uscita")
        return f"Entrate: {entrate} - Uscite: {uscite}"
    except Exception as e:
        return f"Errore: {e}"

def save_transaction(data: dict):
    try:
        data["data"] = datetime.now().strftime("%Y-%m-%d")
        response = requests.post(SHEETS_URL, json=data, timeout=10)
        return response.text == "OK"
    except:
        return False

async def ask_claude(user_message: str):
    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01"
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 500,
                "messages": [{"role": "user", "content": user_message}]
            },
            timeout=30
        )
        data = response.json()
        return data["content"][0]["text"]
    except Exception as e:
        return f"Errore AI: {e}"

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    response = await ask_claude(text)
    await update.message.reply_text(response)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot attivo 🚀")

def main():
    print("🚀 VERSIONE NUOVA ATHOS SENZA JOB_QUEUE")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()
