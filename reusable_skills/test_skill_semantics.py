import unittest
from pathlib import Path


ROOT = Path(__file__).parent


class ParentSkillSemanticsTest(unittest.TestCase):
    def test_parent_skill_uses_primary_module_first_routing(self):
        text = (ROOT / "building-content-intelligence-products" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("one primary module first", text)
        self.assertIn("adjacent module", text)
        self.assertIn("Do not fan out", text)

    def test_validation_includes_controlled_cross_boundary_example(self):
        text = (ROOT / "validation.md").read_text(encoding="utf-8")
        self.assertIn("explicit adjacent", text)
        self.assertIn("Scope stays controlled", text)


if __name__ == "__main__":
    unittest.main()
