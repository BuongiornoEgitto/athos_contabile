from datetime import datetime
import unittest

from transaction_core import (
    build_economic_lines,
    confirmation_help_text,
    count_transaction_starts,
    fallback_transaction_from_text,
    format_amount,
    format_single_confirmation,
    format_registration_result,
    format_registration_summary,
    is_high_amount,
    is_suspect,
    looks_like_transaction_command,
    parse_confirmation,
    split_transactions,
)


class TransactionCoreTests(unittest.TestCase):
    def test_split_signed_transactions(self) -> None:
        text = "+200 tour piramidi -50 LE acqua +90 commissione foto"

        self.assertEqual(count_transaction_starts(text), 3)
        self.assertEqual(
            split_transactions(text),
            ["+200 tour piramidi", "-50 LE acqua", "+90 commissione foto"],
        )

    def test_split_keyword_transactions(self) -> None:
        text = "incasso Mario 120 hotel spesa acqua 50 LE"

        self.assertEqual(count_transaction_starts(text), 2)
        self.assertEqual(
            split_transactions(text),
            ["incasso Mario 120 hotel", "spesa acqua 50 LE"],
        )

    def test_transaction_command_prefix_filter(self) -> None:
        self.assertTrue(looks_like_transaction_command("+200 tour"))
        self.assertTrue(looks_like_transaction_command("-50 acqua"))
        self.assertTrue(looks_like_transaction_command("incasso Mario 120"))
        self.assertTrue(looks_like_transaction_command("spesa acqua 50"))
        self.assertFalse(looks_like_transaction_command("domani 3 escursioni"))
        self.assertFalse(looks_like_transaction_command("ciao Amr"))

    def test_fallback_transaction_from_text(self) -> None:
        income = fallback_transaction_from_text("+200 testo non parsato")
        self.assertEqual(income["tipo"], "entrata")
        self.assertEqual(income["account_code"], "ricavi_escursioni")
        self.assertEqual(income["confidence"], "low")

        expense = fallback_transaction_from_text("spesa strana senza tool")
        self.assertEqual(expense["tipo"], "uscita")
        self.assertEqual(expense["account_code"], "costi_altri")
        self.assertEqual(expense["importo"], 0)

    def test_parse_confirmation_commands(self) -> None:
        self.assertEqual(parse_confirmation("ok", 3), ("all", None))
        self.assertEqual(parse_confirmation("no", 3), ("none", None))
        self.assertEqual(parse_confirmation("solo 2", 3), ("subset", [1]))
        self.assertEqual(parse_confirmation("1,3", 3), ("subset", [0, 2]))
        self.assertEqual(parse_confirmation("2", 3), ("unknown", None))
        self.assertEqual(parse_confirmation("solo 4", 3), ("unknown", None))

    def test_build_economic_lines_balances_income_and_expense(self) -> None:
        income = build_economic_lines(
            "entrata", "cassa_luca", "ricavi_escursioni", 200, "EUR"
        )
        self.assertEqual(income[0]["account_code"], "cassa_luca")
        self.assertEqual(income[0]["dare"], 200.0)
        self.assertEqual(income[1]["avere"], 200.0)

        expense = build_economic_lines(
            "uscita", "cassa_luca", "costi_ristoranti", "50", "EGP"
        )
        self.assertEqual(expense[0]["account_code"], "costi_ristoranti")
        self.assertEqual(expense[0]["dare"], 50.0)
        self.assertEqual(expense[1]["account_code"], "cassa_luca")
        self.assertEqual(expense[1]["avere"], 50.0)

    def test_build_economic_lines_rejects_invalid_amount_or_currency(self) -> None:
        self.assertEqual(
            build_economic_lines("entrata", "cassa", "ricavi", 0, "EUR"),
            [],
        )
        self.assertEqual(
            build_economic_lines("entrata", "cassa", "ricavi", 10, "USD"),
            [],
        )

    def test_suspect_heuristics(self) -> None:
        self.assertEqual(
            is_suspect(
                {
                    "confidence": "low",
                    "importo": 20,
                    "currency": "EUR",
                    "descrizione": "x",
                    "account_code": "costi_altri",
                }
            ),
            (True, "Claude segnala incertezza"),
        )
        self.assertEqual(
            is_high_amount({"importo": 2000, "currency": "EUR"}),
            (True, "importo elevato (€2000)"),
        )

    def test_formatters_are_stable(self) -> None:
        tx = {
            "tipo": "entrata",
            "importo": 90,
            "currency": "EUR",
            "descrizione": "tour",
            "account_code": "ricavi_escursioni",
        }
        self.assertEqual(format_amount(tx), "+90 EUR")
        self.assertIn("solo 1,2", confirmation_help_text(3))
        self.assertIn(
            "15/05/2026",
            format_single_confirmation(
                tx,
                "Omar",
                now=datetime(2026, 5, 15, 12, 0),
            ),
        )

    def test_registration_result_formatting(self) -> None:
        tx = {
            "tipo": "uscita",
            "importo": 50,
            "currency": "EGP",
            "descrizione": "acqua",
            "account_code": "costi_escursioni",
        }

        self.assertEqual(
            format_registration_result(tx, "entry-1", "acqua"),
            "✅ -50 LE acqua",
        )
        self.assertEqual(
            format_registration_result(tx, None, ""),
            "❌ -50 LE (senza descr)",
        )

    def test_registration_summary_formatting(self) -> None:
        self.assertEqual(
            format_registration_summary(["✅ +90 EUR tour"]),
            "✅ +90 EUR tour",
        )
        self.assertEqual(
            format_registration_summary(["✅ +90 EUR tour", "❌ -50 LE acqua"]),
            "💾 Registrate 1/2 transazioni:\n\n✅ +90 EUR tour\n❌ -50 LE acqua",
        )


if __name__ == "__main__":
    unittest.main()
