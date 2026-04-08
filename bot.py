import os
import re
import json
import requests
from datetime import datetime, time
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
import pytz

# ============================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
SHEETS_URL = os.environ.get("SHEETS_URL")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ALLOWED_GROUP_ID = None
# ============================================================

# Tassi di cambio fissi
LE_TO_EUR = 52  # 1 EUR = 52 LE
USD_TO_EUR = 1.08  # 1 EUR = 1.08 USD

# Fuso orario Egitto
TIMEZONE = pytz.timezone("Africa/Cairo")


def convert_currency(text):
    """Converte lire egiziane e dollari in euro direttamente nel testo."""
    nota = None

    le_pattern = re.compile(
        r'(\d+(?:[.,]\d+)?)\s*(?:le|LE|L\.E\.|l\.e\.|lire(?:\s+egiziane)?|EGP|egp)\b',
        re.IGNORECASE
    )
    match_le = le_pattern.search(text)
    if match_le:
        importo_le = float(match_le.group(1).replace(',', '.'))
        importo_eur = round(importo_le / LE_TO_EUR, 2)
        nota = f"({int(importo_le)} LE)"
        text = le_pattern.sub(str(importo_eur), text, count=1)
        return text, nota

    usd_pattern = re.compile(
        r'(\d+(?:[.,]\d+)?)\s*(?:\$|USD|usd)\b|\$\s*(\d+(?:[.,]\d+)?)',
        re.IGNORECASE
    )
    match_usd = usd_pattern.search(text)
    if match_usd:
        importo_usd = float((match_usd.group(1) or match_usd.group(2)).replace(',', '.'))
        importo_eur = round(importo_usd / USD_TO_EUR, 2)
        nota = f"({int(importo_usd)} USD)"
        text = usd_pattern.sub(str(importo_eur), text, count=1)
        return text, nota

    return text, nota


SYSTEM_PROMPT = """Sei Athos, un agente AI contabile specializzato per agenzie di viaggi ed escursioni.
Sei preciso, professionale e cordiale. Rispondi SEMPRE in italiano e in modo conciso.

IMPORTANTE: Tutti gli importi che ricevi sono GIA' convertiti in EURO. Non fare nessuna conversione.
Se nella descrizione vedi una nota tra parentesi come "(300 LE)" o "(7 USD)", lasciala nella descrizione.

REGOLE PER I MESSAGGI:
- Se il messaggio inizia con "+" è sempre un'ENTRATA
- Se il messaggio inizia con "-" è sempre un'USCITA
- Parole come "spesa", "out", "pagamento", "costo" indicano USCITA
- Parole come "in", "incasso", "entrata", "pagato da" indicano ENTRATA
- Tutto il testo dopo il segno e la cifra va interamente in "descrizione"
- Esempi:
  "+100 tizio commissioni motorata" → tipo: entrata, importo: 100, descrizione: "tizio commissioni motorata"
  "-300 Giuseppe guida Vesuvio" → tipo: uscita, importo: 300, descrizione: "Giuseppe guida Vesuvio"
  "+500 Mario" → tipo: entrata, importo: 500, descrizione: "Mario"
  "-80 carburante pulmino" → tipo: uscita, importo: 80, descrizione: "carburante pulmino"
  "spesa 5.77 guide (300 LE)" → tipo: uscita, importo: 5.77, descrizione: "guide (300 LE)"

1. REGISTRARE UNA TRANSAZIONE
Quando l'utente vuole aggiungere entrata/uscita, rispondi SOLO con questo JSON (nient'altro prima o dopo):
TRANSACTION:{"tipo":"entrata","importo":500,"descrizione":"tizio commissioni motorata"}

Il campo "tipo" può essere solo "entrata" o "uscita".
Il campo "importo" è sempre un numero senza simbolo €.
Il campo "descrizione" contiene TUTTO il testo dopo la cifra.

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
            summary += f"- {t.get('data','')} | {t.get('guida','')} | {t.get('tipo','').upper()} | €{t.get('importo','')} | {t.get('descrizione','')}\n"
        return summary
    except Exception as e:
        return f"Errore lettura dati: {e}"


def save_transaction(data: dict):
    try:
        data["data"] = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
        response = requests.post(SHEETS_URL, json=data, timeout=10)
        return response.text == "OK"
    except:
        return False


async def esegui_riepilogo():
    """Calcola il riepilogo del giorno e lo scrive nel foglio Riepilogo."""
    try:
        oggi = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
        response = requests.get(SHEETS_URL, timeout=10)
        rows = response.json()

        if len(rows) <= 1:
            return None, "Nessuna transazione registrata."

        headers = rows[0]
        transazioni_oggi = []
        for row in rows[1:]:
            if any(row):
                t = dict(zip(headers, row))
                if t.get("data", "") == oggi:
                    transazioni_oggi.append(t)

        if not transazioni_oggi:
            return None, f"Nessuna transazione oggi ({oggi})."

        entrate = sum(float(t.get("importo", 0)) for t in transazioni_oggi if t.get("tipo") == "entrata")
        uscite = sum(float(t.get("importo", 0)) for t in transazioni_oggi if t.get("tipo") == "uscita")
        profitto = entrate - uscite

        riepilogo = {
            "action": "riepilogo",
            "data": oggi,
            "entrate": round(entrate, 2),
            "uscite": round(uscite, 2),
            "profitto": round(profitto, 2),
            "num_transazioni": len(transazioni_oggi)
        }

        result = requests.post(SHEETS_URL, json=riepilogo, timeout=10)
        return riepilogo, result.text

    except Exception as e:
        return None, f"Errore: {e}"


async def riepilogo_giornaliero(context: ContextTypes.DEFAULT_TYPE):
    """Job automatico alle 23:00."""
    riepilogo, result = await esegui_riepilogo()
    if riepilogo:
        print(f"Riepilogo {riepilogo['data']} salvato: {result}")
    else:
        print(f"Riepilogo saltato: {result}")


async def riepilogo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /riepilogo per forzare il riepilogo manualmente."""
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    riepilogo, result = await esegui_riepilogo()

    if riepilogo and result == "OK":
        reply = (
            f"📋 *Riepilogo salvato nel foglio!*\n\n"
            f"📅 {riepilogo['data']}\n"
            f"💚 Entrate: €{riepilogo['entrate']}\n"
            f"🔴 Uscite: €{riepilogo['uscite']}\n"
            f"💰 Profitto: €{riepilogo['profitto']}\n"
            f"📝 Transazioni: {riepilogo['num_transazioni']}"
        )
    elif riepilogo:
        reply = f"❌ Errore nel salvare il riepilogo: {result}"
    else:
        reply = f"⚠️ {result}"

    await update.message.reply_text(reply, parse_mode="Markdown")


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

    text_convertito, nota_conversione = convert_currency(text)

    if nota_conversione:
        text_convertito = text_convertito.rstrip() + " " + nota_conversione

    sheet_data = get_sheet_data()
    response = await ask_claude(text_convertito, sheet_data)

    if response.startswith("TRANSACTION:"):
        try:
            json_str = response.replace("TRANSACTION:", "").strip()
            tx_data = json.loads(json_str)
            tx_data["guida"] = (user.first_name or "")[:8]
            success = save_transaction(tx_data)
            if success:
                tipo_emoji = "💚" if tx_data["tipo"] == "entrata" else "🔴"
                conversione_info = ""
                if nota_conversione:
                    conversione_info = f"\n🔄 Convertito da {nota_conversione}"
                reply = (
                    f"{tipo_emoji} *Transazione registrata!*\n\n"
                    f"📝 {tx_data.get('descrizione', '')}\n"
                    f"💶 €{tx_data.get('importo', '')}"
                    f"{conversione_info}\n"
                    f"👤 {tx_data.get('guida', '')}\n"
                    f"📅 {datetime.now(TIMEZONE).strftime('%d/%m/%Y')}"
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
        "`+100 tizio commissioni motorata` → entrata\n"
        "`-300 Giuseppe guida Vesuvio` → uscita\n\n"
        "💱 *Valute:* scrivi in LE, EGP o $ e converto automaticamente!\n"
        "`-1000LE guida canyon` → registra €19.23\n\n"
        "📊 *Report e analisi:*\n"
        "• _'Report del mese'_\n"
        "• _'Qual è il profitto totale?'_\n"
        "• _'Quanto abbiamo speso?'_\n\n"
        "📋 /riepilogo → salva il riepilogo di oggi nel foglio\n"
        "⏰ Riepilogo automatico ogni sera alle 23:00\n\n"
        "Sono pronto! ✅",
        parse_mode="Markdown"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Come usare Athos:*\n\n"
        "➕ *Entrata:* `+importo descrizione`\n"
        "➖ *Uscita:* `-importo descrizione`\n\n"
        "💱 *Valute accettate:*\n"
        "• Euro (default): `+100 Mario`\n"
        "• Lire egiziane: `-1000LE guida` o `-1000 lire guida`\n"
        "• Dollari: `-7$ kefie` o `-7 USD kefie`\n\n"
        "Tasso: 1€ = 52 LE | 1€ = 1.08$\n\n"
        "📋 /riepilogo → salva riepilogo di oggi\n"
        "📊 *Report:* scrivi 'report', 'profitto', 'riepilogo'",
        parse_mode="Markdown"
    )


def main():
    print("🚀 Athos Bot avviato...")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("riepilogo", riepilogo_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Riepilogo giornaliero alle 23:00 ora Egitto
    app.job_queue.run_daily(
        riepilogo_giornaliero,
        time=time(hour=23, minute=0, tzinfo=TIMEZONE)
    )
    print("⏰ Riepilogo giornaliero programmato alle 23:00")

    app.run_polling()


if __name__ == "__main__":
    main()
