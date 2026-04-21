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
import requests
from datetime import datetime, timedelta
from telegram import (
    Update,
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeAllGroupChats,
)
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
- ricavi_escursioni → TUTTI gli incassi da clienti per tour ed escursioni
  (piramidi, luxor, assuan, abu simbel, deserto, mare, quad, cammello, ecc.).
  DEFAULT: usa questo anche quando la descrizione contiene solo nome cliente
  e/o hotel senza specificare l'attività.
- ricavi_commissioni → commissioni da partner, hotel, negozi (solo se la
  descrizione contiene esplicitamente "commissione", "provvigione",
  "commission" o simili).

COSTI (quando tipo=uscita):
- costi_ristoranti → pranzi, cene con clienti, ristoranti
- costi_escursioni_vari → TUTTE le spese sul momento durante un'escursione:
  affitto moto/quad/barche/feluche/motoscafi, cammelli e altri animali,
  MANCE a driver/barcaioli/cammellieri/motoristi, attrezzatura piccola
  (snorkeling, pinne, maschere), acqua/snack per clienti durante il tour,
  piccoli pagamenti al volo al tempio/sito (guide extra, custodi).
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
- costi_altri → tutto il resto delle spese

NOTA "commissione" — può essere sia entrata che uscita:
- se tipo=entrata → ricavi_commissioni (es. guida vende foto e prende %)
- se tipo=uscita → costi_escursioni_vari (mance/commissioni ai driver durante tour)

REGOLE DESCRIZIONE:
- Tutto il testo dopo segno/cifra/valuta va in "descrizione"

ESEMPI:
"+200 tour piramidi" → TRANSACTION:{"tipo":"entrata","importo_eur":200,"importo_le":"","descrizione":"tour piramidi","account_code":"ricavi_escursioni"}
"+150 escursione deserto" → TRANSACTION:{"tipo":"entrata","importo_eur":150,"importo_le":"","descrizione":"escursione deserto","account_code":"ricavi_escursioni"}
"+300 Mario Rossi Hotel Sunrise" → TRANSACTION:{"tipo":"entrata","importo_eur":300,"importo_le":"","descrizione":"Mario Rossi Hotel Sunrise","account_code":"ricavi_escursioni"}
"-50 pranzo clienti" → TRANSACTION:{"tipo":"uscita","importo_eur":50,"importo_le":"","descrizione":"pranzo clienti","account_code":"costi_ristoranti"}
"-300 cammello" → TRANSACTION:{"tipo":"uscita","importo_eur":300,"importo_le":"","descrizione":"cammello","account_code":"costi_escursioni_vari"}
"-50 mancia motorista" → TRANSACTION:{"tipo":"uscita","importo_eur":50,"importo_le":"","descrizione":"mancia motorista","account_code":"costi_escursioni_vari"}
"-20 acqua clienti" → TRANSACTION:{"tipo":"uscita","importo_eur":20,"importo_le":"","descrizione":"acqua clienti","account_code":"costi_escursioni_vari"}
"-100 LE snorkeling" → TRANSACTION:{"tipo":"uscita","importo_eur":"","importo_le":100,"descrizione":"snorkeling","account_code":"costi_escursioni_vari"}
"-1000 LE guida canyon" → TRANSACTION:{"tipo":"uscita","importo_eur":"","importo_le":1000,"descrizione":"guida canyon","account_code":"costi_guide_esterne"}
"-500 LE biglietto valle re" → TRANSACTION:{"tipo":"uscita","importo_eur":"","importo_le":500,"descrizione":"biglietto valle re","account_code":"costi_ingressi"}
"+100 commissione foto" → TRANSACTION:{"tipo":"entrata","importo_eur":100,"importo_le":"","descrizione":"commissione foto","account_code":"ricavi_commissioni"}
"-30 commissione driver" → TRANSACTION:{"tipo":"uscita","importo_eur":30,"importo_le":"","descrizione":"commissione driver","account_code":"costi_escursioni_vari"}
"entrata 100 commissione foto" → TRANSACTION:{"tipo":"entrata","importo_eur":100,"importo_le":"","descrizione":"commissione foto","account_code":"ricavi_commissioni"}
"uscita 30 acqua clienti" → TRANSACTION:{"tipo":"uscita","importo_eur":30,"importo_le":"","descrizione":"acqua clienti","account_code":"costi_escursioni_vari"}

Rispondi SOLO con il JSON nel formato:
TRANSACTION:{"tipo":"...","importo_eur":...,"importo_le":"...","descrizione":"...","account_code":"..."}

CAMPO OPZIONALE "needs_review" (bool): aggiungilo a true SOLO se sei incerto
sulla classificazione (descrizione ambigua, parola sconosciuta, ecc.).
Esempio: "+90 Davide domina" → TRANSACTION:{"tipo":"entrata","importo_eur":90,"importo_le":"","descrizione":"Davide domina","account_code":"ricavi_escursioni","needs_review":true}

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
# Multi-transaction parsing & preview helpers
# ============================================================
# Token che marca l'inizio di una transazione: +/- seguiti da numero, oppure
# parole chiave (entrata/uscita/incasso/spesa) seguite — anche con altre
# parole in mezzo — da un numero. Usato sia per il conteggio (detection) sia
# per lo split. Case-insensitive.
_KEYWORD_TOKENS = r"entrata|uscita|incasso|spesa"
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


def _parse_claude_transaction(response: str) -> dict | None:
    """Estrae il dict da una risposta Claude TRANSACTION:{...}. None se invalida."""
    if not response or not response.startswith("TRANSACTION:"):
        return None
    try:
        return json.loads(response.replace("TRANSACTION:", "").strip())
    except Exception as e:
        print(f"parse_claude_transaction error: {e} -- raw: {response[:200]}")
        return None


def _is_suspect(tx: dict) -> tuple[bool, str]:
    """Heuristic: è una transazione 'sospetta'? Restituisce (bool, motivo).

    Triggers:
      - Claude ha messo needs_review=true
      - importo > soglia (1900 EUR o 60000 LE)
      - account_code è il fallback (costi_altri/ricavi_escursioni) E descrizione
        cortissima o vuota — il classificatore probabilmente non ha capito
    """
    # Truthy check: Claude può restituire bool true OR string "true"/"True".
    # `is True` era troppo stretto e ignorava la stringa.
    nr = tx.get("needs_review")
    if nr is True or (isinstance(nr, str) and nr.strip().lower() == "true"):
        return True, "Claude segnala incertezza"

    def _amt(v):
        try:
            return float(v) if v not in (None, "", "null") else 0.0
        except (TypeError, ValueError):
            return 0.0

    eur = _amt(tx.get("importo_eur"))
    le = _amt(tx.get("importo_le"))
    if eur > SUSPECT_EUR_THRESHOLD:
        return True, f"importo elevato (€{eur:.0f})"
    if le > SUSPECT_LE_THRESHOLD:
        return True, f"importo elevato ({le:.0f} LE)"

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
    eur = tx.get("importo_eur")
    le = tx.get("importo_le")
    # Tratto 0 come "non specificato" così cade sull'altra valuta — evita "+0 EUR"
    # quando in realtà l'importo è in LE.
    def _has_amt(v):
        if v in (None, "", "null"):
            return False
        try:
            return float(v) > 0
        except (TypeError, ValueError):
            return False
    if _has_amt(eur):
        try:
            return f"{sign}{int(float(eur))} EUR"
        except (TypeError, ValueError):
            return f"{sign}{eur} EUR"
    if _has_amt(le):
        try:
            return f"{sign}{int(float(le))} LE"
        except (TypeError, ValueError):
            return f"{sign}{le} LE"
    return f"{sign}? "


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

    lines = _build_economic_lines(
        tipo=tipo,
        cassa_account=cassa_account,
        economic_account=account_code,
        importo_eur=tx.get("importo_eur"),
        importo_le=tx.get("importo_le"),
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
        # Mirror to Sheets — stesso payload del flusso single
        _save_to_sheets({
            "guida": (user_first_name or "")[:8],
            "tipo": tipo,
            "importo_eur": tx.get("importo_eur", ""),
            "importo_le": tx.get("importo_le", ""),
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
    eur = tx.get("importo_eur", "")
    le = tx.get("importo_le", "")
    if eur and str(eur) != "":
        importo_str = f"€{eur}"
    else:
        importo_str = f"{le} LE"
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

    # 3. If contabile: redirect to slash commands
    if role == "contabile":
        await update.message.reply_text(
            "👔 Come contabile usi comandi diversi:\n\n"
            "`/raccolgo 200 saif` — ricevi da una guida\n"
            "`/verso 2000 omar` — consegni alla proprieta",
            parse_mode="Markdown",
        )
        return

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
            response = await ask_claude(piece)
            tx = _parse_claude_transaction(response)
            if tx is None:
                # Una sotto-transazione non parsata → segnaliamo e continuiamo
                # mettendola comunque in coda con flag suspect.
                # Il fallback per "tipo" guarda sia il segno che le keyword
                # (entrata/incasso → entrata, altrimenti → uscita).
                piece_lower = piece.lstrip().lower()
                if re.match(r"^(\+|entrata|incasso|in\s)", piece_lower):
                    fallback_tipo = "entrata"
                else:
                    fallback_tipo = "uscita"
                transactions.append({
                    "tipo": fallback_tipo,
                    "importo_eur": "",
                    "importo_le": "",
                    "descrizione": piece[:60],
                    "account_code": "costi_altri" if fallback_tipo == "uscita" else "ricavi_escursioni",
                    "needs_review": True,
                })
            else:
                transactions.append(tx)

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
    response = await ask_claude(text)

    if not response.startswith("TRANSACTION:"):
        await update.message.reply_text(response)
        return

    tx = _parse_claude_transaction(response)
    if tx is None:
        await update.message.reply_text("❌ Errore parsing risposta AI.")
        return

    # Se l'unica transazione è "sospetta" → passa comunque dal preview
    suspect, _reason = _is_suspect(tx)
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
            importo_eur=tx.get("importo_eur"),
            importo_le=tx.get("importo_le"),
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
# Slash commands — contabile + proprieta
# ============================================================
# /raccolgo e /verso funzionano sia per il contabile che per la proprieta.
# Il conto mittente/destinatario della scrittura contabile viene preso
# dall'account_code dell'utente che scrive:
#   - se scrive Amr (contabile) → cassa_contabile
#   - se scrive Omar (proprieta) → proprieta
# Cosi' entrambi possono registrare movimenti coerenti con dove stanno
# fisicamente i soldi (nel caso di Omar che incontra una guida in ufficio
# e raccoglie direttamente senza passare da Amr).
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


async def cmd_raccolgo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/raccolgo <importo> <guida>  — es. /raccolgo 200 saif

    Scrittura:
      dare  <conto di chi raccoglie>  <importo>
      avere cassa_guida_<guida>       <importo>

    Il conto mittente e' preso dall'account_code dell'utente che scrive:
      - contabile → cassa_contabile
      - proprieta → proprieta
    """
    tg_user = await _require_admin(update)
    if not tg_user:
        return

    receiver_account = tg_user["account_code"]
    receiver_name = tg_user.get("display_name") or "admin"

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
        description=f"Raccolta da {guida['display_name']} (a {receiver_name})",
        source="telegram",
        telegram_user_id=update.effective_user.id,
        lines=[
            {"account_code": receiver_account,
             "dare": importo, "avere": 0, "currency": "EUR"},
            {"account_code": guida["account_code"],
             "dare": 0, "avere": importo, "currency": "EUR"},
        ],
    )
    if not entry_id:
        await update.message.reply_text("❌ Errore nel registrare. Riprova.")
        return

    # No parse_mode — account codes contain underscores che rompono il Markdown.
    await update.message.reply_text(
        f"✅ Raccolto €{importo:.2f} da {guida['display_name']}\n\n"
        f"I soldi sono ora in {receiver_account} ({receiver_name})."
    )


async def cmd_verso(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/verso <importo> <destinazione>  — es. /verso 2000 omar | /verso 500 banca

    Scrittura:
      dare  <destinazione>         <importo>
      avere <conto di chi versa>   <importo>

    Il conto mittente e' preso dall'account_code dell'utente che scrive:
      - contabile → cassa_contabile
      - proprieta → proprieta
    Un admin non puo' versare a se stesso (no-op contabile).
    """
    tg_user = await _require_admin(update)
    if not tg_user:
        return

    sender_account = tg_user["account_code"]
    sender_name = tg_user.get("display_name") or "admin"

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

    # Omar che versa a proprieta (cioe' a se stesso) e' un no-op: blocca.
    if dest_account == sender_account:
        await update.message.reply_text(
            f"❌ Non puoi versare da {sender_account} a se stesso.\n"
            f"Se vuoi spostare soldi, scegli una destinazione diversa (es. banca)."
        )
        return

    entry_id = insert_journal_entry(
        description=f"Versamento a {dest_account} (da {sender_name})",
        source="telegram",
        telegram_user_id=update.effective_user.id,
        lines=[
            {"account_code": dest_account,
             "dare": importo, "avere": 0, "currency": "EUR"},
            {"account_code": sender_account,
             "dare": 0, "avere": importo, "currency": "EUR"},
        ],
    )
    if not entry_id:
        await update.message.reply_text("❌ Errore nel registrare. Riprova.")
        return

    # No parse_mode — account codes contain underscores.
    await update.message.reply_text(
        f"✅ Versati €{importo:.2f} a {dest_account}\n\n"
        f"{sender_account} ({sender_name}) aggiornato."
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

    # --- Contabile (Amr): only transfer slash commands ---
    if role == "contabile":
        await update.message.reply_text(
            f"👔 Ciao {display_name}! Sei registrato come *contabile*.\n\n"
            "*I tuoi comandi:*\n"
            "`/raccolgo 200 saif` — ricevi soldi da una guida\n"
            "`/verso 2000 omar` — consegni soldi alla proprieta\n"
            "`/verso 500 banca` — versi in banca (quando attiva)\n\n"
            "💡 Usa il nome della guida come appare in Telegram "
            "(es: saif, abozeidm, maja).\n\n"
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
#   - Default scope (qualsiasi utente, incl. guide): solo /start /whoami
#   - Per-chat scope (per ogni admin = contabile o proprieta): aggiungo
#     /raccolgo e /verso. Gli altri comandi rimangono nascosti ai non-admin
#     cosi' la guida non viene confusa da bottoni che non puo' usare.
#
# Limite noto: BotCommandScopeChat funziona solo se il bot ha gia' avuto
# almeno un'interazione con quell'utente (altrimenti Telegram restituisce
# "chat not found"). Se aggiungiamo un nuovo contabile dopo aver gia'
# fatto deploy, dovra' fare /start UNA volta e poi serve un riavvio del
# bot perche' veda /raccolgo /verso. Per la rosa attuale (Amr + Omar)
# entrambi hanno gia' interagito → nessun problema.
GUIDA_COMMANDS = [
    BotCommand("start", "Istruzioni e info ruolo"),
    BotCommand("whoami", "Vedi chi sei nel sistema"),
]
ADMIN_COMMANDS = [
    BotCommand("start", "Istruzioni e info ruolo"),
    BotCommand("raccolgo", "Incassa soldi da una guida"),
    BotCommand("verso", "Versa soldi a proprieta o banca"),
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

    # 1b. Gruppi: tutti i membri (anche le guide) vedono tutti e 4 i comandi.
    # Motivo: BotCommandScopeChat(chat_id=user_id) funziona solo in chat
    # private, non nei gruppi. Per differenziare per-utente in un gruppo
    # servirebbe BotCommandScopeChatMember(group_id, user_id) e quindi
    # tenere traccia dei group_id → complessita' in piu' per un beneficio
    # solo cosmetico: gli handler /raccolgo e /verso gia' filtrano per
    # ruolo, quindi se una guida clicca il bot risponde "non sei contabile".
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
    print("🚀 Athos Bot (double-entry) avviato...")
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(_on_startup)
        .build()
    )
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("raccolgo", cmd_raccolgo))
    app.add_handler(CommandHandler("verso", cmd_verso))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()


if __name__ == "__main__":
    main()
