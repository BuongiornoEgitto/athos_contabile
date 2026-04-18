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
Rispondi SEMPRE in italiano e in modo conciso.

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
"+100 Mario commissioni" → TRANSACTION:{"tipo":"entrata","importo_eur":100,"importo_le":"","descrizione":"Mario commissioni"}
"-300 Giuseppe guida" → TRANSACTION:{"tipo":"uscita","importo_eur":300,"importo_le":"","descrizione":"Giuseppe guida"}
"-1000 LE guida canyon" → TRANSACTION:{"tipo":"uscita","importo_eur":"","importo_le":1000,"descrizione":"guida canyon"}
"-1000le guida" → TRANSACTION:{"tipo":"uscita","importo_eur":"","importo_le":1000,"descrizione":"guida"}
"spesa 500 lire sim" → TRANSACTION:{"tipo":"uscita","importo_eur":"","importo_le":500,"descrizione":"sim"}
"+200 EGP foto" → TRANSACTION:{"tipo":"entrata","importo_eur":"","importo_le":200,"descrizione":"foto"}

Rispondi SOLO con il JSON nel formato:
TRANSACTION:{"tipo":"...","importo_eur":...,"importo_le":"...","descrizione":"..."}

Se il messaggio non e' una transazione, rispondi: "Scrivi nel formato +/- importo descrizione"
"""


def save_transaction(data: dict):
    try:
        data["data"] = datetime.now().strftime("%Y-%m-%d")
        response = requests.post(SHEETS_URL, json=data, timeout=10)
        return response.text == "OK"
    except:
        return False


async def ask_claude(user_message: str) -> str:
    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01"
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 200,
                # System prompt is stable across requests — cached so repeated
                # messages don't re-bill the prompt tokens.
                "system": [
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"}
                    }
                ],
                "messages": [{"role": "user", "content": user_message}]
            },
            timeout=30
        )
        data = response.json()

        if "error" in data:
            error_msg = data["error"].get("message", "Errore sconosciuto")
            return f"❌ Errore API: {error_msg}"

        # Log token usage (cache_read > 0 confirms caching is hitting)
        usage = data.get("usage", {})
        print(
            f"tokens — input: {usage.get('input_tokens', 0)}, "
            f"cache_read: {usage.get('cache_read_input_tokens', 0)}, "
            f"cache_create: {usage.get('cache_creation_input_tokens', 0)}, "
            f"output: {usage.get('output_tokens', 0)}"
        )

        return data["content"][0]["text"]
    except Exception as e:
        return f"❌ Errore connessione AI: {e}"


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ALLOWED_GROUP_ID

    chat = update.effective_chat
    user = update.effective_user
    text = update.message.text

    if chat.type in ["group", "supergroup"]:
        if ALLOWED_GROUP_ID is None:
            ALLOWED_GROUP_ID = chat.id
        elif chat.id != ALLOWED_GROUP_ID:
            return

    if not text or text.startswith("@"):
        return

    await context.bot.send_chat_action(chat_id=chat.id, action="typing")

    response = await ask_claude(text)

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
                reply = "❌ Errore nel salvare. Riprova."
        except Exception as e:
            reply = f"❌ Errore: {e}"
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
