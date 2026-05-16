"""Transaction write orchestration with injectable side effects."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional

from transaction_core import amount_value, build_economic_lines

InsertJournalEntry = Callable[..., Optional[str]]
SaveToSheets = Callable[[dict[str, Any]], bool]


def write_one_transaction(
    tx: dict[str, Any],
    cassa_account: str,
    telegram_user_id: int,
    user_first_name: str,
    *,
    insert_journal_entry: InsertJournalEntry,
    save_to_sheets: SaveToSheets,
) -> tuple[str | None, str]:
    """Write one parsed transaction and mirror it to Sheets after DB success."""
    tipo = tx.get("tipo")
    account_code = tx.get("account_code") or (
        "ricavi_escursioni" if tipo == "entrata" else "costi_altri"
    )
    descrizione = tx.get("descrizione", "")
    currency = tx.get("currency", "EUR")
    importo = amount_value(tx.get("importo"))

    lines = build_economic_lines(
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
        save_to_sheets(
            {
                "guida": (user_first_name or "")[:8],
                "tipo": tipo,
                "importo_eur": importo if currency == "EUR" else "",
                "importo_le": importo if currency == "EGP" else "",
                "descrizione": descrizione,
            }
        )
    return entry_id, descrizione
