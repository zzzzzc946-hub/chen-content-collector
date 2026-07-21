import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parent
SKILLS = {
    "building-content-intelligence-products": {
        "module-map.md",
        "acceptance-matrix.md",
    },
    "collecting-cross-platform-content": {
        "platform-adapters.md",
        "browser-cdp-recovery.md",
        "transcription-fallback.md",
        "operator-statuses.md",
    },
    "building-local-content-workbenches": {
        "sqlite-data-model.md",
        "download-queue.md",
        "daily-workbench.md",
        "local-media-playback.md",
    },
    "publishing-collaborative-cloud-dailies": {
        "idempotent-snapshot-publisher.md",
        "resumable-secure-media.md",
        "fixed-secret-collaboration.md",
        "current-day-and-retention.md",
        "responsive-daily-workbench.md",
    },
    "operating-native-desktop-products": {
        "native-service-host.md",
        "canonical-release.md",
        "rollback-and-recovery.md",
        "visual-runtime-qa.md",
    },
    "adding-email-auth-and-role-permissions": {
        "email-registration-and-sessions.md",
        "role-and-date-access.md",
        "owner-administration.md",
        "permission-audit.md",
    },
}
FRONTMATTER = re.compile(
    r"\A---\nname: (?P<name>[a-z0-9-]+)\ndescription: "
    r"(?P<description>[^\n]*\S[^\n]*)\n---\n"
)
SUSPECTED_SECRET = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)\b\s*[:=]\s*\S+"
)
MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\((?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\)")


class SkillStructureTest(unittest.TestCase):
    def read_utf8_text(self, path):
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            self.fail(f"Non-UTF-8 or binary file in skill package: {path} ({error.reason})")
        except OSError as error:
            self.fail(f"Unable to read skill package file: {path} ({error})")

    def test_every_skill_has_valid_frontmatter_and_direct_references(self):
        for name, expected_references in SKILLS.items():
            with self.subTest(skill=name):
                skill_dir = ROOT / name
                self.assertTrue(skill_dir.is_dir(), f"Missing skill directory: {skill_dir}")

                skill_file = skill_dir / "SKILL.md"
                self.assertTrue(skill_file.is_file(), f"Missing SKILL.md: {skill_file}")
                if not skill_file.is_file():
                    continue

                text = self.read_utf8_text(skill_file)
                frontmatter = FRONTMATTER.match(text)
                self.assertIsNotNone(
                    frontmatter,
                    f"Invalid frontmatter in {skill_file}; expected name and description fields",
                )
                if frontmatter is not None:
                    self.assertEqual(
                        frontmatter.group("name"),
                        name,
                        f"Frontmatter name mismatch in {skill_file}",
                    )
                    description = frontmatter.group("description")
                    self.assertRegex(
                        description,
                        r"^Use when .+",
                        f"Description must start with 'Use when' in {skill_file}",
                    )
                    self.assertNotRegex(
                        description,
                        r"(?i)\b(?:first|then|finally|step|read|write|run|validate)\b",
                        f"Description must describe a trigger, not a workflow, in {skill_file}",
                    )

                reference_dir = skill_dir / "references"
                self.assertTrue(
                    reference_dir.is_dir(), f"Missing references directory: {reference_dir}"
                )
                reference_paths = (
                    list(reference_dir.rglob("*")) if reference_dir.is_dir() else []
                )
                nested_directories = {
                    path.relative_to(reference_dir).as_posix()
                    for path in reference_paths
                    if path.is_dir()
                }
                self.assertFalse(
                    nested_directories,
                    f"Nested directories are not allowed in {reference_dir}: "
                    f"{sorted(nested_directories)}",
                )
                actual_references = {
                    path.relative_to(reference_dir).as_posix()
                    for path in reference_paths
                    if path.is_file()
                }
                self.assertSetEqual(
                    actual_references,
                    expected_references,
                    f"References must be the expected direct files in {reference_dir}",
                )

                linked_references = {
                    target.split("#", 1)[0]
                    for match in MARKDOWN_LINK.finditer(text)
                    for target in [match.group("target")]
                    if target.startswith("references/")
                }
                expected_links = {f"references/{filename}" for filename in expected_references}
                self.assertSetEqual(
                    linked_references,
                    expected_links,
                    f"SKILL.md must link every expected direct reference in {skill_file}",
                )
                for linked_reference in linked_references:
                    reference_path = skill_dir / linked_reference
                    self.assertTrue(
                        reference_path.is_file(),
                        f"Missing linked reference from {skill_file}: {reference_path}",
                    )
                    self.assertEqual(
                        reference_path.parent,
                        reference_dir,
                        f"Linked reference is not directly under {reference_dir}: "
                        f"{linked_reference}",
                    )
                    self.assertEqual(
                        reference_path.resolve().parent,
                        reference_dir.resolve(),
                        f"Linked reference resolves outside {reference_dir}: {linked_reference}",
                    )

                files_to_scan = [
                    path for path in skill_dir.rglob("*") if path.is_file()
                ]
                for path in files_to_scan:
                    contents = self.read_utf8_text(path)
                    self.assertNotRegex(
                        contents,
                        SUSPECTED_SECRET,
                        f"Suspected secret assignment in {path}",
                    )


if __name__ == "__main__":
    unittest.main()
