from __future__ import annotations

import unittest
from unittest.mock import patch

from hermes_durable_memory.i18n import t


class I18nTests(unittest.TestCase):
    def test_english_and_russian_pending_copy(self) -> None:
        with patch("hermes_durable_memory.i18n._language", return_value="en"):
            english = t("pending_empty")
        with patch("hermes_durable_memory.i18n._language", return_value="ru"):
            russian = t("pending_empty")
        self.assertIn("pending", english.lower())
        self.assertIn("подтверждения", russian)

    def test_unknown_language_falls_back_to_english(self) -> None:
        with patch("hermes_durable_memory.i18n._language", return_value="de"):
            message = t("search_empty", query="cats")
        self.assertEqual(message, "No memory records matched «cats».")

    def test_human_decision_prompt_is_localized(self) -> None:
        with patch("hermes_durable_memory.i18n._language", return_value="en"):
            english = t("human_decision_title")
        with patch("hermes_durable_memory.i18n._language", return_value="ru"):
            russian = t("human_decision_title")
        self.assertIn("memory", english.lower())
        self.assertIn("памяти", russian)


if __name__ == "__main__":
    unittest.main()
