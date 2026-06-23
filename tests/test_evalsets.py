import tempfile
import unittest
from pathlib import Path

from guardia_tester.evalsets import (
    EvalsetSelectionError,
    discover_evalsets,
    evalset_id,
    select_evalset,
)


class EvalsetTests(unittest.TestCase):
    def test_identifier_removes_standard_prefix(self) -> None:
        self.assertEqual(
            "datos-medicos", evalset_id("GuardIA_EvalSet_Datos_Medicos.md")
        )

    def test_discovers_directory_and_legacy_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "evalsets").mkdir()
            (root / "evalsets" / "Finanzas.md").write_text("# test", encoding="utf-8")
            (root / "GuardIA_EvalSet_Medico.md").write_text("# test", encoding="utf-8")
            (root / "evalsets" / "README.md").write_text("ignore", encoding="utf-8")
            found = discover_evalsets(root, Path("evalsets"))
            self.assertEqual(["finanzas", "medico"], sorted(item.id for item in found))

    def test_selects_by_identifier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "GuardIA_EvalSet_Datos_Medicos.md"
            path.write_text("# test", encoding="utf-8")
            found = discover_evalsets(root, Path("evalsets"))
            self.assertEqual(path, select_evalset(found, "datos-medicos").path)
            with self.assertRaises(EvalsetSelectionError):
                select_evalset(found, "legal")


if __name__ == "__main__":
    unittest.main()

