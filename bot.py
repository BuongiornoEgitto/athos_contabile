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

REGOLE IMPORTO:
- Se il messaggio NON specifica valuta → importo va in "importo_eur", "importo_le" resta ""
- Se il messaggio contiene LE, L.E., lire, EGP, egp → importo va in "importo_le", "importo_eur" resta ""
- NON convertire mai. Scrivi il numero esatto.

REGOLE TIPO:
- "+" o parole "in", "incasso", "entrata", "pagato da" → tipo: "entrata"
- "-" o parole "spesa", "out", "pagamento", "costo" → tipo: "uscita"

REGOLE DESCRIZIONE:
- Tutto il testo dopo il segno, la cifra e l'eventuale valuta va in "descrizione"

ESEMPI:
"+100 Mario commissioni" → tipo: entrata, importo_eur: 100, importo_le: "", descrizione: "Mario commissioni"
"-300 Giuseppe guida" → tipo: uscita, importo_eur: 300, importo_le: "", descrizione: "Giuseppe guida"
"+500 Mario" → tipo: entrata, importo_eur: 500, importo_le: "", descrizione: "Mario"
"-1000 LE guida canyon" → tipo: uscita, importo_eur: "", importo_le: 1000, descrizione: "guida canyon"
"-1000le guida canyon" → tipo: uscita, importo_eur: "", importo_le: 1000, descrizione: "guida canyon"
"spesa 500 lire sim card" → tipo: uscita, importo_eur: "", importo_le: 500, descrizione: "sim card"
"+200 EGP commissioni foto" → tipo: entrata, importo_eur: "", importo_le: 200, descrizione: "commissioni foto"
"in 80 Mario" → tipo: entrata, importo_eur: 80, importo_le: "", descrizione: "Mario"
"out 300 LE guide" → tipo: uscita, importo_eur: "", importo_le: 300, descrizione: "guide"

1. REGISTRARE UNA TRANSAZIONE
Rispondi SOLO con questo JSON (nient'altro prima o dopo):
TRANSACTION:{"tipo":"entrata","importo_eur":100,"importo_le":"","descrizione":"Mario commissioni"}

Per lire egiziane:
TRANSACTION:{"tipo":"uscita","importo_eur":"","importo_le":1000,"descrizione":"guida canyon"}

2. RISPONDERE A DOMANDE
Analizza i dati del foglio e rispondi in modo chiaro con numeri precisi.
Per i report usa emoji per rendere il messaggio leggibile su Telegram.

Se il messaggio non e' chiaro, chiedi una breve conferma.
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
        entrate_eur = sum(float(t.get("importo_eur", 0) or 0) for t in transactions if t.get("tipo") == "entrata")
        uscite_eur = sum(float(t.get("importo_eur", 0) or 0) for t in transactions if t.get("tipo") == "uscita")
        entrate_le = sum(float(t.get("importo_le", 0) or 0) for t in transactions if t.get("tipo") == "entrata")
        uscite_le = sum(float(t.get("importo_le", 0) or 0) for t in transactions if t.get("tipo") == "uscita")
        summary = f"ENTRATE: €{entrate_eur:.2f} + {entrate_le:.0f} LE\n"
        summary += f"USCITE: €{uscite_eur:.2f} + {uscite_le:.0f} LE\n\n"
        summary += "ULTIME 10 TRANSAZIONI:\n"
        for t in transactions[-10:]:
            eur = t.get('importo_eur', '')
            le = t.get('importo_le', '')
            if eur and str(eur).strip() != '':
                importo_str = f"€{eur}"
            elif le and str(le).strip() != '':
                importo_str = f"{le} LE"
            else:
                importo_str = "?"
            summary += f"- {t.get('data','')} | {t.get('guida','')} | {t.get('tipo','').upper()} | {importo_str} | {t.get('descrizione','')}\n"
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

        if "error" in data:
            error_msg = data["error"].get("message", "Errore sconosciuto")
            print(f"ERRORE API ANTHROPIC: {error_msg}")
            return f"❌ Errore API: {error_msg}"

        return data["content"][0]["text"]
    except Exception as e:
        print(f"ERRORE CONNESSIONE: {e}")
        return f"❌ Errore connessione AI: {e}"


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

    if text.startswith("@"):
        return

    await context.bot.send_chat_action(chat_id=chat.id, action="typing")

    sheet_data = get_sheet_data()
    response = await ask_claude(text, sheet_data)

    if response.startswith("TRANSACTION:"):
        try:
            json_str = response.replace("TRANSACTION:", "").strip()
            tx_data = json.loads(json_str)
            tx_data["guida"] = (user.first_name or "")[:8]
            success = save_transaction(tx_data)
            if success:
                tipo_emoji = "💚" if tx_data["tipo"] == "entrata" else "🔴"
                eur = tx_data.get('importo_eur', '')
                le = tx_data.get('importo_le', '')
                if eur and str(eur) != "":
                    importo_str = f"€{eur}"
                else:
                    importo_str = f"{le} LE"
                reply = (
                    f"{tipo_emoji} *Transazione registrata!*\n\n"
                    f"📝 {tx_data.get('descrizione', '')}\n"
                    f"💶 {importo_str}\n"
                    f"👤 {tx_data.get('guida', '')}\n"
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
        "✏️ *Come registrare:*\n"
        "`+100 Mario commissioni` → entrata euro\n"
        "`-300 Giuseppe guida` → uscita euro\n"
        "`-1000 LE guida canyon` → uscita lire\n"
        "`+500 EGP commissioni` → entrata lire\n\n"
        "📊 *Report:* scrivi 'report' o 'profitto'\n\n"
        "Sono pronto! ✅",
        parse_mode="Markdown"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Come usare Athos:*\n\n"
        "➕ *Entrata:* `+importo descrizione`\n"
        "➖ *Uscita:* `-importo descrizione`\n\n"
        "💱 *Valute:*\n"
        "• Euro (default): `+100 Mario`\n"
        "• Lire egiziane: `-1000LE guida` o `-1000 lire guida`\n\n"
        "📊 *Report:* scrivi 'report', 'profitto'",
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
