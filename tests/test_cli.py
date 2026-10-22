import contextlib
import io
import json
import unittest
from datetime import date
from unittest.mock import patch

from tello_usage import Balance
from tello_usage.cli import main


class OutputTests(unittest.TestCase):
    def output(self, *args: str) -> str:
        with (
            patch("sys.argv", ["tello-usage", *args]),
            patch.dict(
                "os.environ", {"TELLO_USERNAME": "fake", "TELLO_PASSWORD": "fake"}
            ),
            patch("tello_usage.cli.TelloClient") as client,
            patch("tello_usage.client.date", wraps=date) as clock,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            clock.today.return_value = date(2026, 10, 19)
            client.return_value.__enter__.return_value.balances.return_value = [
                Balance("a", "Example", "457.59 MB", "10 GB", "2026-10-23"),
                Balance("b", "Unlimited line", "10 GB", "Unlimited"),
                Balance("c", "Tomorrow", "1 GB", "2 GB", "2026-10-20"),
            ]
            main()
        return output.getvalue()

    def test_text_percentage(self) -> None:
        text = self.output()
        self.assertIn("457.59 MB remaining / 10 GB (4.58% available)", text)
        self.assertIn("percentage unavailable", text)
        self.assertIn("4 days left in billing cycle", text)
        self.assertIn("1 day left in billing cycle", text)
        self.assertIn("billing cycle days unavailable", text)

    def test_json_percentage(self) -> None:
        records = json.loads(self.output("--json"))
        self.assertEqual(records[0]["percentage_available"], 4.58)
        self.assertIsNone(records[1]["percentage_available"])
        self.assertNotIn("percentage_used", records[0])
        self.assertEqual(records[0]["renewal_date"], "2026-10-23")
        self.assertEqual(records[0]["days_left"], 4)
        self.assertIsNone(records[1]["days_left"])
