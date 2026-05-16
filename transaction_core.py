"""Pure transaction helpers for Athos Telegram accounting flows.

This module deliberately has no Telegram, Supabase, or Anthropic dependency:
it is the small core that can be tested without starting the bot.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

SUSPECT_EUR_THRESHOLD = 1900
SUSPECT_LE_THRESHOLD = 60_000
FALLBACK_ACCOUNTS = {"costi_altri", "ricavi_escursioni"}
TRANSACTION_START_PREFIXES = (
    "spesa",
    "uscita",
    "out ",
    "pagamento",
    "costo",
    "in ",
    "incasso",
    "entrata",
    "pagato",
)

_KEYWORD_TOKENS = (
    r"entrata|uscita|incasso|spesa|in|out|pagato|ricevuto|speso|costo|pagamento"
)
_DETECT_PATTERN = re.compile(
    rf"""(?ix)
        (?:^|\s)
        (?:
            [+\-]\s*\d
            |
            (?:{_KEYWORD_TOKENS})\b [^\n+\-]{{0,30}}? \d
        )
    """,
)
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


def count_transaction_starts(text: str) -> int:
    """Count transaction starts in free text."""
    return len(_DETECT_PATTERN.findall(text or ""))


def looks_like_transaction_command(text: str) -> bool:
    """Return whether the message prefix is allowed to reach the LLM parser."""
    stripped = (text or "").strip()
    lowered = stripped.lower()
    return (
        stripped.startswith("+")
        or stripped.startswith("-")
        or any(lowered.startswith(prefix) for prefix in TRANSACTION_START_PREFIXES)
    )


def split_transactions(text: str) -> list[str]:
    """Split a message into one chunk per detected transaction."""
    if not text:
        return []
    starts = [match.start() for match in _SPLIT_PATTERN.finditer(text)]
    if not starts:
        return [text.strip()]

    pieces = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        chunk = text[start:end].strip()
        if chunk:
            pieces.append(chunk)
    return pieces


def fallback_transaction_from_text(text: str) -> dict[str, Any]:
    """Build a low-confidence fallback transaction for an unparsed chunk."""
    piece = (text or "").strip()
    piece_lower = piece.lstrip().lower()
    if re.match(r"^(\+|entrata|incasso|in\s|ricevuto)", piece_lower):
        fallback_tipo = "entrata"
        fallback_account = "ricavi_escursioni"
    else:
        fallback_tipo = "uscita"
        fallback_account = "costi_altri"
    return {
        "tipo": fallback_tipo,
        "currency": "EUR",
        "importo": 0,
        "descrizione": piece[:60],
        "account_code": fallback_account,
        "confidence": "low",
    }


def amount_value(value: Any) -> float:
    try:
        return float(value) if value not in (None, "", "null") else 0.0
    except (TypeError, ValueError):
        return 0.0


def build_economic_lines(
    tipo: str,
    cassa_account: str,
    economic_account: str,
    importo: Any,
    currency: str,
) -> list[dict[str, Any]]:
    """Build the two balanced lines for income or expense transactions."""
    amount = amount_value(importo)
    if amount <= 0 or currency not in ("EUR", "EGP"):
        return []

    if tipo == "entrata":
        return [
            {"account_code": cassa_account, "dare": amount, "avere": 0, "currency": currency},
            {"account_code": economic_account, "dare": 0, "avere": amount, "currency": currency},
        ]

    return [
        {"account_code": economic_account, "dare": amount, "avere": 0, "currency": currency},
        {"account_code": cassa_account, "dare": 0, "avere": amount, "currency": currency},
    ]


def is_high_amount(tx: dict[str, Any]) -> tuple[bool, str]:
    """Return whether a transaction amount should trigger a confirmation preview."""
    importo = amount_value(tx.get("importo"))
    currency = tx.get("currency", "EUR")
    if currency == "EUR" and importo > SUSPECT_EUR_THRESHOLD:
        return True, f"importo elevato (€{importo:.0f})"
    if currency == "EGP" and importo > SUSPECT_LE_THRESHOLD:
        return True, f"importo elevato ({importo:.0f} LE)"
    return False, ""


def is_suspect(tx: dict[str, Any]) -> tuple[bool, str]:
    """Return whether a transaction should be highlighted in a multi-preview."""
    confidence = (tx.get("confidence") or "").strip().lower()
    if confidence == "low":
        return True, "Claude segnala incertezza"

    high, reason = is_high_amount(tx)
    if high:
        return True, reason

    descrizione = (tx.get("descrizione") or "").strip()
    account_code = tx.get("account_code") or ""
    if account_code in FALLBACK_ACCOUNTS and len(descrizione) < 4:
        snippet = descrizione if descrizione else "(vuota)"
        return True, f'"{snippet}" poco chiaro'

    return False, ""


def format_amount(tx: dict[str, Any]) -> str:
    """Format an amount as '+90 EUR' or '-1000 LE'."""
    sign = "+" if tx.get("tipo") == "entrata" else "-"
    importo = amount_value(tx.get("importo"))
    currency = tx.get("currency", "EUR")
    if importo <= 0:
        return f"{sign}? "
    label = "EUR" if currency == "EUR" else "LE"
    return f"{sign}{int(importo)} {label}"


def format_preview(transactions: list[dict[str, Any]]) -> str:
    """Build the plain-text multi-transaction preview shown in Telegram."""
    n = len(transactions)
    lines = [f"📝 Ho trovato {n} transazion{'i' if n != 1 else 'e'}. Registro?", ""]
    for index, tx in enumerate(transactions, start=1):
        suspect, reason = is_suspect(tx)
        flag = "⚠️" if suspect else "✅"
        amount = format_amount(tx)
        descr = (tx.get("descrizione") or "").strip() or "(senza descrizione)"
        account = tx.get("account_code") or "?"
        lines.append(f"{index}. {flag}  {amount}  {descr}  → {account}")
        if suspect and reason:
            lines.append(f"     ↑ {reason}")
    lines.append("")
    lines.append("Rispondi:")
    lines.append('• "ok" → registro tutte')
    lines.append('• "no" → annullo')
    lines.append('• "solo 1,2,3" → registro solo quelle (numeri separati da virgola)')
    return "\n".join(lines)


def format_registration_result(
    tx: dict[str, Any],
    entry_id: str | None,
    descrizione: str,
) -> str:
    """Build one result line after attempting to write a transaction."""
    mark = "✅" if entry_id else "❌"
    return f"{mark} {format_amount(tx)} {descrizione or '(senza descr)'}"


def format_registration_summary(results: list[str]) -> str:
    """Build the reply shown after a confirmed multi-transaction write."""
    if len(results) <= 1:
        return "\n".join(results)
    n_ok = sum(1 for result in results if result.startswith("✅"))
    return f"💾 Registrate {n_ok}/{len(results)} transazioni:\n\n" + "\n".join(results)


def format_single_confirmation(
    tx: dict[str, Any],
    display_name: str,
    *,
    now: datetime | None = None,
) -> str:
    """Build the confirmation text for a single saved transaction."""
    tipo = tx.get("tipo")
    emoji = "💚" if tipo == "entrata" else "🔴"
    importo = amount_value(tx.get("importo"))
    currency = tx.get("currency", "EUR")
    importo_str = f"€{importo:g}" if currency == "EUR" else f"{importo:g} LE"
    account_code = tx.get("account_code") or (
        "ricavi_escursioni" if tipo == "entrata" else "costi_altri"
    )
    descrizione = tx.get("descrizione", "")
    stamp = now or datetime.now()
    return (
        f"{emoji} Registrato nel giornale!\n\n"
        f"📝 {descrizione}\n"
        f"💶 {importo_str}\n"
        f"🏷️ {account_code}\n"
        f"👤 {display_name}\n"
        f"📅 {stamp.strftime('%d/%m/%Y')}"
    )


def parse_confirmation(text: str, n_transactions: int) -> tuple[str, list[int] | None]:
    """Parse the user's answer to a multi-transaction preview."""
    if not text:
        return "unknown", None
    normalized = text.strip().lower()

    if normalized in {"ok", "si", "sì", "yes", "conferma", "confermo", "y"}:
        return "all", None
    if normalized in {"no", "annulla", "cancel", "n"}:
        return "none", None

    had_solo_prefix = bool(re.match(r"^solo\s+", normalized))
    cleaned = re.sub(r"^solo\s+", "", normalized)
    parts = [part for part in re.split(r"[,\s]+", cleaned.strip()) if part]
    if parts and all(part.isdigit() for part in parts):
        nums = sorted({int(part) for part in parts})
        if nums and all(1 <= num <= n_transactions for num in nums):
            if len(nums) == 1 and not had_solo_prefix and n_transactions > 1:
                return "unknown", None
            return "subset", [num - 1 for num in nums]
    return "unknown", None


def confirmation_help_text(n: int) -> str:
    return (
        "🤔 Non ho capito. Rispondi con:\n"
        '• "ok" → registro tutte\n'
        '• "no" → annullo\n'
        f'• "solo 1,2" → registro solo quelle (numeri da 1 a {n}, '
        "separati da virgola)"
    )
