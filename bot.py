import os
import re
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


def detect_currency(text):
    """Rileva se l'importo e' in lire egiziane e separa il valore."""
    le_pattern = re.compile(
        r'(\d+(?:[.,]\d+)?)\s*(?:le|LE|L\.E\.|l\.e\.|lire(?:\s+egiziane)?|EGP|egp)\b',
        re.IGNORECASE
    )
    match_le = le_pattern.search(text)
    if match_le:
        importo = float(match_le.group(1).replace(',', '.'))
        testo_pulito = le_pattern.sub('', text, count=1).strip()
        return importo, "le", testo_pulito

    return None, "eur", text


SYSTEM_PROMPT = """Sei Athos, un agente AI contabile specializzato per agenzie di viaggi ed escursioni.
Sei preciso, professionale e cordiale. Rispondi SEMPRE in italiano e in modo conciso.

IMPORTANTE SUL FORMATO IMPORTO:
- Se nel messaggio c'e' un importo con LE, L.E., lire, EGP → metti l'importo nel campo "importo_le" e lascia "importo_eur" vuoto ("")
- Se l'importo e' in euro (nessuna valuta specificata, o simbolo €) → metti l'importo nel campo "importo_eur" e lascia "importo_le" vuoto ("")
- NON convertire mai. Registra il numero esatto che l'utente ha scritto.

SYSTEM_PROMPT = """Sei Athos, un agente AI contabile specializzato per agenzie di viaggi ed escursioni.
Sei preciso, professionale e cordiale. Rispondi SEMPRE in italiano e in modo conciso.

REGOLE PER I MESSAGGI:
- Se il messaggio inizia con "+" è sempre un ENTRATA
- Se il messaggio inizia con "-" è sempre un USCITA
- Tutto il testo dopo il segno e la cifra va interamente in "descrizione"

VALUTA:
- Se NON è specificata nessuna valuta, l'importo è in EURO
- Se trovi LE, L.E., EGP, lire, lire egiziane, l'importo è in LIRE EGIZIANE
- NON convertire nulla
- NON chiedere conferma
- Se l'importo è in euro, compila "importo_eur"
- Se l'importo è in lire egiziane, compila "importo_le"
- L'altro campo deve restare vuoto

Quando l'utente vuole aggiungere entrata/uscita, rispondi SOLO con questo JSON (nient'altro prima o dopo):

TRANSACTION:{"tipo":"entrata","importo_eur":100,"importo_le":"","descrizione":"tizio commissioni motorata"}

oppure

TRANSACTION:{"tipo":"uscita","importo_eur":"","importo_le":300,"descrizione":"guida canyon 300 LE"}

REGOLE JSON:
- "tipo" può essere solo "entrata" o "uscita"
- "importo_eur" contiene solo numeri oppure stringa vuota
- "importo_le" contiene solo numeri oppure stringa vuota
- "descrizione" contiene tutto il testo dopo la cifra

Se il messaggio non è una registrazione ma una domanda, rispondi normalmente in italiano.
"""

1. REGISTRARE UNA TRANSAZIONE
Quando l'utente vuole aggiungere entrata/uscita, rispondi SOLO con questo JSON (nient'altro prima o dopo):
TRANSACTION:{"tipo":"entrata","importo_eur":500,"importo_le":"","descrizione":"tizio commissioni"}

O per lire egiziane:
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
        summary = f"TOTALE ENTRATE: €{entrate_eur:.2f} + {entrate_le:.0f} LE\n"
        summary += f"TOTALE USCITE: €{uscite_eur:.2f} + {uscite_le:.0f} LE\n\n"
        summary += "ULTIME 10 TRANSAZIONI:\n"
        for t in transactions[-10:]:
            eur = t.get('importo_eur', '')
            le = t.get('importo_le', '')
            importo_str = f"€{eur}" if eur else f"{le} LE"
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

        # normalizza campi
        tx_data["importo_eur"] = tx_data.get("importo_eur", "")
        tx_data["importo_le"] = tx_data.get("importo_le", "")

        success = save_transaction(tx_data)

        if success:
            tipo_emoji = "💚" if tx_data["tipo"] == "entrata" else "🔴"

            if tx_data.get("importo_eur", "") != "":
                importo_txt = f"€{tx_data.get('importo_eur', '')}"
            elif tx_data.get("importo_le", "") != "":
                importo_txt = f"{tx_data.get('importo_le', '')} LE"
            else:
                importo_txt = "-"

            reply = (
                f"{tipo_emoji} *Transazione registrata!*\n\n"
                f"📝 {tx_data.get('descrizione', '')}\n"
                f"💰 {importo_txt}\n"
                f"👤 {tx_data.get('guida', '')}\n"
                f"📅 {datetime.now().strftime('%d/%m/%Y')}"
            )
        else:
            reply = "❌ Errore nel salvare la transazione. Riprova."
    except Exception as e:
        reply = f"❌ Errore nel processare la transazione: {e}"
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
        "`+100 tizio commissioni` → entrata in euro\n"
        "`-300 Giuseppe guida` → uscita in euro\n"
        "`-1000 LE guida canyon` → uscita in lire\n"
        "`+500 EGP commissioni` → entrata in lire\n\n"
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
