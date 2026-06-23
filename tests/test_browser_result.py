import unittest

from guardia_tester.browser import KarasenaChecker


class ResultParsingTests(unittest.TestCase):
    def test_blocked_result(self) -> None:
        decision, reason = KarasenaChecker._parse_result(
            "No válido\nNombre completo que permite identificar a una persona concreta"
        )
        self.assertEqual("block", decision)
        self.assertIn("Nombre completo", reason)

    def test_allowed_result(self) -> None:
        decision, reason = KarasenaChecker._parse_result("Válido\nNo se detectaron riesgos")
        self.assertEqual("allow", decision)
        self.assertEqual("No se detectaron riesgos", reason)


if __name__ == "__main__":
    unittest.main()

