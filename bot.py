"""Athos Contabile — Telegram bot for Buongiorno Egitto.

Double-entry version. Every Telegram message becomes a balanced journal entry
in Supabase. The Google Sheet is still populated as a simple single-entry
backup, but ONLY for real economic events (guides logging income/expenses) —
internal cash transfers (/raccolgo, /verso) go to Supabase only, to keep the
Sheet clean and free of double-counting.

Flow:
  - Any message → auto-register sender in `telegram_users` (upsert)
  - If sender unmapped (no account_code) → friendly "wait for Omar" reply
  - If sender is `guida` → Claude parses, bot writes journal entry + Sheet
  - If sender is `contabile` → must use /raccolgo or /verso slash commands
  - If sender is `proprieta` (Omar) → can log entries like a guide would
    (they land on the `proprieta` cash account instead of a guide's cassa)
"""
import os
import json
import requests
from datetime import datetime
from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    filters,
    ContextTypes,
)

# ============================================================
# Config — all from Railway env vars
# ============================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
SHEETS_URL = os.environ.get("SHEETS_URL")  # legacy — dual-write backup
ALLOWED_GROUP_ID = None

# ============================================================
# Claude system prompt — now includes the chart of accounts so
# Claude can classify each transaction into the right category
# ============================================================
SYSTEM_PROMPT = """Sei Athos, agente AI contabile per Buongiorno Egitto (agenzia viaggi Egitto).
Rispondi SEMPRE in italiano e in modo conciso.

Il tuo compito: parsare messaggi tipo "+200 tour piramidi" e restituire JSON con
il giusto conto economico (ricavi/costi).

REGOLE IMPORTO:
- Se NON specifica valuta → importo va in "importo_eur", "importo_le" resta ""
- Se contiene LE, L.E., lire, EGP, egp → importo va in "importo_le", "importo_eur" resta ""
- NON convertire mai. Scrivi il numero esatto.

REGOLE TIPO:
- "+", "in", "incasso", "entrata", "pagato da", "ricevuto da cliente" → tipo: "entrata"
- "-", "spesa", "out", "pagamento", "costo", "speso per" → tipo: "uscita"

CONTO (account_code): scegli UNO di questi in base alla descrizione:

RICAVI (quando tipo=entrata):
- ricavi_tour → tour principali (piramidi, luxor, assuan, abu simbel, escursione di piu giorni)
- ricavi_escursioni → escursioni brevi giornaliere (deserto, mare, quad, cammello)
- ricavi_commissioni → commissioni da partner, hotel, negozi
- ricavi_foto → servizi fotografici
- ricavi_altri → tutto il resto dei ricavi

COSTI (quando tipo=uscita):
- costi_ristoranti → pranzi, cene con clienti, ristoranti
- costi_motorata → cammelli, barche, motoscafi, feluche, quad, motociclette
- costi_ingressi → biglietti siti archeologici, musei, templi
- costi_trasporti → benzina, taxi, voli interni, bus, treni
- costi_alloggio → hotel, case, resort per clienti
- costi_guide_esterne → guide occasionali NON del team fisso
- costi_marketing → pubblicita, social, annunci
- costi_telefono → SIM, ricariche, internet, roaming
- costi_stipendi → compensi guide fisse del team
- costi_commissioni → commissioni pagate a partner, agenti
- costi_bancari → fee PayPal, Stripe, bonifici, cambio valuta
- costi_amministrativi → commercialista, licenze, permessi
- costi_ufficio → cancelleria, attrezzatura, computer
- costi_altri → tutto il resto delle spese

REGOLE DESCRIZIONE:
- Tutto il testo dopo segno/cifra/valuta va in "descrizione"

ESEMPI:
"+200 tour piramidi" → TRANSACTION:{"tipo":"entrata","importo_eur":200,"importo_le":"","descrizione":"tour piramidi","account_code":"ricavi_tour"}
"+150 escursione deserto" → TRANSACTION:{"tipo":"entrata","importo_eur":150,"importo_le":"","descrizione":"escursione deserto","account_code":"ricavi_escursioni"}
"-50 pranzo clienti" → TRANSACTION:{"tipo":"uscita","importo_eur":50,"importo_le":"","descrizione":"pranzo clienti","account_code":"costi_ristoranti"}
"-300 cammello" → TRANSACTION:{"tipo":"uscita","importo_eur":300,"importo_le":"","descrizione":"cammello","account_code":"costi_motorata"}
"-1000 LE guida canyon" → TRANSACTION:{"tipo":"uscita","importo_eur":"","importo_le":1000,"descrizione":"guida canyon","account_code":"costi_guide_esterne"}
"-500 LE biglietto valle re" → TRANSACTION:{"tipo":"uscita","importo_eur":"","importo_le":500,"descrizione":"biglietto valle re","account_code":"costi_ingressi"}
"+100 commissione hotel" → TRANSACTION:{"tipo":"entrata","importo_eur":100,"importo_le":"","descrizione":"commissione hotel","account_code":"ricavi_commissioni"}

Rispondi SOLO con il JSON nel formato:
TRANSACTION:{"tipo":"...","importo_eur":...,"importo_le":"...","descrizione":"...","account_code":"..."}

Se il messaggio non e' una transazione, rispondi: "Scrivi nel formato +/- importo descrizione"
"""


# ============================================================
# Supabase REST helpers
# ============================================================
def _sb_headers(extra: dict | None = None) -> dict:
    h = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def _sb_configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)


def upsert_telegram_user(user) -> None:
    """Called on every incoming message. Creates the row if missing,
    updates display_name/username/last_seen if present. Never overwrites
    role or account_code (those are set manually by Omar in Supabase)."""
    if not _sb_configured():
        return
    payload = {
        "telegram_user_id": user.id,
        "display_name": user.first_name or "",
        "username": user.username or None,
        "last_seen": datetime.utcnow().isoformat(),
    }
    try:
        # on_conflict: only update the 3 volatile fields, leave role/account_code alone
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/telegram_users"
            "?on_conflict=telegram_user_id",
            headers=_sb_headers({
                "Prefer": "resolution=merge-duplicates,return=minimal",
            }),
            json=payload,
            timeout=10,
        )
        if r.status_code not in (200, 201, 204):
            print(f"telegram_users upsert: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"telegram_users upsert error: {e}")


def get_telegram_user(user_id: int) -> dict | None:
    """Read the telegram_users row (including role + account_code)."""
    if not _sb_configured():
        return None
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/telegram_users"
            f"?telegram_user_id=eq.{user_id}&select=*",
            headers=_sb_headers(),
            timeout=10,
        )
        if r.status_code == 200:
            rows = r.json()
            return rows[0] if rows else None
        print(f"get_telegram_user: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"get_telegram_user error: {e}")
    return None


def find_guida_by_name(name: str) -> dict | None:
    """Case-insensitive lookup of a guida by display_name (used by /raccolgo)."""
    if not _sb_configured() or not name:
        return None
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/telegram_users"
            f"?role=eq.guida&display_name=ilike.{name}&select=*",
            headers=_sb_headers(),
            timeout=10,
        )
        if r.status_code == 200:
            rows = r.json()
            return rows[0] if rows else None
        print(f"find_guida_by_name: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"find_guida_by_name error: {e}")
    return None


def insert_journal_entry(
    description: str,
    source: str,
    telegram_user_id: int,
    lines: list,
    entry_date: str | None = None,
) -> str | None:
    """Create a balanced journal entry with N lines. Rolls back on failure.

    lines: list of {account_code, dare, avere, currency}. Sum(dare)==sum(avere)
    per currency must hold, or the balance check RPC will raise and we rollback.
    """
    if not _sb_configured():
        return None

    # 1. Header
    entry_payload = {
        "entry_date": entry_date or datetime.now().strftime("%Y-%m-%d"),
        "description": description,
        "source": source,
        "telegram_user_id": telegram_user_id,
    }
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/journal_entries",
            headers=_sb_headers({"Prefer": "return=representation"}),
            json=entry_payload,
            timeout=10,
        )
    except Exception as e:
        print(f"journal_entries insert error: {e}")
        return None
    if r.status_code not in (200, 201):
        print(f"journal_entries insert: {r.status_code} {r.text[:200]}")
        return None
    entry_id = r.json()[0]["id"]

    # 2. Lines
    for ln in lines:
        ln["entry_id"] = entry_id
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/journal_lines",
            headers=_sb_headers({"Prefer": "return=minimal"}),
            json=lines,
            timeout=10,
        )
    except Exception as e:
        print(f"journal_lines insert error: {e}")
        _rollback_entry(entry_id)
        return None
    if r.status_code not in (200, 201, 204):
        print(f"journal_lines insert: {r.status_code} {r.text[:200]}")
        _rollback_entry(entry_id)
        return None

    # 3. Balance verification via RPC
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/rpc/check_entry_balanced",
            headers=_sb_headers(),
            json={"p_entry_id": entry_id},
            timeout=10,
        )
    except Exception as e:
        print(f"balance check error: {e}")
        _rollback_entry(entry_id)
        return None
    if r.status_code not in (200, 204):
        print(f"balance check failed: {r.status_code} {r.text[:200]}")
        _rollback_entry(entry_id)
        return None

    print(f"journal entry {entry_id} saved with {len(lines)} lines")
    return entry_id


def _rollback_entry(entry_id: str) -> None:
    """Delete a journal entry (lines cascade)."""
    try:
        requests.delete(
            f"{SUPABASE_URL}/rest/v1/journal_entries?id=eq.{entry_id}",
            headers=_sb_headers(),
            timeout=10,
        )
        print(f"rolled back entry {entry_id}")
    except Exception as e:
        print(f"rollback error for {entry_id}: {e}")


def _save_to_sheets(data: dict) -> bool:
    """Legacy dual-write to the Google Apps Script endpoint.
    Skipped entirely for transfer commands (/raccolgo, /verso)."""
    if not SHEETS_URL:
        return False
    try:
        payload = dict(data)
        payload["data"] = datetime.now().strftime("%Y-%m-%d")
        response = requests.post(SHEETS_URL, json=payload, timeout=10)
        ok = response.text == "OK"
        print(f"SHEETS: {'✅ saved' if ok else f'❌ {response.text[:80]}'}")
        return ok
    except Exception as e:
        print(f"SHEETS: ❌ {e}")
        return False


# ============================================================
# Claude call
# ============================================================
async def ask_claude(user_message: str) -> str:
    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 300,
                "system": [
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                "messages": [{"role": "user", "content": user_message}],
            },
            timeout=30,
        )
        data = response.json()
        if "error" in data:
            return f"❌ Errore API: {data['error'].get('message', 'sconosciuto')}"
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


# ============================================================
# Helpers for journal lines
# ============================================================
def _build_economic_lines(
    tipo: str,
    cassa_account: str,
    economic_account: str,
    importo_eur,
    importo_le,
) -> list:
    """Build the 2 balanced lines for a guide/proprieta income/expense event.

    entrata (incasso):  dare cassa_xxx     /  avere ricavi_xxx
    uscita  (spesa):    avere cassa_xxx    /  dare  costi_xxx
    """
    def amount(v):
        try:
            return float(v) if v not in (None, "", "null") else 0
        except (TypeError, ValueError):
            return 0

    eur = amount(importo_eur)
    le = amount(importo_le)
    lines = []

    for amt, currency in ((eur, "EUR"), (le, "EGP")):
        if amt <= 0:
            continue
        if tipo == "entrata":
            lines.append({
                "account_code": cassa_account,
                "dare": amt, "avere": 0, "currency": currency,
            })
            lines.append({
                "account_code": economic_account,
                "dare": 0, "avere": amt, "currency": currency,
            })
        else:  # uscita
            lines.append({
                "account_code": economic_account,
                "dare": amt, "avere": 0, "currency": currency,
            })
            lines.append({
                "account_code": cassa_account,
                "dare": 0, "avere": amt, "currency": currency,
            })
    return lines


# ============================================================
# Handlers
# ============================================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle plain text messages from guides/proprieta (not slash commands)."""
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

    # 1. Always register/refresh the sender
    upsert_telegram_user(user)

    # 2. Check if sender is mapped
    tg_user = get_telegram_user(user.id)
    if not tg_user or not tg_user.get("account_code"):
        await update.message.reply_text(
            "⏳ Ciao! Ti ho registrato. Prima che possa scrivere transazioni "
            "per te, Omar deve associarti a un conto. Scrivimi di nuovo "
            "quando ti conferma che sei pronto. 👍"
        )
        return

    role = tg_user.get("role")

    # 3. If contabile: redirect to slash commands
    if role == "contabile":
        await update.message.reply_text(
            "👔 Come contabile usi comandi diversi:\n\n"
            "`/raccolgo 200 saif` — ricevi da una guida\n"
            "`/verso 2000 omar` — consegni alla proprieta",
            parse_mode="Markdown",
        )
        return

    # 4. Guide / proprieta: parse with Claude
    await context.bot.send_chat_action(chat_id=chat.id, action="typing")
    response = await ask_claude(text)

    if not response.startswith("TRANSACTION:"):
        await update.message.reply_text(response)
        return

    try:
        tx = json.loads(response.replace("TRANSACTION:", "").strip())
    except Exception as e:
        await update.message.reply_text(f"❌ Errore parsing: {e}")
        return

    tipo = tx.get("tipo")
    account_code = tx.get("account_code") or (
        "ricavi_altri" if tipo == "entrata" else "costi_altri"
    )
    descrizione = tx.get("descrizione", "")

    # 5. Build & insert journal entry
    lines = _build_economic_lines(
        tipo=tipo,
        cassa_account=tg_user["account_code"],
        economic_account=account_code,
        importo_eur=tx.get("importo_eur"),
        importo_le=tx.get("importo_le"),
    )
    if not lines:
        await update.message.reply_text("❌ Nessun importo valido nel messaggio.")
        return

    entry_id = insert_journal_entry(
        description=descrizione,
        source="telegram",
        telegram_user_id=user.id,
        lines=lines,
    )
    if not entry_id:
        await update.message.reply_text(
            "❌ Errore nel salvare su Supabase. Riprova o contatta Omar."
        )
        return

    # 6. Mirror to Google Sheet (only for real economic events, not transfers)
    _save_to_sheets({
        "guida": (user.first_name or "")[:8],
        "tipo": tipo,
        "importo_eur": tx.get("importo_eur", ""),
        "importo_le": tx.get("importo_le", ""),
        "descrizione": descrizione,
    })

    # 7. Confirm to the user
    emoji = "💚" if tipo == "entrata" else "🔴"
    eur = tx.get("importo_eur", "")
    le = tx.get("importo_le", "")
    if eur and str(eur) != "":
        importo_str = f"€{eur}"
    else:
        importo_str = f"{le} LE"

    # No parse_mode — account_code like "ricavi_tour" has an underscore
    # that Markdown would interpret as italic markers → BadRequest.
    reply = (
        f"{emoji} Registrato nel giornale!\n\n"
        f"📝 {descrizione}\n"
        f"💶 {importo_str}\n"
        f"🏷️ {account_code}\n"
        f"👤 {tg_user.get('display_name', '')}\n"
        f"📅 {datetime.now().strftime('%d/%m/%Y')}"
    )
    await update.message.reply_text(reply)


# ============================================================
# Slash commands — contabile only
# ============================================================
async def _require_contabile(update: Update):
    """Upsert the user and return their telegram_users row IFF they're contabile.
    On any rejection, replies to the user and returns None."""
    upsert_telegram_user(update.effective_user)
    tg_user = get_telegram_user(update.effective_user.id)

    if not tg_user or not tg_user.get("account_code"):
        await update.message.reply_text(
            "⏳ Ti ho registrato ma Omar deve ancora associarti a un conto."
        )
        return None

    if tg_user.get("role") != "contabile":
        await update.message.reply_text(
            "🚫 Solo il contabile può usare questo comando."
        )
        return None

    return tg_user


async def cmd_raccolgo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/raccolgo <importo> <guida>  — es. /raccolgo 200 saif

    dare  cassa_contabile       <importo>
    avere cassa_guida_<guida>   <importo>
    """
    tg_user = await _require_contabile(update)
    if not tg_user:
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "ℹ️ Uso: `/raccolgo <importo> <nome_guida>`\n"
            "Es: `/raccolgo 200 saif`",
            parse_mode="Markdown",
        )
        return

    try:
        importo = float(args[0].replace(",", "."))
    except ValueError:
        await update.message.reply_text(f"❌ '{args[0]}' non è un numero valido.")
        return
    if importo <= 0:
        await update.message.reply_text("❌ L'importo deve essere maggiore di zero.")
        return

    guida_name = " ".join(args[1:]).strip()
    guida = find_guida_by_name(guida_name)
    if not guida:
        await update.message.reply_text(
            f"❌ Guida '{guida_name}' non trovata.\n"
            f"Deve prima scrivere un messaggio al bot e essere registrata da Omar."
        )
        return
    if not guida.get("account_code"):
        await update.message.reply_text(
            f"❌ {guida['display_name']} è registrata ma non ha ancora un conto assegnato."
        )
        return

    entry_id = insert_journal_entry(
        description=f"Raccolta da {guida['display_name']}",
        source="telegram",
        telegram_user_id=update.effective_user.id,
        lines=[
            {"account_code": "cassa_contabile",
             "dare": importo, "avere": 0, "currency": "EUR"},
            {"account_code": guida["account_code"],
             "dare": 0, "avere": importo, "currency": "EUR"},
        ],
    )
    if not entry_id:
        await update.message.reply_text("❌ Errore nel registrare. Riprova.")
        return

    # No parse_mode — literal "cassa_contabile" contains an underscore
    # that breaks Markdown entity parsing.
    await update.message.reply_text(
        f"✅ Raccolto €{importo:.2f} da {guida['display_name']}\n\n"
        f"I soldi sono ora in cassa del contabile."
    )


async def cmd_verso(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/verso <importo> <destinazione>  — es. /verso 2000 omar | /verso 500 banca

    dare  <destinazione>    <importo>
    avere cassa_contabile   <importo>
    """
    tg_user = await _require_contabile(update)
    if not tg_user:
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "ℹ️ Uso: `/verso <importo> <destinazione>`\n"
            "Destinazioni: `omar` (o `proprieta`), `banca`\n"
            "Es: `/verso 2000 omar`",
            parse_mode="Markdown",
        )
        return

    try:
        importo = float(args[0].replace(",", "."))
    except ValueError:
        await update.message.reply_text(f"❌ '{args[0]}' non è un numero valido.")
        return
    if importo <= 0:
        await update.message.reply_text("❌ L'importo deve essere maggiore di zero.")
        return

    dest_raw = args[1].strip().lower()
    dest_map = {
        "omar": "proprieta",
        "proprieta": "proprieta",
        "proprietà": "proprieta",
        "banca": "banca",
        "bank": "banca",
    }
    dest_account = dest_map.get(dest_raw)
    if not dest_account:
        # No parse_mode — user input (dest_raw) could contain underscores.
        await update.message.reply_text(
            f"❌ Destinazione '{dest_raw}' non valida.\n"
            f"Usa: omar, proprieta, o banca."
        )
        return

    entry_id = insert_journal_entry(
        description=f"Versamento a {dest_account}",
        source="telegram",
        telegram_user_id=update.effective_user.id,
        lines=[
            {"account_code": dest_account,
             "dare": importo, "avere": 0, "currency": "EUR"},
            {"account_code": "cassa_contabile",
             "dare": 0, "avere": importo, "currency": "EUR"},
        ],
    )
    if not entry_id:
        await update.message.reply_text("❌ Errore nel registrare. Riprova.")
        return

    await update.message.reply_text(
        f"✅ *Versati €{importo:.2f} a {dest_account}*\n\n"
        f"Cassa contabile aggiornata.",
        parse_mode="Markdown",
    )


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/whoami — debug: shows how Omar/bot see this user."""
    upsert_telegram_user(update.effective_user)
    tg_user = get_telegram_user(update.effective_user.id)
    if not tg_user:
        await update.message.reply_text("❌ Non ti trovo nel database.")
        return
    # NOTE: no parse_mode — account codes and "user_id" contain underscores
    # which Telegram's Markdown parser interprets as italic markers and fails
    # with "Can't parse entities". Plain text is the safe choice here.
    await update.message.reply_text(
        f"🆔 ID utente: {tg_user['telegram_user_id']}\n"
        f"👤 nome: {tg_user.get('display_name') or '—'}\n"
        f"@username: {tg_user.get('username') or '—'}\n"
        f"🎭 ruolo: {tg_user.get('role') or '(non assegnato)'}\n"
        f"🏷️ conto: {tg_user.get('account_code') or '(non assegnato)'}"
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_telegram_user(update.effective_user)
    await update.message.reply_text(
        "👋 Ciao! Sono *Athos*, contabile AI di Buongiorno Egitto.\n\n"
        "✏️ *Guide — registrare un evento:*\n"
        "`+200 tour piramidi` → incasso\n"
        "`-50 cammello` → spesa\n"
        "`-1000 LE biglietto museo` → spesa in lire\n\n"
        "👔 *Contabile — trasferimenti:*\n"
        "`/raccolgo 200 saif` → ricevi da guida\n"
        "`/verso 2000 omar` → consegni a proprieta\n\n"
        "🔎 `/whoami` per vedere come ti vedo io.",
        parse_mode="Markdown",
    )


# ============================================================
# Main
# ============================================================
def main():
    print("🚀 Athos Bot (double-entry) avviato...")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("raccolgo", cmd_raccolgo))
    app.add_handler(CommandHandler("verso", cmd_verso))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()


if __name__ == "__main__":
    main()
