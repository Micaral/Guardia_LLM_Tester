from pathlib import Path
import unittest

from guardia_tester.parser import CaseParseError, parse_cases, select_cases


ROOT = Path(__file__).resolve().parents[1]


class ParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = parse_cases(ROOT / "evalsets" / "GuardIA_EvalSet_Datos_Medicos.md")

    def test_full_evaluation_set(self) -> None:
        self.assertEqual(62, len(self.cases))
        self.assertEqual(37, sum(case.expected == "block" for case in self.cases))
        self.assertEqual(25, sum(case.expected == "allow" for case in self.cases))

    def test_first_case_fields(self) -> None:
        case = self.cases[0]
        self.assertEqual("A01", case.id)
        self.assertEqual("A", case.group)
        self.assertEqual("block", case.expected)
        self.assertEqual("lab", case.subtype)
        self.assertIn("patient_identifier", case.signals)
        self.assertIn("María Gómez Ruiz", case.prompt)

    def test_select_by_group_or_id(self) -> None:
        selected = select_cases(self.cases, ["M02"], ["J"])
        self.assertEqual(["J01", "J02", "J03", "J04", "J05", "J06", "M02"],
                         [case.id for case in selected])

    def test_unknown_id_fails(self) -> None:
        with self.assertRaises(CaseParseError):
            select_cases(self.cases, ["Z99"], None)


if __name__ == "__main__":
    unittest.main()
