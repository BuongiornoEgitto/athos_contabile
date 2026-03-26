import os
import json
import requests
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
 
# ============================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
SHEETS_URL = os.environ.get("SHEETS_URL")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ALLOWED_GROUP_ID = None
# ============================================================
 
SYSTEM_PROMPT = """Sei Athos, un agente AI contabile specializzato per agenzie di viaggi ed escursioni.
Sei preciso, professionale e cordiale. Rispondi SEMPRE in italiano e in modo conciso.
 
REGOLE SPECIALI PER I MESSAGGI:
- Se il messaggio inizia con "+" è sempre un'ENTRATA
- Se il messaggio inizia con "-" è sempre un'USCITA
- Esempi rapidi:
  "+1000 tour Roma Mario Rossi" → entrata 1000€, descrizione "tour Roma Mario Rossi"
  "-300 guida Vesuvio Giuseppe" → uscita 300€, descrizione "guida Vesuvio Giuseppe"
  "-500 hotel Hilton" → uscita 500€, descrizione "hotel Hilton"
 
Puoi fare due cose:
 
1. REGISTRARE UNA TRANSAZIONE
Quando l'utente vuole aggiungere entrata/uscita, rispondi SOLO con questo JSON (nient'altro prima o dopo):
TRANSACTION:{"tipo":"entrata","importo":500,"descrizione":"Tour Roma Mario Rossi","note":""}
 
Il campo "tipo" può essere solo "entrata" o "uscita".
Il campo "importo" è sempre un numero senza simbolo €.
Il campo "descrizione" è SOLO la prima parola dopo la cifra. Esempio: "+100 tizio commissioni motorata" → descrizione = "tizio".
Il campo "note" è tutto il resto dopo la prima parola. Esempio: "+100 tizio commissioni motorata" → note = "commissioni motorata".
Se c'è solo una parola dopo la cifra, note è vuoto "".
 
2. RISPONDERE A DOMANDE
Analizza i dati del foglio e rispondi in modo chiaro con numeri precisi.
Per i report usa emoji per rendere il messaggio leggibile su Telegram.
 
Se il messaggio non è chiaro, chiedi una breve conferma.
Rispondi sempre in italiano.
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
 
        summary = f"TOTALE ENTRATE: €{entrate:.2f}\nTOTALE USCITE: €{uscite:.2f}\nPROFITTO NETTO: €{entrate-uscite:.2f}\n\n"
        summary += "ULTIME 10 TRANSAZIONI:\n"
        for t in transactions[-10:]:
            summary += f"- {t.get('data','')} | {t.get('tipo','').upper()} | €{t.get('importo','')} | {t.get('descrizione','')}\n"
 
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
 
    if chat.type in ["group", "supergroup"]:
        if ALLOWED_GROUP_ID is None:
            ALLOWED_GROUP_ID = chat.id
            print(f"Gruppo autorizzato impostato: {chat.id}")
        elif chat.id != ALLOWED_GROUP_ID:
            return
    
    if not text:
        return
 
    await context.bot.send_chat_action(chat_id=chat.id, action="typing")
 
    sheet_data = get_sheet_data()
    nome = user.first_name or "collega"
    response = await ask_claude(text, sheet_data)
    if response.startswith("TRANSACTION:"):
        try:
            json_str = response.replace("TRANSACTION:", "").strip()
            tx_data = json.loads(json_str)
            tx_data["guida"] = (user.first_name or "")[:8]
            success = save_transaction(tx_data)
            if success:
                tipo_emoji = "💚" if tx_data["tipo"] == "entrata" else "🔴"
                reply = (
                    f"{tipo_emoji} *Transazione registrata!*\n\n"
                    f"📝 {tx_data.get('descrizione', '')}\n"
                    f"💶 €{tx_data.get('importo', '')}\n"
                    f"📅 {datetime.now().strftime('%d/%m/%Y')}"
                )
                if tx_data.get('note'):
                    reply += f"\n📌 {tx_data.get('note')}"
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
        "✏️ *Scrittura rapida:*\n"
        "`+1000 tour Roma Mario Rossi` → entrata\n"
        "`-300 guida Vesuvio Giuseppe` → uscita\n\n"
        "💬 *Oppure scrivi normalmente:*\n"
        "• _'Entrata 500€ escursione Etna'_\n"
        "• _'Uscita 120€ carburante'_\n\n"
        "📊 *Report e analisi:*\n"
        "• _'Report del mese'_\n"
        "• _'Qual è il profitto totale?'_\n"
        "• _'Quanto abbiamo speso in guide?'_\n\n"
        "Sono pronto! ✅",
        parse_mode="Markdown"
    )
 
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Come usare Athos:*\n\n"
        "➕ *Entrata:* `+importo descrizione cliente`\n"
        "➖ *Uscita:* `-importo descrizione fornitore`\n\n"
        "Esempi:\n"
        "`+2000 pacchetto Sicilia famiglia Rossi`\n"
        "`-450 hotel partner Taormina`\n"
        "`-80 carburante pulmino`\n\n"
        "📊 *Report:* scrivi 'report', 'profitto', 'riepilogo'",
        parse_mode="Markdown"
    )
 
def main():
    print("🚀 Athos Bot avviato...")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()
 
if __name__ == "__main__":
    main()
