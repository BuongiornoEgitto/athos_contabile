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
import re
import json
import logging
import requests
from datetime import datetime, timedelta
from telegram import (
    Update,
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeAllGroupChats,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
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
# Constants — Anthropic API + timeouts (estratti per leggibilita')
# ============================================================
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-4-6"
REQUEST_TIMEOUT_SECONDS = 30
SHEETS_TIMEOUT_SECONDS = 10

# ============================================================
# Logging — proper structured logging invece di print sparsi
# ============================================================
# Mantenuti i print() esistenti per compatibilita' con i log Railway,
# ma il logger e' disponibile per le nuove funzioni (token usage,
# errori non-fatali). Livello INFO: vediamo flussi normali + warnings.
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("athos")


def validate_environment() -> None:
    """Fail-fast se mancano env vars critiche. Chiamato in cima a main().

    Senza questo, il bot partiva e poi crashava al primo messaggio con
    errori difficili da debuggare ("None has no attribute..."). Meglio
    morire subito con un messaggio chiaro nei log Railway.

    SHEETS_URL e' opzionale (legacy backup, puo' restare vuoto).
    """
    required = {
        "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
        "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_SERVICE_KEY": SUPABASE_SERVICE_KEY,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            f"❌ Variabili ambiente mancanti su Railway: {', '.join(missing)}. "
            f"Configurale in Railway → progetto athos_contabile → Variables, "
            f"poi rideploya."
        )

# ============================================================
# Multi-transaction preview state
# ============================================================
# Pending previews keyed by telegram_user_id. Solo UNA preview attiva per
# utente alla volta — se ne arriva una nuova, la vecchia viene sostituita.
# Ogni voce contiene: transactions (list[dict]), created_at (datetime),
# cassa_account (str catturato al momento della preview).
_pending_previews: dict[int, dict] = {}
PREVIEW_TIMEOUT_MIN = 5

# Soglie "sospetto" per l'anteprima
SUSPECT_EUR_THRESHOLD = 1900
SUSPECT_LE_THRESHOLD = 60_000
FALLBACK_ACCOUNTS = {"costi_altri", "ricavi_escursioni"}

# ============================================================
# Claude system prompt — istruisce Claude su QUANDO chiamare il tool
# register_transaction e su come scegliere account_code/tipo/currency.
# Lo schema vero e proprio dei campi vive nel tool (vedi
# _build_register_transaction_tool); qui mettiamo SOLO le regole di
# business e gli esempi che lo schema da solo non può comunicare.
# ============================================================
SYSTEM_PROMPT = """Sei Athos, agente AI contabile per Buongiorno Egitto (agenzia viaggi Egitto).
Rispondi SEMPRE in italiano e in modo conciso.

COME RISPONDERE:
- Se il messaggio è una transazione (es. "+200 tour piramidi", "-50 LE acqua"),
  CHIAMA il tool register_transaction con i campi corretti. Non rispondere con
  testo libero in quel caso.
- Se il messaggio NON è una transazione (saluto, domanda, conversazione fra
  colleghi che è arrivata al bot per sbaglio), NON chiamare il tool: rispondi
  con un testo breve in italiano, suggerendo il formato corretto, es.
  "Scrivi nel formato +/- importo descrizione".

REGOLE TIPO:
- "+", "in", "incasso", "entrata", "pagato da", "ricevuto da cliente" → tipo "entrata"
- "-", "spesa", "out", "pagamento", "costo", "speso per" → tipo "uscita"

REGOLE CURRENCY:
- Default EUR. Se il messaggio contiene "LE", "L.E.", "lire", "EGP", "egp" →
  currency "EGP". Non convertire mai: scrivi il numero esatto nella currency
  scelta.

REGOLE ACCOUNT_CODE — scegli SEMPRE da uno dei conti elencati nello schema
del tool. Non inventare codici. Linee guida sulla scelta:

RICAVI (quando tipo=entrata):
- ricavi_escursioni → TUTTI gli incassi da clienti per tour ed escursioni
  (piramidi, luxor, assuan, abu simbel, deserto, mare, quad, cammello, ecc.).
  DEFAULT per entrate: usa questo anche quando la descrizione contiene solo
  nome cliente e/o hotel senza specificare l'attività.
- ricavi_commissioni → commissioni da partner, hotel, negozi (solo se la
  descrizione contiene esplicitamente "commissione", "provvigione",
  "commission" o simili).

COSTI (quando tipo=uscita):
- costi_ristoranti → pranzi, cene con clienti, ristoranti
- costi_escursioni → TUTTE le spese sul momento durante un'escursione:
  affitto moto/quad/barche/feluche/motoscafi, cammelli e altri animali,
  MANCE a driver/barcaioli/cammellieri/motoristi, attrezzatura piccola
  (snorkeling, pinne, maschere), acqua/snack per clienti durante il tour,
  piccoli pagamenti al volo al tempio/sito (guide extra, custodi),
  pagamenti a fornitori di escursioni (Shamandura, Shaarawy, ecc.).
- costi_ingressi → biglietti UFFICIALI siti archeologici, musei, templi
- costi_trasporti → benzina, taxi, voli interni, bus, treni, transfer aeroporto
- costi_alloggio → hotel, case, resort per clienti
- costi_guide_esterne → guide occasionali NON del team fisso
- costi_marketing → pubblicita, social, annunci
- costi_telefono → SIM, ricariche, internet, roaming
- costi_stipendi → compensi guide fisse del team
- costi_bancari → fee PayPal, Stripe, bonifici, cambio valuta
- costi_amministrativi → commercialista, licenze, permessi
- costi_ufficio → cancelleria, attrezzatura ufficio, computer
- costi_altri → tutto il resto delle spese (FALLBACK: usa solo se nessun
  altro conto sopra è applicabile, e in quel caso metti confidence "low").

NOTA "commissione" — può essere sia entrata che uscita:
- se tipo=entrata → ricavi_commissioni (es. guida vende foto e prende %)
- se tipo=uscita → costi_escursioni (mance/commissioni ai driver durante tour)

REGOLE DESCRIZIONE:
- Tutto il testo dopo segno/cifra/valuta va in "descrizione".
- Se è inferiore a 2 caratteri, espandi con un placeholder ragionevole
  ("transazione senza descrizione") e metti confidence "low".

REGOLE CONFIDENCE:
- "high" → classificazione netta: account_code ovvio, descrizione chiara,
  tipo non ambiguo (es. "+200 tour piramidi", "-50 LE biglietto valle re").
- "low" → ALMENO UNA delle seguenti:
  • la descrizione è ambigua o contiene parole sconosciute
  • hai dovuto usare un account fallback (ricavi_escursioni come default
    senza indicazione esplicita, costi_altri come ultima spiaggia)
  • il segno (+/-) è in conflitto con l'uso tipico della parola
    (es. "+1 acqua" — acqua di solito è un costo, non un ricavo)

ESEMPI (input → input al tool register_transaction):
"+200 tour piramidi" → tipo=entrata currency=EUR importo=200 descrizione="tour piramidi" account_code=ricavi_escursioni confidence=high
"+150 escursione deserto" → tipo=entrata currency=EUR importo=150 descrizione="escursione deserto" account_code=ricavi_escursioni confidence=high
"+300 Mario Rossi Hotel Sunrise" → tipo=entrata currency=EUR importo=300 descrizione="Mario Rossi Hotel Sunrise" account_code=ricavi_escursioni confidence=high
"-50 pranzo clienti" → tipo=uscita currency=EUR importo=50 descrizione="pranzo clienti" account_code=costi_ristoranti confidence=high
"-300 cammello" → tipo=uscita currency=EUR importo=300 descrizione="cammello" account_code=costi_escursioni confidence=high
"-50 mancia motorista" → tipo=uscita currency=EUR importo=50 descrizione="mancia motorista" account_code=costi_escursioni confidence=high
"-100 LE snorkeling" → tipo=uscita currency=EGP importo=100 descrizione="snorkeling" account_code=costi_escursioni confidence=high
"-1000 LE guida canyon" → tipo=uscita currency=EGP importo=1000 descrizione="guida canyon" account_code=costi_guide_esterne confidence=high
"-500 LE biglietto valle re" → tipo=uscita currency=EGP importo=500 descrizione="biglietto valle re" account_code=costi_ingressi confidence=high
"+100 commissione foto" → tipo=entrata currency=EUR importo=100 descrizione="commissione foto" account_code=ricavi_commissioni confidence=high
"-30 commissione driver" → tipo=uscita currency=EUR importo=30 descrizione="commissione driver" account_code=costi_escursioni confidence=high
"+90 Davide domina" → tipo=entrata currency=EUR importo=90 descrizione="Davide domina" account_code=ricavi_escursioni confidence=low
"+1 acqua" → tipo=entrata currency=EUR importo=1 descrizione="acqua" account_code=ricavi_escursioni confidence=low
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


def find_user_by_name(name: str) -> dict | None:
    """Case-insensitive lookup of a telegram_user by display_name (any role).
    Used by /raccolgo and /verso when the user types the name directly."""
    if not _sb_configured() or not name:
        return None
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/telegram_users"
            f"?display_name=ilike.{name}&account_code=not.is.null&select=*",
            headers=_sb_headers(),
            timeout=10,
        )
        if r.status_code == 200:
            rows = r.json()
            return rows[0] if rows else None
        print(f"find_user_by_name: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"find_user_by_name error: {e}")
    return None


def fetch_users_with_account(exclude_account: str | None = None) -> list[dict]:
    """All telegram_users (guida/manager/contabile/proprieta) with an
    account_code assigned, alphabetically. Optionally excludes a single
    account_code (typically the caller, who can't /raccolgo or /verso to self).
    """
    if not _sb_configured():
        return []
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/telegram_users"
            f"?account_code=not.is.null"
            f"&role=in.(guida,manager,contabile,proprieta)"
            f"&select=display_name,account_code,role"
            f"&order=display_name.asc",
            headers=_sb_headers(),
            timeout=10,
        )
        if r.status_code == 200:
            rows = r.json() or []
            if exclude_account:
                rows = [u for u in rows if u.get("account_code") != exclude_account]
            return rows
        print(f"fetch_users_with_account: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"fetch_users_with_account error: {e}")
    return []


def fetch_active_economic_accounts() -> list[str]:
    """Ritorna i `code` dei conti attivi di tipo ricavo o costo, in ordine
    alfabetico. Usato per popolare l'enum dinamico di `account_code` nel
    tool register_transaction (cosi' Claude non puo' inventare codici).

    Crash se Supabase non risponde o ritorna lista vuota — un enum vuoto
    farebbe rifiutare a Claude qualsiasi tool call, e un enum stale e'
    peggio di un crash chiaro all'avvio.
    """
    if not _sb_configured():
        raise RuntimeError(
            "Supabase non configurato — impossibile caricare il piano dei conti"
        )
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/accounts"
        f"?type=in.(ricavo,costo)&active=eq.true"
        f"&select=code&order=code.asc",
        headers=_sb_headers(),
        timeout=10,
    )
    r.raise_for_status()
    rows = r.json() or []
    codes = [row["code"] for row in rows if row.get("code")]
    if not codes:
        raise RuntimeError(
            "Piano dei conti vuoto (nessun ricavo/costo attivo) — "
            "applica le migrazioni Supabase prima di avviare il bot"
        )
    return codes


def find_user_by_account_code(account_code: str) -> dict | None:
    """Lookup any telegram_user by account_code (used by inline-button callbacks)."""
    if not _sb_configured() or not account_code:
        return None
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/telegram_users"
            f"?account_code=eq.{account_code}&select=*",
            headers=_sb_headers(),
            timeout=10,
        )
        if r.status_code == 200:
            rows = r.json()
            return rows[0] if rows else None
    except Exception as e:
        print(f"find_user_by_account_code error: {e}")
    return None


def _user_button_label(user: dict) -> str:
    """Inline-button label with a role-emoji prefix to distinguish at a glance."""
    role = user.get("role")
    name = user.get("display_name") or "?"
    emoji = {
        "proprieta": "🏠",
        "contabile": "🧮",
        "manager":   "👔",
        "guida":     "🧭",
    }.get(role, "👤")
    return f"{emoji} {name}"


def insert_journal_entry(
    description: str,
    source: str,
    telegram_user_id: int,
    lines: list,
    entry_date: str | None = None,
    customer_name: str | None = None,
    supplier_code: str | None = None,
    payment_reference: str | None = None,
    pharos_match_status: str | None = None,
    pharos_booking_code: str | None = None,
) -> str | None:
    """Create a balanced journal entry with N lines via atomic Supabase RPC.

    lines: list of {account_code, dare, avere, currency}. Sum(dare)==sum(avere)
    per currency must hold. The database RPC creates header + lines in one
    transaction and rejects invalid/header-only entries (migration 029).
    """
    if not _sb_configured():
        return None

    payload = {
        "p_description": description,
        "p_lines": lines,
        "p_telegram_user_id": (
            int(telegram_user_id) if telegram_user_id is not None else None
        ),
        "p_source": source,
        "p_entry_date": entry_date or datetime.now().strftime("%Y-%m-%d"),
        "p_external_id": None,
        "p_customer_name": customer_name,
    }
    if supplier_code is not None:
        payload["p_supplier_code"] = supplier_code
    if payment_reference is not None:
        payload["p_payment_reference"] = payment_reference
    if pharos_match_status is not None:
        payload["p_pharos_match_status"] = pharos_match_status
    if pharos_booking_code is not None:
        payload["p_pharos_booking_code"] = pharos_booking_code
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/rpc/create_balanced_journal_entry",
            headers=_sb_headers(),
            json=payload,
            timeout=15,
        )
    except Exception as e:
        print(f"RPC create_balanced_journal_entry error: {e}")
        return None

    if r.status_code not in (200, 204):
        print(f"RPC create_balanced_journal_entry: {r.status_code} {r.text[:300]}")
        return None

    try:
        result = r.json() if r.text else {}
    except Exception as e:
        print(f"RPC create_balanced_journal_entry non-JSON response: {e}")
        return None

    if not result.get("ok", False):
        print(f"RPC create_balanced_journal_entry failed: {result.get('msg', 'RPC fallita.')}")
        return None

    entry_id = result.get("entry_id")
    if not entry_id:
        print("RPC create_balanced_journal_entry missing entry_id")
        return None

    print(f"journal entry {entry_id} saved with {len(lines)} lines")
    return entry_id


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
# ============================================================
# Tool schema — register_transaction
# ============================================================
# Lo schema e' costruito a runtime perche' l'enum di `account_code` viene
# popolato dai conti veri presenti in Supabase (vedi
# fetch_active_economic_accounts). Cosi' Claude non puo' inventare codici
# inesistenti — il modello vede SOLO i conti che esistono nel DB.
#
# Cache process-wide: il tool e' costruito una volta a startup
# (init_claude_tool) e riusato per ogni messaggio. Per riflettere nuovi
# conti aggiunti via SQL serve un riavvio del bot.
_REGISTER_TRANSACTION_TOOL: dict | None = None


def _build_register_transaction_tool(active_accounts: list[str]) -> dict:
    """Costruisce lo schema del tool. active_accounts deve essere non vuoto
    (validato a monte da fetch_active_economic_accounts)."""
    return {
        "name": "register_transaction",
        "description": (
            "Register a single accounting transaction in the double-entry "
            "journal. Use ONLY when the user message describes one transaction "
            "with amount + description (e.g., '+200 tour piramidi', '-50 LE "
            "acqua'). Do NOT use for greetings, questions, conversation, or "
            "ambiguous text without a clear amount."
        ),
        "input_schema": {
            "type": "object",
            "required": [
                "tipo", "currency", "importo",
                "account_code", "descrizione", "confidence",
            ],
            "properties": {
                "tipo": {
                    "type": "string",
                    "enum": ["entrata", "uscita"],
                    "description": (
                        "entrata = incasso/ricavo (+ sign, 'incasso', "
                        "'ricevuto'); uscita = spesa/costo (- sign, 'spesa', "
                        "'pagato', 'speso')"
                    ),
                },
                "currency": {
                    "type": "string",
                    "enum": ["EUR", "EGP"],
                    "description": (
                        "EUR by default. EGP if the message contains "
                        "'LE', 'L.E.', 'lire', 'EGP', or 'egp'."
                    ),
                },
                "importo": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "description": (
                        "Numeric amount in the chosen currency, as written by "
                        "the user. Example: 200, 1500.5, 5000. Never convert "
                        "between currencies."
                    ),
                },
                "account_code": {
                    "type": "string",
                    "enum": active_accounts,
                    "description": (
                        "Economic account code. Choose the most specific match "
                        "from the list. For unclear income use "
                        "'ricavi_escursioni' and set confidence='low'. For "
                        "unclear expense use 'costi_altri' (last resort) and "
                        "set confidence='low'. See SYSTEM PROMPT for full "
                        "category descriptions."
                    ),
                },
                "descrizione": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": 200,
                    "description": (
                        "Short Italian description of the transaction "
                        "(e.g., 'tour piramidi', 'pranzo clienti', "
                        "'mancia driver'). Pass through the user's words."
                    ),
                },
                "confidence": {
                    "type": "string",
                    "enum": ["high", "low"],
                    "description": (
                        "high = unambiguous classification. low = at least "
                        "one of: ambiguous description, fallback account "
                        "(ricavi_escursioni without explicit hint, or "
                        "costi_altri), sign conflicts with typical use of "
                        "the word (e.g., '+1 acqua' — acqua is usually a "
                        "cost, not income)."
                    ),
                },
            },
        },
    }


def init_claude_tool() -> None:
    """Idempotente: fetcha i conti attivi e costruisce il tool schema.
    Chiamato a startup. Crash se Supabase non risponde — meglio non avviare
    che girare con uno schema vuoto/stale."""
    global _REGISTER_TRANSACTION_TOOL
    accounts = fetch_active_economic_accounts()
    _REGISTER_TRANSACTION_TOOL = _build_register_transaction_tool(accounts)
    logger.info(
        f"register_transaction tool inizializzato con "
        f"{len(accounts)} conti economici: {accounts}"
    )


# ============================================================
# Claude API — un'unica chiamata che usa il tool register_transaction
# come canale strutturato di output. Per messaggi non-transazione il
# modello risponde con testo libero (vedi system prompt).
# ============================================================
async def ask_claude(user_message: str) -> tuple[str, dict | str]:
    """Chiama Claude. Ritorna una tupla (kind, payload) tipata:
      - ("tx", dict)  → il modello ha chiamato register_transaction; dict
                        contiene i campi tipati validati dallo schema
      - ("msg", str)  → il modello ha risposto testo libero (es. "questo
                        non sembra una transazione") oppure errore API
                        con messaggio user-facing prefissato da ❌

    Usa prompt caching ephemeral sul SYSTEM_PROMPT — dopo la prima
    chiamata, le successive entro 5min leggono il prompt cachato.
    """
    if _REGISTER_TRANSACTION_TOOL is None:
        return "msg", (
            "❌ Bot non inizializzato (tool schema mancante). "
            "Riavvia o contatta Omar."
        )

    try:
        response = requests.post(
            ANTHROPIC_MESSAGES_URL,
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 400,
                "system": [
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                "tools": [_REGISTER_TRANSACTION_TOOL],
                # tool_choice="auto": Claude decide se chiamare il tool
                # (transazione) o rispondere con testo (non-transazione).
                # "force" qui sarebbe pericoloso: registrerebbe ANCHE messaggi
                # non-transazione (es. "+200 ciao a tutti") inquinando il
                # journal con tx spurie.
                "tool_choice": {"type": "auto"},
                "messages": [{"role": "user", "content": user_message}],
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()

        if "error" in data:
            err_msg = data["error"].get("message", "sconosciuto")
            logger.error(f"Anthropic API error: {err_msg}")
            return "msg", f"❌ Errore API: {err_msg}"

        # Log token usage — cache_read > 0 conferma che il caching funziona
        usage = data.get("usage", {})
        logger.info(
            f"tokens — input: {usage.get('input_tokens', 0)}, "
            f"cache_read: {usage.get('cache_read_input_tokens', 0)}, "
            f"cache_create: {usage.get('cache_creation_input_tokens', 0)}, "
            f"output: {usage.get('output_tokens', 0)}, "
            f"stop_reason: {data.get('stop_reason')}"
        )

        content = data.get("content") or []

        # Cerca il primo blocco tool_use con name=register_transaction.
        # Se presente → estraiamo i campi tipati. Se non c'è, scaliamo al
        # primo blocco di tipo "text" come messaggio user-facing.
        for block in content:
            if (
                block.get("type") == "tool_use"
                and block.get("name") == "register_transaction"
            ):
                tx = block.get("input") or {}
                # L'API garantisce che `input` rispetta lo schema dichiarato
                # (enum, required, types). Logghiamo comunque per audit.
                logger.info(
                    f"tool_use register_transaction: {tx}"
                )
                return "tx", tx

        for block in content:
            if block.get("type") == "text":
                return "msg", block.get("text", "").strip()

        logger.error(f"Risposta Anthropic senza tool_use né text: {str(data)[:300]}")
        return "msg", "❌ Risposta AI non valida. Riprova."

    except requests.HTTPError as e:
        logger.error(f"Anthropic HTTP error {e.response.status_code}: {e.response.text[:200]}")
        return "msg", f"❌ Errore HTTP AI ({e.response.status_code}). Riprova fra poco."
    except requests.RequestException as e:
        logger.exception(f"Anthropic request error: {e}")
        return "msg", f"❌ Errore connessione AI: {e}"
    except (ValueError, KeyError) as e:
        logger.exception(f"Anthropic response parse error: {e}")
        return "msg", "❌ Risposta AI non valida. Riprova."


# ============================================================
# Helpers for journal lines
# ============================================================
def _build_economic_lines(
    tipo: str,
    cassa_account: str,
    economic_account: str,
    importo,
    currency: str,
) -> list:
    """Build the 2 balanced lines for a guide/proprieta income/expense event.

    entrata (incasso):  dare cassa_xxx     /  avere ricavi_xxx
    uscita  (spesa):    avere cassa_xxx    /  dare  costi_xxx

    Una sola currency per tx (semplificazione introdotta col refactor a
    tool use 2026-05-10): un messaggio = un importo in una currency.
    Per scrivere in EUR e EGP nello stesso evento serve /cambia, non
    questa via.
    """
    try:
        amt = float(importo) if importo not in (None, "", "null") else 0.0
    except (TypeError, ValueError):
        amt = 0.0
    if amt <= 0:
        return []
    if currency not in ("EUR", "EGP"):
        return []

    if tipo == "entrata":
        return [
            {"account_code": cassa_account,
             "dare": amt, "avere": 0, "currency": currency},
            {"account_code": economic_account,
             "dare": 0, "avere": amt, "currency": currency},
        ]
    else:  # uscita
        return [
            {"account_code": economic_account,
             "dare": amt, "avere": 0, "currency": currency},
            {"account_code": cassa_account,
             "dare": 0, "avere": amt, "currency": currency},
        ]


# ============================================================
# Multi-transaction parsing & preview helpers
# ============================================================
# Token che marca l'inizio di una transazione: +/- seguiti da numero, oppure
# parole chiave (entrata/uscita/incasso/spesa/in/out/pagato/ricevuto/speso/
# costo/pagamento) seguite — anche con altre parole in mezzo — da un numero.
# Allineato al system prompt di Claude (vedi righe 77-78). Il \b nella regex
# evita falsi positivi tipo "incoming" (in), "output" (out), "pagatoltre" (pagato).
# Usato sia per il conteggio (detection) sia per lo split. Case-insensitive.
_KEYWORD_TOKENS = r"entrata|uscita|incasso|spesa|in|out|pagato|ricevuto|speso|costo|pagamento"
# Per la detection accettiamo tokens "ragionevoli": +/- attaccati a un numero,
# oppure una keyword che precede un numero entro pochi caratteri.
_DETECT_PATTERN = re.compile(
    rf"""(?ix)
        (?:^|\s)
        (?:
            [+\-]\s*\d                          # +200 / - 50
            |
            (?:{_KEYWORD_TOKENS})\b [^\n+\-]{{0,30}}? \d   # entrata 100 ...
        )
    """,
)
# Per lo split troviamo le posizioni di inizio di ogni transazione e ritagliamo
# fra una posizione e la successiva.
_SPLIT_PATTERN = re.compile(
    rf"""(?ix)
        (?:^|(?<=\s))
        (?:
            [+\-]\s*\d
            |
            (?:{_KEYWORD_TOKENS})\b [^\n+\-]{{0,30}}? \d
        )
    """,
)


def _count_transaction_starts(text: str) -> int:
    """Conta quanti 'inizi di transazione' ci sono nel messaggio."""
    return len(_DETECT_PATTERN.findall(text or ""))


def _split_transactions(text: str) -> list[str]:
    """Spezza il messaggio in N pezzi, uno per transazione.

    Trova ogni 'inizio' (segno+numero o keyword+numero) e ritaglia fra una
    occorrenza e la successiva. Se non trova nulla, restituisce l'intero testo
    come singolo pezzo (così il chiamante può comunque inoltrarlo a Claude).
    """
    if not text:
        return []
    starts = [m.start() for m in _SPLIT_PATTERN.finditer(text)]
    if not starts:
        return [text.strip()]
    pieces = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        chunk = text[start:end].strip()
        if chunk:
            pieces.append(chunk)
    return pieces


def _amt(v) -> float:
    try:
        return float(v) if v not in (None, "", "null") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _is_high_amount(tx: dict) -> tuple[bool, str]:
    """Solo trigger 'importo elevato' (sanity check su grosse cifre).
    Usato per decidere se mostrare preview su una singola transazione:
    Omar vuole conferma SOLO per importi grossi, non per ogni dubbio
    di classificazione di Claude (vedi richiesta 2026-05-08)."""
    importo = _amt(tx.get("importo"))
    currency = tx.get("currency", "EUR")
    if currency == "EUR" and importo > SUSPECT_EUR_THRESHOLD:
        return True, f"importo elevato (€{importo:.0f})"
    if currency == "EGP" and importo > SUSPECT_LE_THRESHOLD:
        return True, f"importo elevato ({importo:.0f} LE)"
    return False, ""


def _is_suspect(tx: dict) -> tuple[bool, str]:
    """Heuristic: è una transazione 'sospetta'? Restituisce (bool, motivo).

    Usato nel preview multi-transazione per marcare le righe con ⚠️.
    Per singola transazione vedi _is_high_amount (Omar vuole meno friction).

    Triggers:
      - Claude ha messo confidence="low" (incertezza esplicita)
      - importo > soglia (1900 EUR o 60000 LE)
      - account_code è un fallback E descrizione cortissima — segnale che
        Claude ha riempito con un default senza capire bene
    """
    confidence = (tx.get("confidence") or "").strip().lower()
    if confidence == "low":
        return True, "Claude segnala incertezza"

    high, reason = _is_high_amount(tx)
    if high:
        return True, reason

    descrizione = (tx.get("descrizione") or "").strip()
    account_code = tx.get("account_code") or ""
    if account_code in FALLBACK_ACCOUNTS and len(descrizione) < 4:
        snippet = descrizione if descrizione else "(vuota)"
        return True, f'"{snippet}" poco chiaro'

    return False, ""


def _format_amount(tx: dict) -> str:
    """Formatta l'importo in stile '+90 EUR' / '-1000 LE'."""
    tipo = tx.get("tipo")
    sign = "+" if tipo == "entrata" else "-"
    importo = _amt(tx.get("importo"))
    currency = tx.get("currency", "EUR")
    if importo <= 0:
        return f"{sign}? "
    label = "EUR" if currency == "EUR" else "LE"
    return f"{sign}{int(importo)} {label}"


def _format_preview(transactions: list[dict]) -> str:
    """Costruisce il messaggio di anteprima multi-transazione (plain text)."""
    n = len(transactions)
    lines = [f"📝 Ho trovato {n} transazion{'i' if n != 1 else 'e'}. Registro?", ""]
    for i, tx in enumerate(transactions, start=1):
        suspect, reason = _is_suspect(tx)
        flag = "⚠️" if suspect else "✅"
        amount = _format_amount(tx)
        descr = (tx.get("descrizione") or "").strip() or "(senza descrizione)"
        account = tx.get("account_code") or "?"
        # Padding leggero per leggibilità — non rigido, Telegram usa font proporzionale
        lines.append(f"{i}. {flag}  {amount}  {descr}  → {account}")
        if suspect and reason:
            lines.append(f"     ↑ {reason}")
    lines.append("")
    lines.append("Rispondi:")
    lines.append('• "ok" → registro tutte')
    lines.append('• "no" → annullo')
    lines.append('• "solo 1,2,3" → registro solo quelle (numeri separati da virgola)')
    return "\n".join(lines)


def _write_one_transaction(
    tx: dict,
    cassa_account: str,
    telegram_user_id: int,
    user_first_name: str,
) -> tuple[str | None, str]:
    """Scrive una singola transazione: ritorna (entry_id|None, descrizione)."""
    tipo = tx.get("tipo")
    account_code = tx.get("account_code") or (
        "ricavi_escursioni" if tipo == "entrata" else "costi_altri"
    )
    descrizione = tx.get("descrizione", "")
    currency = tx.get("currency", "EUR")
    importo = _amt(tx.get("importo"))

    lines = _build_economic_lines(
        tipo=tipo,
        cassa_account=cassa_account,
        economic_account=account_code,
        importo=importo,
        currency=currency,
    )
    if not lines:
        return None, descrizione

    entry_id = insert_journal_entry(
        description=descrizione,
        source="telegram",
        telegram_user_id=telegram_user_id,
        lines=lines,
    )
    if entry_id:
        # Mirror to Sheets — il GAS legacy si aspetta importo_eur/importo_le
        # come campi separati. Manteniamo quel contratto qui anche se il
        # nostro modello interno e' (importo, currency).
        _save_to_sheets({
            "guida": (user_first_name or "")[:8],
            "tipo": tipo,
            "importo_eur": importo if currency == "EUR" else "",
            "importo_le": importo if currency == "EGP" else "",
            "descrizione": descrizione,
        })
    return entry_id, descrizione


def _format_single_confirmation(
    tx: dict,
    display_name: str,
) -> str:
    """Riproduce il messaggio di conferma usato dal flusso single."""
    tipo = tx.get("tipo")
    emoji = "💚" if tipo == "entrata" else "🔴"
    importo = _amt(tx.get("importo"))
    currency = tx.get("currency", "EUR")
    importo_str = (
        f"€{importo:g}" if currency == "EUR" else f"{importo:g} LE"
    )
    account_code = tx.get("account_code") or (
        "ricavi_escursioni" if tipo == "entrata" else "costi_altri"
    )
    descrizione = tx.get("descrizione", "")
    return (
        f"{emoji} Registrato nel giornale!\n\n"
        f"📝 {descrizione}\n"
        f"💶 {importo_str}\n"
        f"🏷️ {account_code}\n"
        f"👤 {display_name}\n"
        f"📅 {datetime.now().strftime('%d/%m/%Y')}"
    )


def _parse_confirmation(text: str, n_transactions: int) -> tuple[str, list[int] | None]:
    """Interpreta la risposta dell'utente al preview.

    Ritorna:
      ("all", None) → registra tutte
      ("none", None) → annulla
      ("subset", [indici 0-based]) → registra solo quelle
      ("unknown", None) → non capito
    """
    if not text:
        return "unknown", None
    t = text.strip().lower()

    if t in {"ok", "si", "sì", "yes", "conferma", "confermo", "y"}:
        return "all", None
    if t in {"no", "annulla", "cancel", "n"}:
        return "none", None

    # "solo 1,3" / "solo 1, 3" / "1,3" / "1 3" / "solo 2"
    had_solo_prefix = bool(re.match(r"^solo\s+", t))
    cleaned = re.sub(r"^solo\s+", "", t)
    # Accetta separatori virgola/spazio
    parts = re.split(r"[,\s]+", cleaned.strip())
    parts = [p for p in parts if p]
    if parts and all(p.isdigit() for p in parts):
        nums = sorted({int(p) for p in parts})
        # Validità: tutti gli indici devono essere in [1..n]
        if nums and all(1 <= n <= n_transactions for n in nums):
            # Anti-typo: un singolo numero senza "solo" potrebbe essere un
            # errore di battitura. Richiedo "solo N" esplicito quando c'è
            # solo un numero. Per "1,3" o più resta accettato com'è (chiaro
            # che è una scelta esplicita).
            if len(nums) == 1 and not had_solo_prefix and n_transactions > 1:
                return "unknown", None
            return "subset", [n - 1 for n in nums]
    return "unknown", None


def _confirmation_help_text(n: int) -> str:
    return (
        "🤔 Non ho capito. Rispondi con:\n"
        '• "ok" → registro tutte\n'
        '• "no" → annullo\n'
        f'• "solo 1,2" → registro solo quelle (numeri da 1 a {n}, '
        "separati da virgola)"
    )


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

    if not text:
        return

    # Ignore messages that are conversations between team members, not
    # transactions intended for the bot. Two heuristics:
    #   - Message starts with "@" (old guard, kept as belt-and-suspenders)
    #   - Message contains any @mention or text-mention entity (Telegram
    #     tells us explicitly when the user tagged someone)
    # Replies to other users (not to the bot) are also treated as chatter.
    if text.startswith("@"):
        return

    entities = update.message.entities or []
    if any(e.type in ("mention", "text_mention") for e in entities):
        return

    # If this is a reply, only process it if it's a reply to the bot itself
    # (e.g., someone correcting a previous bot message). Replies to other
    # humans are chatter.
    reply_to = update.message.reply_to_message
    if reply_to and reply_to.from_user and not reply_to.from_user.is_bot:
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

    # 3. Role check: qualsiasi utente mappato (guida, contabile, proprieta)
    # puo' usare la sintassi "+X ..." / "-X ..." per registrare ricavi/costi
    # sulla propria cassa. Il conto DARE/AVERE viene da tg_user["account_code"]
    # (cassa_guida_X, cassa_contabile, proprieta). I comandi slash /raccolgo
    # e /verso restano per i trasferimenti fra casse.

    # 4. Pending preview? Se sì, gestiamo conferma/annullo/subset PRIMA di
    # qualsiasi altra logica. Anche un timeout viene gestito qui.
    # `replaced_pending` resta True se l'utente ha "abbandonato" il vecchio
    # preview mandando una nuova transazione invece di rispondere — così il
    # nuovo preview multi può prefissare un avviso.
    replaced_pending = False
    pending = _pending_previews.get(user.id)
    if pending:
        age = datetime.utcnow() - pending["created_at"]
        if age > timedelta(minutes=PREVIEW_TIMEOUT_MIN):
            # Timeout: drop e processa il messaggio corrente normalmente
            _pending_previews.pop(user.id, None)
            await update.message.reply_text(
                "⏰ Il preview precedente è scaduto. Rimanda il messaggio "
                "per vedere la nuova anteprima."
            )
            return
        # Preview ancora valida → tenta di interpretare come risposta
        action, indices = _parse_confirmation(text, len(pending["transactions"]))
        if action == "unknown":
            # Sblocco anti-lockout: se il messaggio "non risposta" sembra
            # invece una NUOVA transazione (almeno un token shape-tx), buttiamo
            # via il vecchio preview e processiamo il nuovo messaggio. Così
            # l'utente non resta intrappolato finché non scrive "no".
            if _count_transaction_starts(text) >= 1:
                _pending_previews.pop(user.id, None)
                replaced_pending = True
                # Fall through al codice normale sotto (single o multi path)
            else:
                await update.message.reply_text(
                    _confirmation_help_text(len(pending["transactions"]))
                )
                return
        elif action == "none":
            _pending_previews.pop(user.id, None)
            await update.message.reply_text("🚫 Annullato. Niente registrato.")
            return
        elif action in ("all", "subset"):
            # all / subset → scrivi le selezionate
            selected = (
                pending["transactions"]
                if action == "all"
                else [pending["transactions"][i] for i in indices]
            )
            _pending_previews.pop(user.id, None)
            await context.bot.send_chat_action(chat_id=chat.id, action="typing")
            results = []
            for tx in selected:
                entry_id, descr = _write_one_transaction(
                    tx,
                    cassa_account=pending["cassa_account"],
                    telegram_user_id=user.id,
                    user_first_name=user.first_name or "",
                )
                mark = "✅" if entry_id else "❌"
                results.append(f"{mark} {_format_amount(tx)} {descr or '(senza descr)'}")
            n_ok = sum(1 for r in results if r.startswith("✅"))
            header = (
                f"💾 Registrate {n_ok}/{len(selected)} transazioni:\n\n"
                if len(selected) > 1
                else ""
            )
            await update.message.reply_text(header + "\n".join(results))
            return
        # action == "unknown" + tx-shape → fall through al codice sotto
        # (replaced_pending è True, pending è già stato pop dal dict)

    # 5. Guide / proprieta: detect multi-transaction PRIMA di chiamare Claude
    n_starts = _count_transaction_starts(text)

    # Pre-Claude filter: scarta messaggi che NON sembrano transazioni.
    # `_count_transaction_starts` ritorna 0 quando non trova nessun segno
    # +/- attaccato a un numero, ne' una keyword (entrata/uscita/incasso/
    # spesa) vicino a un numero. In quel caso e' inutile (e rischioso)
    # interrogare Claude: a volte "interpretava" creativamente messaggi
    # tipo "ciao Amr" o "domani 3 escursioni" provando a parsarli, e la
    # guida riceveva "Errore parsing..." senza capire perche'.
    # Ora rispondiamo con un messaggio educativo e ci fermiamo subito —
    # zero token sprecati, zero righe spurie nel journal.
    if n_starts == 0:
        await update.message.reply_text(
            "🤔 Questo non sembra una transazione.\n\n"
            "Per registrare un movimento usa il segno + o -:\n"
            "`+200 tour piramidi` → incasso EUR\n"
            "`-50 cammello` → spesa EUR\n"
            "`+500 LE commissione` → incasso lire\n\n"
            "Oppure /whoami per vedere il tuo profilo.",
            parse_mode="Markdown",
        )
        return

    await context.bot.send_chat_action(chat_id=chat.id, action="typing")

    if n_starts >= 2:
        # --- Multi-transaction path ---
        pieces = _split_transactions(text)
        transactions = []
        for piece in pieces:
            kind, payload = await ask_claude(piece)
            if kind == "tx":
                transactions.append(payload)
            else:
                # Claude non ha chiamato il tool su questo pezzo: lo mettiamo
                # comunque in preview con confidence "low" cosi' l'utente
                # decide se tenerlo. Fallback per `tipo`: segno o keyword.
                piece_lower = piece.lstrip().lower()
                if re.match(r"^(\+|entrata|incasso|in\s|ricevuto)", piece_lower):
                    fallback_tipo = "entrata"
                    fallback_account = "ricavi_escursioni"
                else:
                    fallback_tipo = "uscita"
                    fallback_account = "costi_altri"
                transactions.append({
                    "tipo": fallback_tipo,
                    "currency": "EUR",
                    "importo": 0,
                    "descrizione": piece[:60],
                    "account_code": fallback_account,
                    "confidence": "low",
                })

        if not transactions:
            await update.message.reply_text(
                "❌ Non sono riuscito a parsare nessuna transazione."
            )
            return

        # Sostituisci eventuale preview pendente. `replaced_pending` è True se
        # l'utente ci è arrivato dal blocco pending (ha "abbandonato" il vecchio
        # preview mandando direttamente una nuova transazione).
        _pending_previews[user.id] = {
            "transactions": transactions,
            "created_at": datetime.utcnow(),
            "cassa_account": tg_user["account_code"],
        }
        preview = _format_preview(transactions)
        if replaced_pending:
            preview = (
                "⚠️ Ho sostituito il preview precedente.\n\n" + preview
            )
        await update.message.reply_text(preview)
        return

    # --- Single transaction path ---
    kind, payload = await ask_claude(text)
    if kind != "tx":
        # Claude ha risposto con testo (non era una transazione, oppure errore).
        # `payload` e' gia' user-facing in italiano (i casi errore sono
        # prefissati da ❌ in ask_claude).
        await update.message.reply_text(payload)
        return

    tx = payload

    # Singola transazione: preview SOLO se importo elevato (sanity check).
    # Non blocchiamo piu' su confidence=low / account fallback / descrizione corta:
    # Omar (2026-05-08) vuole zero friction quando struttura del messaggio e'
    # chiara (1 riga, +/- importo descrizione). Le multi-transazioni continuano
    # ad usare _is_suspect dentro _format_preview per marcare le righe dubbie.
    suspect, _reason = _is_high_amount(tx)
    if suspect:
        _pending_previews[user.id] = {
            "transactions": [tx],
            "created_at": datetime.utcnow(),
            "cassa_account": tg_user["account_code"],
        }
        preview = _format_preview([tx])
        if replaced_pending:
            preview = "⚠️ Ho sostituito il preview precedente.\n\n" + preview
        await update.message.reply_text(preview)
        return

    # Path "veloce" originale: scrivi subito
    entry_id, descrizione = _write_one_transaction(
        tx,
        cassa_account=tg_user["account_code"],
        telegram_user_id=user.id,
        user_first_name=user.first_name or "",
    )
    if not entry_id:
        # Distinguiamo: nessun importo valido vs errore Supabase
        lines_check = _build_economic_lines(
            tipo=tx.get("tipo"),
            cassa_account=tg_user["account_code"],
            economic_account=tx.get("account_code") or "costi_altri",
            importo=tx.get("importo"),
            currency=tx.get("currency", "EUR"),
        )
        if not lines_check:
            await update.message.reply_text("❌ Nessun importo valido nel messaggio.")
        else:
            await update.message.reply_text(
                "❌ Errore nel salvare su Supabase. Riprova o contatta Omar."
            )
        return

    await update.message.reply_text(
        _format_single_confirmation(tx, tg_user.get("display_name", ""))
    )


# ============================================================
# Slash commands
# ============================================================
# /raccolgo e /verso sono aperti a chiunque abbia un account_code
# (guide, manager, contabile, proprieta). Il conto mittente/destinatario
# della scrittura contabile viene preso dall'account_code dell'utente
# che scrive il comando — cosi' ognuno registra movimenti coerenti con
# dove stanno fisicamente i soldi.
# Gli altri comandi (cambia, paga_fornitore, report_cassa) restano
# riservati a contabile/proprieta tramite _require_admin.
async def _require_admin(update: Update):
    """Upsert the user and return their telegram_users row IFF they have
    admin role (contabile or proprieta). On rejection, replies and returns None."""
    upsert_telegram_user(update.effective_user)
    tg_user = get_telegram_user(update.effective_user.id)

    if not tg_user or not tg_user.get("account_code"):
        await update.message.reply_text(
            "⏳ Ti ho registrato ma Omar deve ancora associarti a un conto."
        )
        return None

    if tg_user.get("role") not in ("contabile", "proprieta"):
        await update.message.reply_text(
            "🚫 Solo contabile o proprieta possono usare questo comando."
        )
        return None

    return tg_user


async def _require_account_user(update: Update):
    """Upsert the user and return their telegram_users row IFF they have
    an account_code. Used by /raccolgo and /verso, which are open to any
    role with a cassa assegnata."""
    upsert_telegram_user(update.effective_user)
    tg_user = get_telegram_user(update.effective_user.id)

    if not tg_user or not tg_user.get("account_code"):
        await update.message.reply_text(
            "⏳ Ti ho registrato ma Omar deve ancora associarti a un conto."
        )
        return None

    return tg_user


async def _require_paga_fornitore_user(update: Update):
    """Variante di _require_admin per /paga_fornitore: ammette anche utenti
    non-admin che hanno il flag opt-in `can_pay_supplier=TRUE` su
    telegram_users (vedi migration 027). Cosi' Omar puo' concedere accesso
    a singole guide (es. Haytham) con una UPDATE SQL, senza redeploy.
    /cambia e /report_cassa restano _require_admin (admin-only)."""
    upsert_telegram_user(update.effective_user)
    tg_user = get_telegram_user(update.effective_user.id)

    if not tg_user or not tg_user.get("account_code"):
        await update.message.reply_text(
            "⏳ Ti ho registrato ma Omar deve ancora associarti a un conto."
        )
        return None

    role = tg_user.get("role")
    can_pay = bool(tg_user.get("can_pay_supplier"))
    if role not in ("contabile", "proprieta") and not can_pay:
        await update.message.reply_text(
            "🚫 Non sei autorizzato a pagare i fornitori. "
            "Chiedi a Omar di abilitarti."
        )
        return None

    return tg_user


# Token riconosciuti come marcatori di valuta in /raccolgo e /verso.
# Possono comparire come suffisso dell'importo ("5000le") o come token
# separato ("5000 le saif"). Ordinati piu' lunghi prima per evitare match
# parziali (es. "lire" prima di "le").
EGP_SUFFIXES = ("lire", "egp", "le")
EUR_SUFFIXES = ("euro", "eur", "€")
EGP_TOKENS = {"le", "egp", "lire", "l.e.", "l.e", "egiziane"}
EUR_TOKENS = {"eur", "euro", "€"}


def _parse_amount_token(token: str) -> tuple[float | None, str | None]:
    """Parse un singolo token tipo '5000le', '200', '200eur'. Ritorna
    (importo, currency_o_None). Currency e' None se non specificata."""
    s = token.strip().lower().replace(",", ".")
    cur = None
    for t in EGP_SUFFIXES:
        if s.endswith(t) and len(s) > len(t):
            cur = "EGP"
            s = s[:-len(t)].strip()
            break
    if cur is None:
        for t in EUR_SUFFIXES:
            if s.endswith(t) and len(s) > len(t):
                cur = "EUR"
                s = s[:-len(t)].strip()
                break
    try:
        return float(s), cur
    except ValueError:
        return None, None


def _parse_amount_text(text: str) -> tuple[float | None, str]:
    """Parse il testo libero del flow conversazionale. Accetta:
    '200', '200 EUR', '5000 le', '5000le', '5,000 lire'. Default EUR."""
    parts = (text or "").strip().split()
    if not parts:
        return None, "EUR"
    amt, cur = _parse_amount_token(parts[0])
    if amt is None:
        return None, "EUR"
    if cur is None and len(parts) > 1:
        tail = parts[1].lower()
        if tail in EGP_TOKENS:
            cur = "EGP"
        elif tail in EUR_TOKENS:
            cur = "EUR"
    return amt, (cur or "EUR")


def _extract_currency_args(args: list[str]) -> tuple[list[str], float | None, str]:
    """Estrae importo e valuta dagli args di /raccolgo o /verso. Ritorna
    (args_residui_per_destinatario, importo, currency). Importo None se
    args[0] non e' parsabile come numero."""
    if not args:
        return [], None, "EUR"
    amt, cur = _parse_amount_token(args[0])
    if amt is None:
        return args, None, "EUR"
    rest = list(args[1:])
    # Se il prossimo token e' una keyword di valuta, la consuma.
    if rest:
        head = rest[0].lower()
        if head in EGP_TOKENS:
            cur = "EGP"
            rest = rest[1:]
        elif head in EUR_TOKENS:
            cur = "EUR"
            rest = rest[1:]
    return rest, amt, (cur or "EUR")


def _fmt_money(amount: float, currency: str) -> str:
    """Display compatto: '€200.00' o 'EGP 5,000'."""
    if currency == "EGP":
        return f"EGP {amount:,.0f}"
    return f"€{amount:,.2f}"


def _do_raccolgo(
    importo: float,
    sender: dict,
    receiver_account: str,
    receiver_name: str,
    telegram_user_id: int,
    currency: str = "EUR",
) -> tuple[bool, str]:
    """Insert the balanced journal entry for /raccolgo. The 'sender' is the
    telegram_user the money is collected FROM (any role with account_code)."""
    entry_id = insert_journal_entry(
        description=f"Raccolta da {sender['display_name']} (a {receiver_name})",
        source="telegram",
        telegram_user_id=telegram_user_id,
        lines=[
            {"account_code": receiver_account,
             "dare": importo, "avere": 0, "currency": currency},
            {"account_code": sender["account_code"],
             "dare": 0, "avere": importo, "currency": currency},
        ],
    )
    if not entry_id:
        return False, "❌ Errore nel registrare. Riprova."
    return True, (
        f"✅ Raccolto {_fmt_money(importo, currency)} da {sender['display_name']}\n\n"
        f"I soldi sono ora in {receiver_account} ({receiver_name})."
    )


async def _send_user_keyboard(
    message,
    importo: float,
    *,
    callback_prefix: str,                  # "racc" | "verso"
    cancel_data: str,                      # "racc_cancel" | "verso_cancel"
    title: str,                            # heading shown above the keyboard
    question: str,                         # prompt below the heading
    currency: str = "EUR",
    exclude_account: str | None = None,
    extra_buttons: list[InlineKeyboardButton] | None = None,
) -> bool:
    """Render an inline keyboard with all telegram_users having an account_code.
    Optionally appends extra buttons (e.g. '🏦 Banca' for /verso) and an Annulla.
    callback_data: '<prefix>:<currency>:<importo>:<account>'.
    Returns True if at least one option was shown, False otherwise."""
    users = fetch_users_with_account(exclude_account=exclude_account)
    if not users and not extra_buttons:
        await message.reply_text("❌ Nessun utente disponibile con conto assegnato.")
        return False
    keyboard = []
    for i in range(0, len(users), 2):
        row = [
            InlineKeyboardButton(
                _user_button_label(users[i]),
                callback_data=f"{callback_prefix}:{currency}:{importo:.2f}:{users[i]['account_code']}",
            )
        ]
        if i + 1 < len(users):
            row.append(
                InlineKeyboardButton(
                    _user_button_label(users[i + 1]),
                    callback_data=f"{callback_prefix}:{currency}:{importo:.2f}:{users[i + 1]['account_code']}",
                )
            )
        keyboard.append(row)
    if extra_buttons:
        for btn in extra_buttons:
            keyboard.append([btn])
    keyboard.append([
        InlineKeyboardButton("❌ Annulla", callback_data=cancel_data)
    ])
    await message.reply_text(
        f"{title}\n\n{question}",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return True


async def cmd_raccolgo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/raccolgo [importo[valuta]] [le|egp] [da_chi]  — flow entry point.

    Modalita':
      - /raccolgo                 → chiede importo, poi mostra lista utenti
      - /raccolgo 200             → 200 EUR, mostra lista
      - /raccolgo 5000le          → 5000 EGP, mostra lista
      - /raccolgo 200 saif        → 200 EUR diretto da saif
      - /raccolgo 5000 le saif    → 5000 EGP diretto da saif
      - /raccolgo 5000le saif     → 5000 EGP diretto da saif

    Default valuta = EUR. Per lire egiziane: suffisso 'le'/'egp'/'lire'
    sull'importo, oppure parola separata subito dopo l'importo.

    Telegram in chat invia il comando subito quando lo selezioni
    dall'autocomplete: per questo /raccolgo da solo entra in un flow
    conversazionale (chiede l'importo come messaggio).

    Scrittura:
      dare  <conto di chi raccoglie>  <importo>
      avere <conto di chi consegna>   <importo>

    Il conto destinatario e' preso dall'account_code dell'utente che scrive
    il comando. Si puo' raccogliere da qualsiasi utente con account_code
    (guida, manager, contabile, proprieta) — esclusi se' stessi.
    """
    tg_user = await _require_account_user(update)
    if not tg_user:
        return ConversationHandler.END

    receiver_account = tg_user["account_code"]
    receiver_name = tg_user.get("display_name") or "admin"

    args = context.args
    if len(args) == 0:
        await update.message.reply_text(
            "💰 Quanto stai raccogliendo?\n"
            "Scrivi il numero — default EUR.\n"
            "Per lire egiziane aggiungi 'le' o 'lire' (es. '5000 le').\n\n"
            "(Annulla con /annulla)"
        )
        return RACC_AMOUNT

    rest, importo, currency = _extract_currency_args(args)
    if importo is None:
        await update.message.reply_text(f"❌ '{args[0]}' non è un numero valido.")
        return ConversationHandler.END
    if importo <= 0:
        await update.message.reply_text("❌ L'importo deve essere maggiore di zero.")
        return ConversationHandler.END

    if not rest:
        await _send_user_keyboard(
            update.message, importo,
            callback_prefix="racc",
            cancel_data="racc_cancel",
            title=f"💰 Raccogli {_fmt_money(importo, currency)}",
            question="Da chi?",
            currency=currency,
            exclude_account=receiver_account,
        )
        return ConversationHandler.END

    sender_name = " ".join(rest).strip()
    sender = find_user_by_name(sender_name)
    if not sender:
        await update.message.reply_text(
            f"❌ '{sender_name}' non trovato/a.\n"
            f"Deve prima scrivere un messaggio al bot e essere registrato/a da Omar."
        )
        return ConversationHandler.END
    if not sender.get("account_code"):
        await update.message.reply_text(
            f"❌ {sender['display_name']} è registrato/a ma non ha ancora un conto assegnato."
        )
        return ConversationHandler.END
    if sender["account_code"] == receiver_account:
        await update.message.reply_text("❌ Non puoi raccogliere da te stesso.")
        return ConversationHandler.END

    ok, msg = _do_raccolgo(
        importo, sender, receiver_account, receiver_name,
        update.effective_user.id, currency=currency,
    )
    # No parse_mode — account codes contain underscores che rompono il Markdown.
    await update.message.reply_text(msg)
    return ConversationHandler.END


async def racc_on_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2 del flow: l'utente ha scritto l'importo come messaggio."""
    tg_user = await _require_account_user(update)
    if not tg_user:
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    importo, currency = _parse_amount_text(text)
    if importo is None:
        await update.message.reply_text(
            f"❌ '{text}' non è un numero valido. Riprova "
            f"(es. '200' per EUR, '5000 le' per lire egiziane)."
        )
        return RACC_AMOUNT
    if importo <= 0:
        await update.message.reply_text("❌ L'importo deve essere maggiore di zero. Riprova.")
        return RACC_AMOUNT

    await _send_user_keyboard(
        update.message, importo,
        callback_prefix="racc",
        cancel_data="racc_cancel",
        title=f"💰 Raccogli {_fmt_money(importo, currency)}",
        question="Da chi?",
        currency=currency,
        exclude_account=tg_user["account_code"],
    )
    return ConversationHandler.END


async def racc_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback /annulla per il flow di /raccolgo."""
    await update.message.reply_text("❌ Raccolta annullata.")
    return ConversationHandler.END


async def racc_on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback handler per la tastiera di /raccolgo: completa la raccolta."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "racc_cancel":
        await query.edit_message_text("❌ Raccolta annullata.")
        return

    if not data.startswith("racc:"):
        return

    # Re-check permessi: serve un account_code (qualsiasi ruolo).
    tg_user = get_telegram_user(update.effective_user.id)
    if not tg_user or not tg_user.get("account_code"):
        await query.edit_message_text("🚫 Non sei autorizzato a raccogliere.")
        return

    receiver_account = tg_user["account_code"]
    receiver_name = tg_user.get("display_name") or "admin"

    # Format: 'racc:<currency>:<importo>:<account_code>'
    try:
        _, currency, importo_str, account_code = data.split(":", 3)
        importo = float(importo_str)
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Errore nel callback.")
        return

    if account_code == receiver_account:
        await query.edit_message_text("❌ Non puoi raccogliere da te stesso.")
        return

    sender = find_user_by_account_code(account_code)
    if not sender or not sender.get("account_code"):
        await query.edit_message_text("❌ Utente non trovato.")
        return

    ok, msg = _do_raccolgo(
        importo, sender, receiver_account, receiver_name,
        update.effective_user.id, currency=currency,
    )
    await query.edit_message_text(msg)


def _do_verso(
    importo: float,
    dest_account: str,
    dest_label: str,
    sender_account: str,
    sender_name: str,
    telegram_user_id: int,
    currency: str = "EUR",
) -> tuple[bool, str]:
    """Insert the balanced journal entry for /verso. Returns (ok, message)."""
    entry_id = insert_journal_entry(
        description=f"Versamento a {dest_label} (da {sender_name})",
        source="telegram",
        telegram_user_id=telegram_user_id,
        lines=[
            {"account_code": dest_account,
             "dare": importo, "avere": 0, "currency": currency},
            {"account_code": sender_account,
             "dare": 0, "avere": importo, "currency": currency},
        ],
    )
    if not entry_id:
        return False, "❌ Errore nel registrare. Riprova."
    return True, (
        f"✅ Versati {_fmt_money(importo, currency)} a {dest_label}\n\n"
        f"{sender_account} ({sender_name}) aggiornato."
    )


async def _send_verso_keyboard(
    message, importo: float, exclude_account: str, currency: str = "EUR"
) -> bool:
    """Tastiera per /verso: lista utenti + bottone 'Banca' come extra."""
    bank_btn = InlineKeyboardButton(
        "🏦 Banca",
        callback_data=f"verso:{currency}:{importo:.2f}:{BANK_ACCOUNT}",
    )
    return await _send_user_keyboard(
        message, importo,
        callback_prefix="verso",
        cancel_data="verso_cancel",
        title=f"💸 Versa {_fmt_money(importo, currency)}",
        question="A chi?",
        currency=currency,
        exclude_account=exclude_account,
        extra_buttons=[bank_btn],
    )


async def cmd_verso(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/verso [importo[valuta]] [le|egp] [destinazione]  — flow entry point.

    Modalita':
      - /verso                    → chiede importo, poi mostra lista
      - /verso 2000               → 2000 EUR, mostra lista (utenti + banca)
      - /verso 5000le             → 5000 EGP, mostra lista
      - /verso 2000 omar          → 2000 EUR diretto a Omar
      - /verso 500 banca          → 500 EUR diretto a banca
      - /verso 5000 le banca      → 5000 EGP diretto a banca
      - /verso 5000le banca       → 5000 EGP diretto a banca

    Default valuta = EUR. Per lire egiziane: suffisso 'le'/'egp'/'lire'
    sull'importo, oppure parola separata subito dopo l'importo.

    Scrittura:
      dare  <destinazione>         <importo>
      avere <conto di chi versa>   <importo>

    Il conto mittente e' preso dall'account_code dell'utente che scrive
    il comando. Si puo' versare a qualsiasi utente con account_code o a
    'banca' — escluso se' stessi.
    """
    tg_user = await _require_account_user(update)
    if not tg_user:
        return ConversationHandler.END

    sender_account = tg_user["account_code"]
    sender_name = tg_user.get("display_name") or "admin"

    args = context.args
    if len(args) == 0:
        await update.message.reply_text(
            "💸 Quanto stai versando?\n"
            "Scrivi il numero — default EUR.\n"
            "Per lire egiziane aggiungi 'le' o 'lire' (es. '5000 le').\n\n"
            "(Annulla con /annulla)"
        )
        return VERSO_AMOUNT

    rest, importo, currency = _extract_currency_args(args)
    if importo is None:
        await update.message.reply_text(f"❌ '{args[0]}' non è un numero valido.")
        return ConversationHandler.END
    if importo <= 0:
        await update.message.reply_text("❌ L'importo deve essere maggiore di zero.")
        return ConversationHandler.END

    if not rest:
        await _send_verso_keyboard(update.message, importo, sender_account, currency)
        return ConversationHandler.END

    # rest >= 1: prima prova le destinazioni speciali (banca / proprieta alias),
    # altrimenti cerca un utente per nome.
    dest_raw = rest[0].strip().lower()
    dest_aliases = {
        "banca": (BANK_ACCOUNT, "Banca"),
        "bank":  (BANK_ACCOUNT, "Banca"),
        "omar":      ("proprieta", "Omar"),
        "proprieta": ("proprieta", "Omar"),
        "proprietà": ("proprieta", "Omar"),
    }
    if dest_raw in dest_aliases:
        dest_account, dest_label = dest_aliases[dest_raw]
    else:
        dest_user = find_user_by_name(dest_raw)
        if not dest_user or not dest_user.get("account_code"):
            await update.message.reply_text(
                f"❌ Destinazione '{dest_raw}' non trovata.\n"
                f"Usa il nome di un utente registrato, oppure 'banca'."
            )
            return ConversationHandler.END
        dest_account = dest_user["account_code"]
        dest_label = dest_user["display_name"]

    if dest_account == sender_account:
        await update.message.reply_text(
            f"❌ Non puoi versare a te stesso ({sender_account})."
        )
        return ConversationHandler.END

    ok, msg = _do_verso(
        importo, dest_account, dest_label,
        sender_account, sender_name, update.effective_user.id,
        currency=currency,
    )
    # No parse_mode — account codes contain underscores.
    await update.message.reply_text(msg)
    return ConversationHandler.END


async def verso_on_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2 del flow: l'utente ha scritto l'importo come messaggio."""
    tg_user = await _require_account_user(update)
    if not tg_user:
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    importo, currency = _parse_amount_text(text)
    if importo is None:
        await update.message.reply_text(
            f"❌ '{text}' non è un numero valido. Riprova "
            f"(es. '2000' per EUR, '50000 le' per lire egiziane)."
        )
        return VERSO_AMOUNT
    if importo <= 0:
        await update.message.reply_text("❌ L'importo deve essere maggiore di zero. Riprova.")
        return VERSO_AMOUNT

    await _send_verso_keyboard(
        update.message, importo, tg_user["account_code"], currency
    )
    return ConversationHandler.END


async def verso_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback /annulla per il flow di /verso."""
    await update.message.reply_text("❌ Versamento annullato.")
    return ConversationHandler.END


async def verso_on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback handler per la tastiera di /verso: completa il versamento."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "verso_cancel":
        await query.edit_message_text("❌ Versamento annullato.")
        return

    if not data.startswith("verso:"):
        return

    tg_user = get_telegram_user(update.effective_user.id)
    if not tg_user or not tg_user.get("account_code"):
        await query.edit_message_text("🚫 Non sei autorizzato a versare.")
        return

    sender_account = tg_user["account_code"]
    sender_name = tg_user.get("display_name") or "admin"

    # Format: 'verso:<currency>:<importo>:<dest_account>'
    try:
        _, currency, importo_str, dest_account = data.split(":", 3)
        importo = float(importo_str)
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Errore nel callback.")
        return

    if dest_account == sender_account:
        await query.edit_message_text("❌ Non puoi versare a te stesso.")
        return

    if dest_account == BANK_ACCOUNT:
        dest_label = "Banca"
    else:
        dest_user = find_user_by_account_code(dest_account)
        if not dest_user:
            await query.edit_message_text("❌ Destinazione non trovata.")
            return
        dest_label = dest_user["display_name"]

    ok, msg = _do_verso(
        importo, dest_account, dest_label,
        sender_account, sender_name, update.effective_user.id,
        currency=currency,
    )
    await query.edit_message_text(msg)


# ============================================================
# /cambia — cambio valuta EUR → EGP (transit via cambio_valuta)
# ============================================================
# Aggiunto 05/05/2026 (richiesta Omar — Amr cambia ~10 volte/settimana).
#
# L'utente scrive 2 importi reali (EUR dato, EGP ricevuto). Il bot calcola
# il rate, mostra conferma, e crea UNA entry con 4 righe:
#
#   dare  cambio_valuta      EUR <eur>
#   avere <cassa>            EUR <eur>   ← user perde EUR dalla tasca
#   dare  <cassa>            EGP <egp>   ← user riceve EGP in tasca
#   avere cambio_valuta      EGP <egp>
#
# Bilanciato per-currency (EUR side e EGP side balance separately, come
# richiesto dal trigger check_entry_balanced).
#
# Cassa = account_code di chi scrive il comando (Amr → cassa_contabile,
# Omar → proprieta). Coerente con /raccolgo e /verso.
#
# IMPORTANTE: il conto 'cambio_valuta' deve esistere in accounts. Se manca,
# l'INSERT delle lines fallisce (FK su account_code) e _do_cambio rolla
# back l'entry. Mostra errore esplicito all'utente.

def _do_cambio(
    eur: float,
    egp: float,
    cassa_account: str,
    cassa_name: str,
    telegram_user_id: int,
) -> tuple[bool, str]:
    """Insert the balanced 4-line journal entry for /cambia.
    Returns (ok, message). Uses the 'cambio_valuta' transit account to
    balance EUR and EGP sides independently."""
    rate = egp / eur if eur > 0 else 0
    description = f"Cambio €{eur:.2f} → EGP {egp:.0f} (rate {rate:.2f})"
    entry_id = insert_journal_entry(
        description=description,
        source="telegram",
        telegram_user_id=telegram_user_id,
        lines=[
            # EUR side: cassa esce (avere), cambio_valuta riceve (dare)
            {"account_code": "cambio_valuta",
             "dare": eur, "avere": 0, "currency": "EUR"},
            {"account_code": cassa_account,
             "dare": 0, "avere": eur, "currency": "EUR"},
            # EGP side: cassa riceve (dare), cambio_valuta esce (avere)
            {"account_code": cassa_account,
             "dare": egp, "avere": 0, "currency": "EGP"},
            {"account_code": "cambio_valuta",
             "dare": 0, "avere": egp, "currency": "EGP"},
        ],
    )
    if not entry_id:
        return False, (
            "❌ Errore nel registrare. Verifica che il conto 'cambio_valuta' "
            "esista in accounts. Riprova."
        )
    return True, (
        f"✅ Cambio registrato!\n\n"
        f"Hai dato:    €{eur:.2f} EUR\n"
        f"Hai ricevuto: EGP {egp:,.0f}\n"
        f"Rate:         1 EUR = {rate:.2f} EGP\n\n"
        f"{cassa_account} ({cassa_name}): −€{eur:.2f}, +EGP {egp:,.0f}"
    )


async def _show_cambio_confirm(message, eur: float, egp: float):
    """Render the confirmation card with summary + Conferma/Annulla buttons."""
    rate = egp / eur if eur > 0 else 0
    keyboard = [[
        InlineKeyboardButton("✅ Conferma", callback_data="cambia_confirm"),
        InlineKeyboardButton("❌ Annulla",  callback_data="cambia_cancel"),
    ]]
    # No parse_mode — i nomi conto contengono underscore e potrebbero
    # finire dentro la message text in altri contesti. Plain + emoji.
    await message.reply_text(
        f"💱 Cambio valuta\n\n"
        f"Hai dato:    €{eur:.2f} EUR\n"
        f"Hai ricevuto: EGP {egp:,.0f}\n"
        f"Rate:         1 EUR = {rate:.2f} EGP\n\n"
        f"Confermi?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_cambia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cambia [eur] [egp]  — cambio valuta EUR → EGP.

    Tre modalita':
      - /cambia              → flow: chiede EUR poi EGP poi conferma
      - /cambia 100          → flow accorciato: chiede solo EGP poi conferma
      - /cambia 100 5050     → diretto: mostra conferma, registra al click

    Cassa coinvolta = account_code del chiamante (cassa_contabile per Amr,
    proprieta per Omar).
    """
    tg_user = await _require_admin(update)
    if not tg_user:
        return ConversationHandler.END

    args = context.args

    # Caso 1: nessun argomento → entra in flow chiedendo EUR
    if len(args) == 0:
        await update.message.reply_text(
            "💱 Quanti EUR hai dato?\n"
            "Scrivi solo il numero, es. 100.\n\n"
            "(Annulla con /annulla)"
        )
        return CAMBIA_EUR_AMOUNT

    # Parse primo arg (EUR)
    try:
        eur = float(args[0].replace(",", "."))
    except ValueError:
        await update.message.reply_text(f"❌ '{args[0]}' non è un numero valido.")
        return ConversationHandler.END
    if eur <= 0:
        await update.message.reply_text("❌ L'importo EUR deve essere maggiore di zero.")
        return ConversationHandler.END

    # Caso 2: solo EUR → chiedi EGP
    if len(args) == 1:
        context.user_data["cambia_eur"] = eur
        await update.message.reply_text(
            f"💱 €{eur:.2f} EUR.\n\n"
            f"Quanti EGP hai ricevuto?\n"
            f"Scrivi solo il numero, es. 5050."
        )
        return CAMBIA_EGP_AMOUNT

    # Caso 3: EUR + EGP → mostra conferma
    try:
        egp = float(args[1].replace(",", "."))
    except ValueError:
        await update.message.reply_text(f"❌ '{args[1]}' non è un numero valido.")
        return ConversationHandler.END
    if egp <= 0:
        await update.message.reply_text("❌ L'importo EGP deve essere maggiore di zero.")
        return ConversationHandler.END

    context.user_data["cambia_eur"] = eur
    context.user_data["cambia_egp"] = egp
    await _show_cambio_confirm(update.message, eur, egp)
    return CAMBIA_CONFIRM


async def cambia_on_eur(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step EUR del flow: l'utente ha scritto l'importo EUR come messaggio."""
    tg_user = await _require_admin(update)
    if not tg_user:
        return ConversationHandler.END

    text = (update.message.text or "").strip().replace(",", ".")
    try:
        eur = float(text)
    except ValueError:
        await update.message.reply_text(
            f"❌ '{text}' non è un numero valido. Riprova (solo il numero, es. 100)."
        )
        return CAMBIA_EUR_AMOUNT
    if eur <= 0:
        await update.message.reply_text("❌ Deve essere maggiore di zero. Riprova.")
        return CAMBIA_EUR_AMOUNT

    context.user_data["cambia_eur"] = eur
    await update.message.reply_text(
        f"💱 €{eur:.2f} EUR.\n\n"
        f"Quanti EGP hai ricevuto?\n"
        f"Scrivi solo il numero, es. 5050."
    )
    return CAMBIA_EGP_AMOUNT


async def cambia_on_egp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step EGP del flow: l'utente ha scritto l'importo EGP come messaggio."""
    tg_user = await _require_admin(update)
    if not tg_user:
        return ConversationHandler.END

    text = (update.message.text or "").strip().replace(",", ".")
    try:
        egp = float(text)
    except ValueError:
        await update.message.reply_text(
            f"❌ '{text}' non è un numero valido. Riprova (solo il numero, es. 5050)."
        )
        return CAMBIA_EGP_AMOUNT
    if egp <= 0:
        await update.message.reply_text("❌ Deve essere maggiore di zero. Riprova.")
        return CAMBIA_EGP_AMOUNT

    eur = context.user_data.get("cambia_eur")
    if not eur:
        # Non dovrebbe succedere se il flow e' rispettato, ma difensivo
        await update.message.reply_text(
            "❌ Importo EUR perso. Ricomincia con /cambia."
        )
        return ConversationHandler.END

    context.user_data["cambia_egp"] = egp
    await _show_cambio_confirm(update.message, eur, egp)
    return CAMBIA_CONFIRM


async def cambia_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback /annulla per il flow di /cambia."""
    context.user_data.pop("cambia_eur", None)
    context.user_data.pop("cambia_egp", None)
    await update.message.reply_text("❌ Cambio annullato.")
    return ConversationHandler.END


async def cambia_on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback dal bottone Conferma/Annulla: scrive l'entry o annulla."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "cambia_cancel":
        context.user_data.pop("cambia_eur", None)
        context.user_data.pop("cambia_egp", None)
        await query.edit_message_text("❌ Cambio annullato.")
        return ConversationHandler.END

    if data != "cambia_confirm":
        return CAMBIA_CONFIRM

    # Re-check permessi: solo contabile/proprieta con account_code possono cambiare
    tg_user = get_telegram_user(update.effective_user.id)
    if (
        not tg_user
        or not tg_user.get("account_code")
        or tg_user.get("role") not in ("contabile", "proprieta")
    ):
        await query.edit_message_text("🚫 Non sei autorizzato a fare cambi valuta.")
        return ConversationHandler.END

    eur = context.user_data.get("cambia_eur")
    egp = context.user_data.get("cambia_egp")
    if not eur or not egp:
        await query.edit_message_text("❌ Dati persi. Ricomincia con /cambia.")
        return ConversationHandler.END

    cassa_account = tg_user["account_code"]
    cassa_name = tg_user.get("display_name") or "admin"

    ok, msg = _do_cambio(eur, egp, cassa_account, cassa_name, update.effective_user.id)
    await query.edit_message_text(msg)

    context.user_data.pop("cambia_eur", None)
    context.user_data.pop("cambia_egp", None)
    return ConversationHandler.END


# ============================================================
# /paga_fornitore — registra pagamento fornitore con flow conversazionale
# ============================================================
# Aggiunto 26/04/2026 (richiesta Omar). Permette a contabile/proprieta di
# registrare via Telegram un pagamento a uno dei fornitori (Shamandura,
# Shaarawy, ecc.) senza dover aprire la dashboard. Flow a step:
#   1. /paga_fornitore     → tastiera con i 6 fornitori
#   2. tap fornitore       → chiede importo (€)
#   3. risposta numero     → tastiera con casse pagatrici
#   4. tap cassa           → mostra riepilogo + ✅/❌
#   5. tap conferma        → scrive su Supabase, conferma all'utente
#
# Scrittura partita doppia generata (default in_mano=0 → no compensazione
# cassa_fornitore_X; identica al caso "in_mano=0" della dashboard
# register_supplier_payment):
#   DARE  costi_escursioni    importo
#   AVERE cassa_pagatrice     importo
#
# Per casi rari con compensazione (fornitore aveva soldi in mano dai
# clienti) → usare la dashboard, non il bot.
# ============================================================

# Stati del ConversationHandler
PAY_SUPPLIER, PAY_AMOUNT, PAY_CASSA, PAY_CONFIRM, PAY_CLIENT_NAME = range(5)

# Sentinel per la "cassa" speciale "il cliente paga direttamente". Non e' un
# account_code reale — quando arriva qui ramifichiamo a una entry header-only
# (vedi pf_on_confirm + insert_journal_entry con lines=[]).
CLIENTE_PAGA_SENTINEL = "cliente"
RACC_AMOUNT = 10  # Stato per /raccolgo (separato dai PAY_*)
VERSO_AMOUNT = 11  # Stato per /verso
CAMBIA_EUR_AMOUNT = 12  # Stato per /cambia, step "quanti EUR hai dato?"
CAMBIA_EGP_AMOUNT = 13  # Stato per /cambia, step "quanti EGP hai ricevuto?"
CAMBIA_CONFIRM = 14     # Stato per /cambia, attesa click su Conferma/Annulla

# Account speciale per i versamenti in banca (NON un telegram_user — e' un
# conto contabile diretto). Usato come callback_data per /verso.
BANK_ACCOUNT = "banca"

# Lista fornitori (code → label). Hardcoded perche':
# - cambia raramente (nuovo fornitore = side-task migration + redeploy bot)
# - evita query Supabase a ogni /paga_fornitore
SUPPLIERS = [
    ("cassa_fornitore_shamandura",   "🌊 Shamandura"),
    ("cassa_fornitore_shaarawy",     "✈️ Shaarawy"),
    ("cassa_fornitore_ramadan",      "🚌 Ramadan"),
    ("cassa_fornitore_sottomarino",  "🤿 Sottomarino"),
    ("cassa_fornitore_naama_safari", "🛥 Naama Safari"),
    ("cassa_fornitori_vari",         "📦 Vari"),
]

# Casse pagatrici disponibili (chi ha materialmente i soldi che escono)
PAYER_CASSE = [
    ("cassa_contabile", "Cassa Contabile"),
    ("proprieta",       "Cassa Omar (proprietà)"),
]

# Categoria di costo fissa per i pagamenti fornitore via bot. Se in futuro
# servisse differenziare (es. trasporti per Naama Safari), aggiungere step
# di scelta categoria. Per ora 90% dei pagamenti = escursioni.
PAYFORN_COST_ACCOUNT = "costi_escursioni"
# Categoria di ricavo per il Branch A ("cliente paga direttamente al fornitore"):
# il cliente consegna 150€ al fornitore → 150€ ricavi nostri + saldo cassa_fornitore
# che cala mano a mano che il fornitore consuma il servizio o ci restituisce.
PAYFORN_REVENUE_ACCOUNT = "ricavi_escursioni"


def _supplier_label(code: str) -> str:
    """Lookup label leggibile per un supplier_code. Ritorna il code stesso
    come fallback se il fornitore non e' nella lista (no crash)."""
    for c, label in SUPPLIERS:
        if c == code:
            return label
    return code


def _cassa_label(code: str) -> str:
    """Lookup label leggibile per una cassa pagatrice."""
    for c, label in PAYER_CASSE:
        if c == code:
            return label
    return code


async def cmd_paga_fornitore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: /paga_fornitore — mostra tastiera fornitori.
    Ammessi: contabile, proprieta, oppure utenti con can_pay_supplier=TRUE."""
    tg_user = await _require_paga_fornitore_user(update)
    if not tg_user:
        return ConversationHandler.END

    # Reset stato user_data eventuale (se l'utente aveva un flow aperto)
    context.user_data.pop("pf_supplier", None)
    context.user_data.pop("pf_amount", None)
    context.user_data.pop("pf_cassa", None)
    context.user_data.pop("pf_client_name", None)
    context.user_data.pop("pf_payment_reference", None)

    # Costruisci tastiera 2 colonne con i 6 fornitori + bottone annulla
    keyboard = []
    for i in range(0, len(SUPPLIERS), 2):
        row = [
            InlineKeyboardButton(
                SUPPLIERS[i][1],
                callback_data=f"pf_supp:{SUPPLIERS[i][0]}",
            )
        ]
        if i + 1 < len(SUPPLIERS):
            row.append(
                InlineKeyboardButton(
                    SUPPLIERS[i + 1][1],
                    callback_data=f"pf_supp:{SUPPLIERS[i + 1][0]}",
                )
            )
        keyboard.append(row)
    keyboard.append([
        InlineKeyboardButton("❌ Annulla", callback_data="pf_cancel")
    ])

    await update.message.reply_text(
        "💼 Pagamento fornitore\n\nScegli il fornitore:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return PAY_SUPPLIER


async def pf_on_supplier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2: utente ha scelto fornitore → chiedi importo."""
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == "pf_cancel":
        await query.edit_message_text("❌ Pagamento annullato.")
        context.user_data.clear()
        return ConversationHandler.END

    if not data.startswith("pf_supp:"):
        return PAY_SUPPLIER  # ignora callback inattese

    supplier_code = data.removeprefix("pf_supp:")
    context.user_data["pf_supplier"] = supplier_code

    await query.edit_message_text(
        f"💼 Fornitore: {_supplier_label(supplier_code)}\n\n"
        f"💰 Quanto stai pagando (€)?\n"
        f"Scrivi solo il numero, es. 450"
    )
    return PAY_AMOUNT


async def pf_on_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 3: utente ha scritto importo → mostra tastiera casse."""
    text = (update.message.text or "").strip().replace(",", ".")
    try:
        importo = float(text)
    except ValueError:
        await update.message.reply_text(
            f"❌ '{text}' non è un numero valido. Riprova (es. 450)."
        )
        return PAY_AMOUNT
    if importo <= 0:
        await update.message.reply_text(
            "❌ L'importo deve essere maggiore di zero. Riprova."
        )
        return PAY_AMOUNT

    context.user_data["pf_amount"] = importo
    supplier_label = _supplier_label(context.user_data.get("pf_supplier", ""))

    # Tastiera: 1 riga con le casse pagatrici (cassa contabile / proprieta),
    # 1 riga con "Cliente" (sentinel per pagamento diretto cliente→fornitore),
    # 1 riga con Annulla.
    keyboard = [
        [
            InlineKeyboardButton(label, callback_data=f"pf_cassa:{code}")
            for code, label in PAYER_CASSE
        ],
        [
            InlineKeyboardButton(
                "🧑 Cliente (paga direttamente)",
                callback_data=f"pf_cassa:{CLIENTE_PAGA_SENTINEL}",
            )
        ],
        [
            InlineKeyboardButton("❌ Annulla", callback_data="pf_cancel")
        ],
    ]

    await update.message.reply_text(
        f"💼 Fornitore: {supplier_label}\n"
        f"💰 Importo: €{importo:.2f}\n\n"
        f"🏦 Da chi vengono i soldi?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return PAY_CASSA


async def pf_on_cassa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 4: utente ha scelto la fonte dei soldi.
    - Se cassa interna → vai dritto al riepilogo (PAY_CONFIRM).
    - Se 'Cliente' (CLIENTE_PAGA_SENTINEL) → chiedi nome e cognome del
      cliente (PAY_CLIENT_NAME), poi riepilogo.
    """
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == "pf_cancel":
        await query.edit_message_text("❌ Pagamento annullato.")
        context.user_data.clear()
        return ConversationHandler.END

    if not data.startswith("pf_cassa:"):
        return PAY_CASSA

    cassa_code = data.removeprefix("pf_cassa:")
    context.user_data["pf_cassa"] = cassa_code

    # Branch: cliente paga direttamente → chiediamo il nome prima di confermare.
    if cassa_code == CLIENTE_PAGA_SENTINEL:
        await query.edit_message_text(
            "🧑 Pagamento del cliente direttamente al fornitore.\n\n"
            "Scrivi nome e cognome del cliente (es. 'Mario Rossi').\n"
            "La data pagamento sarà oggi; potrai abbinarlo alla prenotazione "
            "Pharos appena esiste.\n\n"
            "(Annulla con /annulla)"
        )
        return PAY_CLIENT_NAME

    supplier_code = context.user_data.get("pf_supplier", "")
    importo = context.user_data.get("pf_amount", 0.0)

    keyboard = [[
        InlineKeyboardButton("✅ Conferma", callback_data="pf_confirm"),
        InlineKeyboardButton("❌ Annulla",  callback_data="pf_cancel"),
    ]]

    # NB: NIENTE parse_mode="Markdown" qui — il nome del conto contiene
    # underscore (costi_escursioni) che Telegram interpreta come italic
    # delimiter, causando 400 BAD REQUEST su edit_message_text e bloccando
    # il flow. Plain text + emoji = sicuro a prescindere dal payload.
    await query.edit_message_text(
        "📋 Riepilogo pagamento\n\n"
        f"• Fornitore: {_supplier_label(supplier_code)}\n"
        f"• Importo: €{importo:.2f}\n"
        f"• Esce da: {_cassa_label(cassa_code)}\n"
        f"• Categoria: Escursioni\n\n"
        "Confermi?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return PAY_CONFIRM


async def pf_on_client_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 4b (solo per cliente_paga_fornitore): l'utente ha scritto il
    nome e cognome del cliente come messaggio. Validiamo (>=2 char),
    salviamo in user_data, mostriamo il riepilogo e chiediamo conferma.
    """
    raw = (update.message.text or "").strip()
    # Validazione minima: almeno 2 caratteri non-whitespace. Non imponiamo
    # pattern strict (nomi composti, accenti, transliterazioni arabe → tutti
    # validi). Trimmiamo e collassiamo gli spazi multipli.
    name = " ".join(raw.split())
    if len(name) < 2:
        await update.message.reply_text(
            "❌ Nome troppo corto. Scrivi nome e cognome del cliente "
            "(almeno 2 caratteri). Es. 'Mario Rossi'."
        )
        return PAY_CLIENT_NAME

    context.user_data["pf_client_name"] = name

    supplier_code = context.user_data.get("pf_supplier", "")
    importo = context.user_data.get("pf_amount", 0.0)

    keyboard = [[
        InlineKeyboardButton("✅ Conferma", callback_data="pf_confirm"),
        InlineKeyboardButton("❌ Annulla",  callback_data="pf_cancel"),
    ]]

    await update.message.reply_text(
        "📋 Riepilogo pagamento (cliente diretto)\n\n"
        f"• Fornitore: {_supplier_label(supplier_code)}\n"
        f"• Importo: €{importo:.2f}\n"
        f"• Pagato da: 🧑 {name} (cliente)\n"
        f"• Data pagamento: oggi\n"
        f"• Match Pharos: da abbinare\n\n"
        "Registro?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return PAY_CONFIRM


async def pf_on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 5: scrive su Supabase + conferma."""
    query = update.callback_query
    await query.answer()

    if query.data == "pf_cancel":
        await query.edit_message_text("❌ Pagamento annullato.")
        context.user_data.clear()
        return ConversationHandler.END

    if query.data != "pf_confirm":
        return PAY_CONFIRM

    supplier_code = context.user_data.get("pf_supplier", "")
    cassa_code    = context.user_data.get("pf_cassa", "")
    importo       = float(context.user_data.get("pf_amount", 0.0))
    client_name   = context.user_data.get("pf_client_name", "")

    if not (supplier_code and cassa_code and importo > 0):
        await query.edit_message_text(
            "❌ Dati incompleti. Riprova con /paga_fornitore."
        )
        context.user_data.clear()
        return ConversationHandler.END

    supplier_label = _supplier_label(supplier_code)
    imp_round      = round(importo, 2)

    # ---------- Branch A: cliente paga direttamente il fornitore ----------
    # Schema migration 011: i soldi sono fisicamente in mano al fornitore
    # ma logicamente nostri (l'incasso E' un nostro ricavo). Quindi:
    #   DARE  cassa_fornitore_X       ← soldi in mano al fornitore (asset)
    #   AVERE ricavi_escursioni       ← ricavo riconosciuto
    # Il saldo positivo su cassa_fornitore_X cala quando il fornitore consuma
    # il servizio (futuro entry con costi_X / cassa_fornitore_X) o ci
    # restituisce il resto (cassa_contabile / cassa_fornitore_X).
    # source='cliente_paga_fornitore' + customer_name (migration 026).
    if cassa_code == CLIENTE_PAGA_SENTINEL:
        if not client_name:
            await query.edit_message_text(
                "❌ Manca il nome del cliente. Riprova con /paga_fornitore."
            )
            context.user_data.clear()
            return ConversationHandler.END

        clean_supplier = supplier_label.lstrip('🌊✈️🚌🤿🛥📦 ')
        description = (
            f"Cliente {client_name} ha pagato direttamente {clean_supplier} "
            f"€{imp_round:.2f}"
        )
        entry_id = insert_journal_entry(
            description=description,
            source="cliente_paga_fornitore",
            telegram_user_id=update.effective_user.id,
            lines=[
                {"account_code": supplier_code,
                 "dare": imp_round, "avere": 0, "currency": "EUR"},
                {"account_code": PAYFORN_REVENUE_ACCOUNT,
                 "dare": 0, "avere": imp_round, "currency": "EUR"},
            ],
            customer_name=client_name,
            supplier_code=supplier_code,
            pharos_match_status="pending",
        )
        if not entry_id:
            await query.edit_message_text(
                "❌ Errore nel registrare. Riprova con /paga_fornitore."
            )
            context.user_data.clear()
            return ConversationHandler.END

        await query.edit_message_text(
            f"✅ Registrato.\n\n"
            f"🧑 {client_name} ha pagato €{imp_round:.2f} direttamente "
            f"a {supplier_label}.\n"
            f"I soldi risultano ora in {supplier_label} (in mano al fornitore)."
        )
        context.user_data.clear()
        return ConversationHandler.END

    # ---------- Branch B: pagamento da cassa interna (flow originale) ----------
    cassa_label = _cassa_label(cassa_code)
    description = f"Pagamento {supplier_label.lstrip('🌊✈️🚌🤿🛥📦 ')} €{importo:.2f}"

    # Scrittura partita doppia "passing-through" su cassa_fornitore_X (4 righe).
    # Cambio del 27/04/2026 (richiesta Omar): il vecchio pattern 2-righe
    # registrava solo costo + cassa pagatrice e non toccava cassa_fornitore_X
    # → l'estratto conto del fornitore non mostrava il pagamento.
    #
    # Ora il pagamento "passa attraverso" il conto del fornitore:
    #   1) Riconosci il debito (lui ha reso servizio):
    #      DARE  costi_escursioni              importo  ← costo in P&L
    #      AVERE cassa_fornitore_X             importo  ← gli dobbiamo
    #   2) Saldi il debito (paghi):
    #      DARE  cassa_fornitore_X             importo  ← chiudi debito
    #      AVERE cassa_pagatrice               importo  ← esce cassa
    #
    # Net effect su cassa_fornitore_X: 0 (debito creato + chiuso). Ma i 2
    # movimenti restano visibili nell'estratto conto del fornitore.
    entry_id = insert_journal_entry(
        description=description,
        source="telegram",
        telegram_user_id=update.effective_user.id,
        lines=[
            # Step 1: cost recognition + debt arises
            {"account_code": PAYFORN_COST_ACCOUNT,
             "dare": imp_round, "avere": 0, "currency": "EUR"},
            {"account_code": supplier_code,
             "dare": 0, "avere": imp_round, "currency": "EUR"},
            # Step 2: debt cleared via cash payment
            {"account_code": supplier_code,
             "dare": imp_round, "avere": 0, "currency": "EUR"},
            {"account_code": cassa_code,
             "dare": 0, "avere": imp_round, "currency": "EUR"},
        ],
    )

    if not entry_id:
        await query.edit_message_text(
            "❌ Errore nel registrare. Riprova con /paga_fornitore."
        )
        context.user_data.clear()
        return ConversationHandler.END

    # No parse_mode: i label sono safe oggi ma se Omar in futuro aggiunge
    # un fornitore con underscore nel nome (improbabile ma possibile) il
    # messaggio si rompe. Plain text e' a prova di payload.
    await query.edit_message_text(
        f"✅ Pagato €{importo:.2f} a {supplier_label}\n\n"
        f"{cassa_label} aggiornata. Vedi il movimento nella dashboard."
    )
    context.user_data.clear()
    return ConversationHandler.END


async def pf_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /annulla durante un flow → reset stato."""
    context.user_data.clear()
    if update.message:
        await update.message.reply_text("❌ Pagamento annullato.")
    return ConversationHandler.END


# ============================================================
# Daily cash snapshot — invio automatico a Omar ogni sera 20:00 Cairo
# ============================================================
# Il contabile (Amr) di solito mandava manualmente ogni sera "ho in mano X,
# oggi entrato Y, uscito Z". Lo automatizziamo: ogni sera alle 20:00 (ora
# del Cairo) il bot calcola lo snapshot di cassa_contabile dai dati Supabase
# e invia un messaggio formattato in chat PRIVATA a Omar (NON nel gruppo).
#
# chat_id destinatario = telegram_user_id di Omar (= chat_id privato col bot
# in Telegram). Lo recuperiamo da telegram_users WHERE role='proprieta'.
# Vincolo: Omar deve aver scritto almeno una volta in privato al bot perche'
# il send_message funzioni (Telegram non permette di scrivere a chi non ha
# mai iniziato la conversazione).
#
# Comando manuale /report_cassa: per testare senza aspettare le 20:00, o
# per chiedere il report al volo. Solo proprieta/contabile.

CAIRO_TZ_NAME = "Africa/Cairo"
DAILY_REPORT_HOUR_CAIRO = 20  # 20:00 Cairo time
SNAPSHOT_CASSA_CODE = "cassa_contabile"


def _fetch_proprieta_user_id() -> int | None:
    """Recupera il telegram_user_id dell'utente con role='proprieta' (Omar)."""
    if not _sb_configured():
        return None
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/telegram_users",
            headers=_sb_headers(),
            params={
                "select": "telegram_user_id",
                "role": "eq.proprieta",
                "limit": "1",
            },
            timeout=10,
        )
        if r.status_code != 200:
            return None
        rows = r.json()
        return int(rows[0]["telegram_user_id"]) if rows else None
    except Exception as e:
        print(f"_fetch_proprieta_user_id error: {e}")
        return None


def _compute_cassa_snapshot(account_code: str, target_date_str: str) -> dict | None:
    """Calcola lo snapshot di una cassa per una data specifica.

    Ritorna dict con: apertura_eur, apertura_le, incassi_eur/le, uscite_eur/le,
    trasferimenti_eur/le, chiusura_eur/le, n_movimenti.

    Strategia: legge tutti i journal_lines di account_code via v_journal_lines
    fino a target_date inclusa. Per le righe del giorno, recupera anche le
    righe gemelle (stessa entry) per classificare incasso/uscita/trasferimento.
    """
    if not _sb_configured():
        return None
    try:
        # 1. Fetch lines on this account up to target_date inclusive
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/v_journal_lines",
            headers=_sb_headers(),
            params={
                "select": "entry_id,entry_date,description,account_code,dare,avere,currency",
                "account_code": f"eq.{account_code}",
                "entry_date": f"lte.{target_date_str}",
                "order": "entry_date.asc",
            },
            timeout=15,
        )
        if r.status_code != 200:
            print(f"_compute_cassa_snapshot fetch lines: {r.status_code}")
            return None
        lines = r.json()
    except Exception as e:
        print(f"_compute_cassa_snapshot error: {e}")
        return None

    # 2. Saldo apertura: somma di tutte le righe PRIMA del target_date
    apertura_eur = 0.0
    apertura_le = 0.0
    today_lines: list[dict] = []
    for ln in lines:
        edate = str(ln.get("entry_date", ""))
        dare = float(ln.get("dare") or 0)
        avere = float(ln.get("avere") or 0)
        curr = ln.get("currency", "EUR")
        if edate < target_date_str:
            if curr == "EUR":
                apertura_eur += dare - avere
            elif curr == "EGP":
                apertura_le += dare - avere
        elif edate == target_date_str:
            today_lines.append(ln)

    # 3. Per le linee del giorno, classifica via siblings
    incassi_eur = incassi_le = 0.0
    uscite_eur = uscite_le = 0.0
    trasf_eur = trasf_le = 0.0

    if today_lines:
        entry_ids = list({ln["entry_id"] for ln in today_lines})
        # PostgREST 'in' filter syntax: in.(uuid1,uuid2,...)
        ids_str = ",".join(entry_ids)
        try:
            r2 = requests.get(
                f"{SUPABASE_URL}/rest/v1/v_journal_lines",
                headers=_sb_headers(),
                params={
                    "select": "entry_id,account_code,account_type,dare,avere,currency",
                    "entry_id": f"in.({ids_str})",
                },
                timeout=15,
            )
            siblings_data = r2.json() if r2.status_code == 200 else []
        except Exception as e:
            print(f"_compute_cassa_snapshot siblings: {e}")
            siblings_data = []

        # Index siblings by entry_id (escludi la riga della cassa stessa)
        siblings_by_entry: dict[str, list[dict]] = {}
        for s in siblings_data:
            if s["account_code"] != account_code:
                siblings_by_entry.setdefault(s["entry_id"], []).append(s)

        for ln in today_lines:
            eid = ln["entry_id"]
            sibs = siblings_by_entry.get(eid, [])
            dare = float(ln.get("dare") or 0)
            avere = float(ln.get("avere") or 0)
            importo = dare - avere  # positivo = entrata cassa
            curr = ln.get("currency", "EUR")

            # Classifica in base al primo sibling significativo
            tipo = "altro"
            if sibs:
                sib = sibs[0]
                stype = sib.get("account_type", "")
                scode = sib.get("account_code", "")
                if stype == "ricavo":
                    tipo = "incasso"
                elif stype == "costo":
                    tipo = "uscita"
                elif scode.startswith("cassa_") or scode in ("proprieta", "banca"):
                    tipo = "trasferimento"

            if curr == "EUR":
                if tipo == "incasso":
                    incassi_eur += importo
                elif tipo == "uscita":
                    uscite_eur += abs(importo)
                elif tipo == "trasferimento":
                    trasf_eur += importo
            elif curr == "EGP":
                if tipo == "incasso":
                    incassi_le += importo
                elif tipo == "uscita":
                    uscite_le += abs(importo)
                elif tipo == "trasferimento":
                    trasf_le += importo

    chiusura_eur = apertura_eur + incassi_eur - uscite_eur + trasf_eur
    chiusura_le = apertura_le + incassi_le - uscite_le + trasf_le

    return {
        "apertura_eur": apertura_eur,
        "apertura_le": apertura_le,
        "incassi_eur": incassi_eur,
        "incassi_le": incassi_le,
        "uscite_eur": uscite_eur,
        "uscite_le": uscite_le,
        "trasferimenti_eur": trasf_eur,
        "trasferimenti_le": trasf_le,
        "chiusura_eur": chiusura_eur,
        "chiusura_le": chiusura_le,
        "n_movimenti": len(today_lines),
    }


def _format_snapshot_text(snapshot: dict, target_date, cassa_label: str = "Cassa Contabile") -> str:
    """Formatta lo snapshot come testo per Telegram."""
    s = snapshot
    delta_eur = s["chiusura_eur"] - s["apertura_eur"]
    delta_le = s["chiusura_le"] - s["apertura_le"]

    def _fmt_eur(v):
        return f"€ {v:,.2f}"

    def _fmt_le_opt(v):
        return f" + {v:,.0f} LE" if abs(v) >= 0.5 else ""

    lines = [
        f"📊 Report {cassa_label}",
        f"📅 {target_date.strftime('%A %d/%m/%Y')}",
        "",
        f"🟦 Apertura:  {_fmt_eur(s['apertura_eur'])}{_fmt_le_opt(s['apertura_le'])}",
        f"📥 Incassi:   {_fmt_eur(s['incassi_eur'])}{_fmt_le_opt(s['incassi_le'])}",
        f"📤 Uscite:    {_fmt_eur(s['uscite_eur'])}{_fmt_le_opt(s['uscite_le'])}",
    ]
    if abs(s["trasferimenti_eur"]) >= 0.01 or abs(s["trasferimenti_le"]) >= 0.5:
        lines.append(
            f"🔄 Trasf.:    € {s['trasferimenti_eur']:+,.2f}"
            f"{_fmt_le_opt(s['trasferimenti_le'])}"
        )
    lines.extend([
        "─────────────────────",
        f"🟩 Chiusura:  {_fmt_eur(s['chiusura_eur'])}{_fmt_le_opt(s['chiusura_le'])}",
        f"Δ vs apertura: € {delta_eur:+,.2f}{_fmt_le_opt(delta_le)}",
        f"# movimenti:  {s['n_movimenti']}",
    ])
    if s["n_movimenti"] == 0:
        lines.append("")
        lines.append("(nessun movimento oggi — saldo invariato)")
    return "\n".join(lines)


async def send_daily_cash_report(context: ContextTypes.DEFAULT_TYPE):
    """JOB SCHEDULATO: invia il report cassa contabile a Omar ogni sera.

    Eseguito da JobQueue alle 20:00 ora del Cairo. NON manda al gruppo:
    SOLO chat privata di Omar (telegram_user_id = chat_id privato col bot).
    """
    from datetime import date as _date
    target_chat = _fetch_proprieta_user_id()
    if not target_chat:
        print("[daily_report] proprieta user non trovato in telegram_users → skip")
        return

    target_date = _date.today()  # data di esecuzione = oggi
    snapshot = _compute_cassa_snapshot(
        SNAPSHOT_CASSA_CODE, target_date.isoformat()
    )
    if snapshot is None:
        print("[daily_report] errore calcolo snapshot → skip")
        return

    text = _format_snapshot_text(snapshot, target_date, "Cassa Contabile")
    try:
        await context.bot.send_message(chat_id=target_chat, text=text)
        print(f"[daily_report] inviato a chat {target_chat}")
    except Exception as e:
        # Caso tipico: Omar non ha mai scritto in privato al bot
        # ("Forbidden: bot can't initiate conversation with a user")
        print(f"[daily_report] send a {target_chat} fallito: {e}")


async def cmd_report_cassa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/report_cassa — invia lo snapshot di cassa_contabile al volo (per test
    o per averlo prima delle 20:00). Solo proprieta/contabile.
    """
    tg_user = await _require_admin(update)
    if not tg_user:
        return
    from datetime import date as _date
    target_date = _date.today()
    snapshot = _compute_cassa_snapshot(
        SNAPSHOT_CASSA_CODE, target_date.isoformat()
    )
    if snapshot is None:
        await update.message.reply_text(
            "❌ Errore nel calcolare lo snapshot. Riprova fra poco."
        )
        return
    text = _format_snapshot_text(snapshot, target_date, "Cassa Contabile")
    await update.message.reply_text(text)


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
    """Role-aware intro message — users see only the commands relevant to them."""
    upsert_telegram_user(update.effective_user)
    tg_user = get_telegram_user(update.effective_user.id)
    role = (tg_user or {}).get("role")
    display_name = (tg_user or {}).get("display_name") or "amico"

    # --- Not yet mapped by Omar: show a minimal message ---
    if not tg_user or not tg_user.get("account_code"):
        await update.message.reply_text(
            f"👋 Ciao {display_name}! Ti ho registrato nel sistema.\n\n"
            "Omar deve ancora associarti a un conto. Quando te lo conferma "
            "torna qui e scrivi /start per vedere i comandi che puoi usare.\n\n"
            "🔎 /whoami — controlla il tuo stato"
        )
        return

    # --- Contabile (Amr): can now do both (economic events AND transfers) ---
    if role == "contabile":
        await update.message.reply_text(
            f"👔 Ciao {display_name}! Sei registrato come *contabile*.\n\n"
            "*Registrare incassi/spese (come le guide):*\n"
            "`+200 tour piramidi` → incasso in euro\n"
            "`+500 EGP commissione hotel` → incasso in lire\n"
            "`-50 pranzo clienti` → spesa in euro\n"
            "`-1000 LE biglietto museo` → spesa in lire\n\n"
            "*Comandi per trasferimenti fra casse:*\n"
            "`/raccolgo 200 saif` — ricevi soldi da una guida\n"
            "`/verso 2000 omar` — consegni soldi alla proprieta\n"
            "`/verso 500 banca` — versi in banca\n\n"
            "🔎 /whoami — controlla il tuo stato",
            parse_mode="Markdown",
        )
        return

    # --- Guida: only the +/- syntax for economic events ---
    if role == "guida":
        await update.message.reply_text(
            f"👋 Ciao {display_name}! Sei registrato come *guida*.\n\n"
            "*Come registrare un evento:*\n"
            "`+200 tour piramidi` → incasso in euro\n"
            "`+500 EGP commissione` → incasso in lire egiziane\n"
            "`-50 cammello` → spesa in euro\n"
            "`-1000 LE biglietto museo` → spesa in lire\n\n"
            "Quando passi i soldi ad Amr, lui scrive /raccolgo dalla sua parte "
            "e i tuoi saldi si aggiornano da soli.\n\n"
            "🔎 /whoami — controlla il tuo stato",
            parse_mode="Markdown",
        )
        return

    # --- Manager: stessa cosa di guida ma label diversa (richiesta Omar
    # 27/04/2026 — ruolo cosmetico per riconoscimento gerarchico,
    # funzionalmente identico a guida).
    if role == "manager":
        await update.message.reply_text(
            f"👋 Ciao {display_name}! Sei registrato come *manager*.\n\n"
            "*Come registrare un evento:*\n"
            "`+200 tour piramidi` → incasso in euro\n"
            "`+500 EGP commissione` → incasso in lire egiziane\n"
            "`-50 cammello` → spesa in euro\n"
            "`-1000 LE biglietto museo` → spesa in lire\n\n"
            "Quando passi i soldi ad Amr, lui scrive /raccolgo dalla sua parte "
            "e i tuoi saldi si aggiornano da soli.\n\n"
            "🔎 /whoami — controlla il tuo stato",
            parse_mode="Markdown",
        )
        return

    # --- Proprieta (Omar): can do both (logs economic events AND sees all) ---
    if role == "proprieta":
        await update.message.reply_text(
            f"🏠 Ciao {display_name}! Sei registrato come *proprieta*.\n\n"
            "*Registrare spese/incassi fatti da te:*\n"
            "`+200 commissione hotel` → incasso\n"
            "`-1000 LE pranzo beduino` → spesa in lire\n\n"
            "*Comandi contabili* (se vuoi registrare movimenti manuali):\n"
            "`/raccolgo <importo> <guida>`\n"
            "`/verso <importo> <destinazione>`\n\n"
            "🔎 /whoami — controlla il tuo stato",
            parse_mode="Markdown",
        )
        return

    # --- Unknown role (shouldn't happen): generic fallback ---
    await update.message.reply_text(
        f"👋 Ciao {display_name}! Il tuo ruolo è '{role}' — contatta Omar per info."
    )


# ============================================================
# Main
# ============================================================
# ============================================================
# Telegram bot commands (the "/" dropdown)
# ============================================================
# Telegram mostra il dropdown dei comandi solo se il bot ha chiamato
# `setMyCommands`. Senza quella chiamata i comandi *funzionano* lo stesso
# (gli handler ci sono) ma l'utente non vede l'autocomplete quando scrive
# "/" → confondente.
#
# Strategia per ruolo:
#   - Default scope (qualsiasi utente, incl. guide): /start, /whoami,
#     /raccolgo, /verso (questi due sono aperti a chiunque abbia un
#     account_code, vedi _require_account_user).
#   - Per-chat scope (per ogni admin = contabile o proprieta): aggiungo
#     anche /cambia, /paga_fornitore, /report_cassa.
#
# Limite noto: BotCommandScopeChat funziona solo se il bot ha gia' avuto
# almeno un'interazione con quell'utente (altrimenti Telegram restituisce
# "chat not found"). Se aggiungiamo un nuovo contabile dopo aver gia'
# fatto deploy, dovra' fare /start UNA volta e poi serve un riavvio del
# bot perche' veda i comandi admin extra. Per la rosa attuale (Amr + Omar)
# entrambi hanno gia' interagito → nessun problema.
GUIDA_COMMANDS = [
    BotCommand("start", "Istruzioni e info ruolo"),
    BotCommand("raccolgo", "Incassa soldi da un altro utente"),
    BotCommand("verso", "Versa soldi a un altro utente o banca"),
    BotCommand("whoami", "Vedi chi sei nel sistema"),
]
ADMIN_COMMANDS = [
    BotCommand("start", "Istruzioni e info ruolo"),
    BotCommand("raccolgo", "Incassa soldi da una guida"),
    BotCommand("verso", "Versa soldi a proprieta o banca"),
    BotCommand("cambia", "Cambio valuta EUR → EGP"),
    BotCommand("paga_fornitore", "Registra pagamento fornitore (Shamandura, ecc.)"),
    BotCommand("report_cassa", "Snapshot cassa contabile di oggi"),
    BotCommand("whoami", "Vedi chi sei nel sistema"),
]


def _fetch_admin_user_ids() -> list[int]:
    """Ritorna i telegram_user_id di chi ha role contabile o proprieta.
    Usato a startup per impostare la lista comandi 'admin' su quei chat
    specifici. Se Supabase non e' configurato o la query fallisce, ritorna
    lista vuota e si usa solo lo scope di default."""
    if not _sb_configured():
        return []
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/telegram_users",
            headers=_sb_headers(),
            params={
                "select": "telegram_user_id",
                "role": "in.(contabile,proprieta)",
            },
            timeout=10,
        )
        r.raise_for_status()
        return [int(row["telegram_user_id"]) for row in r.json()]
    except Exception as e:
        print(f"[startup] fetch admin users fallito: {e}")
        return []


async def _on_startup(application) -> None:
    """post_init hook: registra i comandi su Telegram cosi' compaiono nel
    dropdown quando l'utente scrive '/'. Va eseguito UNA volta a ogni
    avvio del bot — la registrazione e' idempotente lato Telegram."""
    bot = application.bot
    # 1. Default per tutti (guide incluse, e nuovi utenti non ancora mappati)
    try:
        await bot.set_my_commands(GUIDA_COMMANDS)
        print(f"[startup] set_my_commands default OK ({len(GUIDA_COMMANDS)} cmds)")
    except Exception as e:
        print(f"[startup] set_my_commands default FAIL: {e}")
        return  # se il default fallisce, non ha senso provare gli scope per-chat

    # 1b. Gruppi: tutti i membri vedono i comandi admin nel menu. Motivo:
    # BotCommandScopeChat(chat_id=user_id) funziona solo in chat private,
    # non nei gruppi. Per differenziare per-utente in un gruppo servirebbe
    # BotCommandScopeChatMember(group_id, user_id) e quindi tenere traccia
    # dei group_id → complessita' in piu' per un beneficio solo cosmetico:
    # gli handler admin (cambia/paga_fornitore/report_cassa) filtrano per
    # ruolo, quindi se una guida clicca il bot risponde "non autorizzato".
    try:
        await bot.set_my_commands(
            ADMIN_COMMANDS, scope=BotCommandScopeAllGroupChats()
        )
        print(f"[startup] set_my_commands group-chats OK ({len(ADMIN_COMMANDS)} cmds)")
    except Exception as e:
        print(f"[startup] set_my_commands group-chats FAIL: {e}")

    # 2. Lista estesa per ogni admin (contabile / proprieta)
    # Nota: questo scope vale per la chat privata admin↔bot. Nei gruppi
    # e' gia' stato gestito sopra con AllGroupChats.
    admin_ids = _fetch_admin_user_ids()
    print(f"[startup] admin users trovati: {admin_ids}")
    for uid in admin_ids:
        try:
            await bot.set_my_commands(
                ADMIN_COMMANDS,
                scope=BotCommandScopeChat(chat_id=uid),
            )
            print(f"[startup] set_my_commands admin {uid} OK")
        except Exception as e:
            # Caso tipico: l'utente non ha mai scritto al bot in privato →
            # "Bad Request: chat not found". Non e' fatale: vedra' i comandi
            # default e potra' comunque digitare /raccolgo /verso a mano.
            print(f"[startup] set_my_commands admin {uid} skip ({e})")


def main():
    # Fail-fast su env vars mancanti (errore esplicito invece di crash
    # criptico al primo messaggio). Vedi validate_environment() in cima.
    validate_environment()

    # Carica i conti economici attivi da Supabase e costruisce lo schema
    # del tool register_transaction. Crash con messaggio chiaro se il fetch
    # fallisce — meglio non avviare che girare con un enum vuoto/stale.
    init_claude_tool()

    print("🚀 Athos Bot (double-entry) avviato...")
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(_on_startup)
        .build()
    )
    app.add_handler(CommandHandler("start", start_command))

    # /raccolgo — flow conversazionale (entry → optional amount → keyboard).
    # Va REGISTRATO PRIMA del MessageHandler globale, altrimenti il numero
    # scritto dopo "Quanto raccogli?" verrebbe intercettato da handle_message
    # e interpretato come transazione libera.
    raccolgo_conv = ConversationHandler(
        entry_points=[CommandHandler("raccolgo", cmd_raccolgo)],
        states={
            RACC_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, racc_on_amount)],
        },
        fallbacks=[CommandHandler("annulla", racc_cancel)],
        per_user=True,
        per_chat=True,
    )
    app.add_handler(raccolgo_conv)
    app.add_handler(CallbackQueryHandler(racc_on_callback, pattern=r"^racc[:_]"))

    # /verso — stesso pattern di /raccolgo (flow conversazionale + tastiera).
    verso_conv = ConversationHandler(
        entry_points=[CommandHandler("verso", cmd_verso)],
        states={
            VERSO_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, verso_on_amount)],
        },
        fallbacks=[CommandHandler("annulla", verso_cancel)],
        per_user=True,
        per_chat=True,
    )
    app.add_handler(verso_conv)
    app.add_handler(CallbackQueryHandler(verso_on_callback, pattern=r"^verso[:_]"))

    # /cambia — flow conversazionale (eur → egp → conferma con bottoni).
    # 3 stati: EUR amount, EGP amount, confirm (callback con bottoni inline).
    # La conferma e' un CallbackQueryHandler dentro il flow stesso (non
    # globale come per /raccolgo) perche' qui dobbiamo accedere a
    # context.user_data che e' per-conversazione.
    cambia_conv = ConversationHandler(
        entry_points=[CommandHandler("cambia", cmd_cambia)],
        states={
            CAMBIA_EUR_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, cambia_on_eur)],
            CAMBIA_EGP_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, cambia_on_egp)],
            CAMBIA_CONFIRM:    [CallbackQueryHandler(cambia_on_callback, pattern=r"^cambia_(confirm|cancel)$")],
        },
        fallbacks=[CommandHandler("annulla", cambia_cancel)],
        per_user=True,
        per_chat=True,
    )
    app.add_handler(cambia_conv)

    app.add_handler(CommandHandler("report_cassa", cmd_report_cassa))
    app.add_handler(CommandHandler("whoami", cmd_whoami))

    # /paga_fornitore — flow conversazionale (entry → step → confirm).
    # Va REGISTRATO PRIMA del MessageHandler globale, altrimenti i messaggi
    # numerici (importo) verrebbero intercettati da handle_message e
    # interpretati come transazioni libere.
    paga_fornitore_conv = ConversationHandler(
        entry_points=[CommandHandler("paga_fornitore", cmd_paga_fornitore)],
        states={
            PAY_SUPPLIER:    [CallbackQueryHandler(pf_on_supplier, pattern=r"^pf_(supp|cancel)")],
            PAY_AMOUNT:      [MessageHandler(filters.TEXT & ~filters.COMMAND, pf_on_amount)],
            PAY_CASSA:       [CallbackQueryHandler(pf_on_cassa,    pattern=r"^pf_(cassa|cancel)")],
            PAY_CLIENT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, pf_on_client_name)],
            PAY_CONFIRM:     [CallbackQueryHandler(pf_on_confirm,  pattern=r"^pf_(confirm|cancel)")],
        },
        fallbacks=[CommandHandler("annulla", pf_cancel)],
        # Per-user: ogni utente ha il suo stato indipendente
        per_user=True,
        per_chat=True,
    )
    app.add_handler(paga_fornitore_conv)

    # ─────────────────────────────────────────────────────────────────
    # JobQueue: schedule daily cash report alle 20:00 ora del Cairo,
    # invio in chat PRIVATA al proprieta (Omar). Richiede l'extra
    # `python-telegram-bot[job-queue]` in requirements.txt (installa
    # APScheduler sotto il cofano).
    # ─────────────────────────────────────────────────────────────────
    try:
        from datetime import time as _time
        from zoneinfo import ZoneInfo
        job_queue = app.job_queue
        if job_queue is None:
            print("⚠️ JobQueue non disponibile: installa python-telegram-bot[job-queue]")
        else:
            job_queue.run_daily(
                send_daily_cash_report,
                time=_time(
                    hour=DAILY_REPORT_HOUR_CAIRO, minute=0,
                    tzinfo=ZoneInfo(CAIRO_TZ_NAME),
                ),
                name="daily_cash_report",
            )
            print(
                f"📅 Report cassa contabile schedulato ogni giorno alle "
                f"{DAILY_REPORT_HOUR_CAIRO:02d}:00 ({CAIRO_TZ_NAME})"
            )
    except Exception as e:
        print(f"⚠️ Errore setup daily report job: {e}")

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()


if __name__ == "__main__":
    main()
