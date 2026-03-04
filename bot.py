import os
import json
import requests
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

# ============================================================
# CONFIGURAZIONE — modifica questi valori
# ============================================================
TELEGRAM_TOKEN = "7845527801:AAHJUK5k0rRObYhTB4pDdEjOwlEMp20ENYw
"
SHEETS_URL = "https://script.google.com/macros/s/AKfycbzz7PDcz4vxJAngO2mJBOgsmK6Kni28b5jcuxk9KKolyqRdj_h6nngMASu7GdspTTuLvw/exec"
ANTHROPIC_API_KEY = "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"
ALLOWED_GROUP_ID = None  # Verrà impostato automaticamente al primo messaggio del gruppo
# ============================================================

SYSTEM_PROMPT = """Sei Athos, un agente AI contabile specializzato per agenzie di viaggi ed escursioni.
Sei preciso, professionale e cordiale. Rispondi SEMPRE in italiano e in modo conciso.

Puoi fare due cose:

1. REGISTRARE UNA TRANSAZIONE
Quando l'utente vuole aggiungere entrata/uscita, rispondi SOLO con questo JSON (nient'altro prima o dopo):
TRANSACTION:{"tipo":"entrata","importo":500,"descrizione":"Tour Roma","categoria":"Escursioni","tour":"Tour Roma","fornitore_cliente":"Mario Rossi","note":""}

Categorie entrata: Prenotazioni Tour, Escursioni, Pacchetti Viaggio, Servizi Extra, Altro
Categorie uscita: Guide, Trasporti, Alloggi, Marketing, Commissioni, Spese Operative, Altro

2. RISPONDERE A DOMANDE
Analizza i dati del foglio e rispondi in modo chiaro con numeri precisi.
Per i report usa emoji per rendere il messaggio leggibile su Telegram.

Se il messaggio non è chiaro, chiedi una breve conferma.
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
        
        # Calcola statistiche
        entrate = sum(float(t.get("importo", 0)) for t in transactions if t.get("tipo") == "entrata")
        uscite = sum(float(t.get("importo", 0)) for t in transactions if t.get("tipo") == "uscita")
        
        summary = f"TOTALE ENTRATE: €{entrate:.2f}\nTOTALE USCITE: €{uscite:.2f}\nPROFITTO NETTO: €{entrate-uscite:.2f}\n\n"
        summary += "ULTIME 10 TRANSAZIONI:\n"
        for t in transactions[-10:]:
            summary += f"- {t.get('data','')} | {t.get('tipo','').upper()} | €{t.get('importo','')} | {t.get('descrizione','')} | {t.get('fornitore_cliente','')}\n"
        
        return summary
    except Exception as e:
        return f"Errore lettura dati: {e}"

def save_transaction(data: dict):
    try:
        data["data"] = datetime.now().strftime("%Y-%m-%d")
        response = requests.post(SHEETS_URL, json=data, timeout=10)
        return response.text == "OK"
    except:
        return False

async def ask_claude(user_message: str, sheet_context: str) -> str:
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
                "max_tokens": 1000,
                "system": SYSTEM_PROMPT + f"\n\nDATI ATTUALI AGENZIA:\n{sheet_context}",
                "messages": [{"role": "user", "content": user_message}]
            },
            timeout=30
        )
        data = response.json()
        return data["content"][0]["text"]
    except Exception as e:
        return f"Errore connessione AI: {e}"

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ALLOWED_GROUP_ID
    
    chat = update.effective_chat
    user = update.effective_user
    text = update.message.text
    
    # Imposta automaticamente il gruppo autorizzato al primo messaggio
    if chat.type in ["group", "supergroup"]:
        if ALLOWED_GROUP_ID is None:
            ALLOWED_GROUP_ID = chat.id
            print(f"Gruppo autorizzato impostato: {chat.id}")
        elif chat.id != ALLOWED_GROUP_ID:
            return  # Ignora altri gruppi
    elif chat.type == "private":
        pass  # Accetta anche messaggi privati
    
    if not text:
        return

    # Mostra "sta scrivendo..."
    await context.bot.send_chat_action(chat_id=chat.id, action="typing")
    
    # Prendi i dati dal foglio
    sheet_data = get_sheet_data()
    
    # Chiedi a Claude
    nome = user.first_name or "collega"
    response = await ask_claude(f"{nome} dice: {text}", sheet_data)
    
    # Controlla se è una transazione
    if response.startswith("TRANSACTION:"):
        try:
            json_str = response.replace("TRANSACTION:", "").strip()
            tx_data = json.loads(json_str)
            success = save_transaction(tx_data)
            if success:
                tipo_emoji = "💚" if tx_data["tipo"] == "entrata" else "🔴"
                reply = (
                    f"{tipo_emoji} *Transazione registrata!*\n\n"
                    f"📝 {tx_data.get('descrizione', '')}\n"
                    f"💶 €{tx_data.get('importo', '')}\n"
                    f"📂 {tx_data.get('categoria', '')}\n"
                    f"👤 {tx_data.get('fornitore_cliente', '') or '—'}\n"
                    f"✈️ {tx_data.get('tour', '') or '—'}\n"
                    f"📅 {datetime.now().strftime('%d/%m/%Y')}"
                )
            else:
                reply = "❌ Errore nel salvare la transazione. Riprova."
        except Exception as e:
            reply = f"❌ Errore nel processare la transazione: {e}"
    else:
        reply = response
    
    await update.message.reply_text(reply, parse_mode="Markdown")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Ciao! Sono *Athos*, il vostro contabile AI!\n\n"
        "Potete scrivermi:\n"
        "• _'Entrata 500€ tour Roma da Mario Rossi'_\n"
        "• _'Uscita 120€ carburante'_\n"
        "• _'Report del mese'_\n"
        "• _'Qual è il profitto totale?'_\n"
        "• _'Quanto abbiamo speso in guide?'_\n\n"
        "Sono pronto! ✅",
        parse_mode="Markdown"
    )

def main():
    print("🚀 Athos Bot avviato...")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()
