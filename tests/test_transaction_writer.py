import unittest
from unittest.mock import Mock

from transaction_writer import write_one_transaction


class TransactionWriterTests(unittest.TestCase):
    def test_writes_journal_and_mirrors_to_sheets_after_success(self) -> None:
        insert = Mock(return_value="entry-1")
        sheets = Mock(return_value=True)

        entry_id, descrizione = write_one_transaction(
            {
                "tipo": "entrata",
                "account_code": "ricavi_escursioni",
                "descrizione": "tour piramidi",
                "currency": "EUR",
                "importo": 200,
            },
            "cassa_luca",
            123,
            "LucaLunghissimo",
            insert_journal_entry=insert,
            save_to_sheets=sheets,
        )

        self.assertEqual(entry_id, "entry-1")
        self.assertEqual(descrizione, "tour piramidi")
        insert.assert_called_once_with(
            description="tour piramidi",
            source="telegram",
            telegram_user_id=123,
            lines=[
                {
                    "account_code": "cassa_luca",
                    "dare": 200.0,
                    "avere": 0,
                    "currency": "EUR",
                },
                {
                    "account_code": "ricavi_escursioni",
                    "dare": 0,
                    "avere": 200.0,
                    "currency": "EUR",
                },
            ],
        )
        sheets.assert_called_once_with(
            {
                "guida": "LucaLung",
                "tipo": "entrata",
                "importo_eur": 200.0,
                "importo_le": "",
                "descrizione": "tour piramidi",
            }
        )

    def test_does_not_mirror_to_sheets_when_journal_write_fails(self) -> None:
        insert = Mock(return_value=None)
        sheets = Mock()

        entry_id, descrizione = write_one_transaction(
            {
                "tipo": "uscita",
                "account_code": "costi_ristoranti",
                "descrizione": "pranzo clienti",
                "currency": "EGP",
                "importo": "50",
            },
            "cassa_luca",
            123,
            "Luca",
            insert_journal_entry=insert,
            save_to_sheets=sheets,
        )

        self.assertIsNone(entry_id)
        self.assertEqual(descrizione, "pranzo clienti")
        insert.assert_called_once()
        sheets.assert_not_called()

    def test_invalid_transaction_does_not_call_side_effects(self) -> None:
        insert = Mock()
        sheets = Mock()

        entry_id, descrizione = write_one_transaction(
            {
                "tipo": "entrata",
                "account_code": "ricavi_escursioni",
                "descrizione": "zero",
                "currency": "EUR",
                "importo": 0,
            },
            "cassa_luca",
            123,
            "Luca",
            insert_journal_entry=insert,
            save_to_sheets=sheets,
        )

        self.assertIsNone(entry_id)
        self.assertEqual(descrizione, "zero")
        insert.assert_not_called()
        sheets.assert_not_called()

    def test_defaults_missing_account_by_transaction_type(self) -> None:
        insert = Mock(return_value="entry-2")
        sheets = Mock()

        write_one_transaction(
            {
                "tipo": "uscita",
                "descrizione": "varie",
                "currency": "EUR",
                "importo": 10,
            },
            "cassa_luca",
            123,
            "Luca",
            insert_journal_entry=insert,
            save_to_sheets=sheets,
        )

        lines = insert.call_args.kwargs["lines"]
        self.assertEqual(lines[0]["account_code"], "costi_altri")


if __name__ == "__main__":
    unittest.main()
