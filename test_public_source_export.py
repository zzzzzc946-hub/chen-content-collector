from __future__ import annotations

import importlib.util
import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
EXPORTER_PATH = ROOT / "scripts" / "export_public_source.py"


def load_exporter():
    spec = importlib.util.spec_from_file_location("export_public_source", EXPORTER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


class PublicSourceExportTests(unittest.TestCase):
    def test_project_export_includes_public_handoff_documents(self) -> None:
        exporter = load_exporter()
        with tempfile.TemporaryDirectory() as temporary:
            archive_path = Path(temporary) / "public-source.tar.gz"
            exporter.export_public_source(ROOT, archive_path, ref="HEAD")

            with tarfile.open(archive_path, "r:gz") as archive:
                archive_names = archive.getnames()
                names = set(archive_names)

        self.assertIn("docs/INSTALL_AND_HANDOFF_ZH.md", names)
        self.assertIn("docs/AGENT_GUIDED_SETUP_ZH.md", names)
        self.assertIn("docs/INDEPENDENT_CLOUD_DEPLOYMENT_ZH.md", names)
        self.assertEqual(archive_names.count("PUBLIC-SOURCE-MANIFEST.json"), 1)
        self.assertNotIn("config.json", names)

    def test_export_excludes_private_paths_and_personal_path_content(self) -> None:
        exporter = load_exporter()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            root.mkdir()
            (root / "docs" / "superpowers").mkdir(parents=True)
            (root / "docs" / "superpowers" / "internal.md").write_text("private\n")
            (root / "docs").joinpath("public.md").write_text("public\n")
            private_path = str(Path.home() / "private")
            (root / "source.py").write_text(f"ROOT = '{private_path}'\n")
            (root / "README.md").write_text("public README\n")
            git(root, "init", "-b", "master")
            git(root, "config", "user.name", "Test")
            git(root, "config", "user.email", "test@example.com")
            git(root, "add", ".")
            git(root, "commit", "-m", "fixture")

            archive_path = Path(temporary) / "public-source.tar.gz"
            manifest = exporter.export_public_source(root, archive_path)

            self.assertEqual(manifest["included_count"], 2)
            self.assertEqual(manifest["excluded_count"], 2)
            with tarfile.open(archive_path, "r:gz") as archive:
                names = set(archive.getnames())
                self.assertIn("README.md", names)
                self.assertIn("docs/public.md", names)
                self.assertIn("PUBLIC-SOURCE-MANIFEST.json", names)
                self.assertNotIn("docs/superpowers/internal.md", names)
                self.assertNotIn("source.py", names)
                exported_manifest = json.loads(
                    archive.extractfile("PUBLIC-SOURCE-MANIFEST.json").read()
                )
            self.assertEqual(exported_manifest["commit"], manifest["commit"])

    def test_export_keeps_loopback_security_code(self) -> None:
        exporter = load_exporter()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            root.mkdir()
            (root / "local_service.py").write_text(
                "LOCAL_API = 'http://127.0.0.1:51216'\n"
            )
            git(root, "init", "-b", "master")
            git(root, "config", "user.name", "Test")
            git(root, "config", "user.email", "test@example.com")
            git(root, "add", ".")
            git(root, "commit", "-m", "fixture")

            archive_path = Path(temporary) / "public-source.tar.gz"
            exporter.export_public_source(root, archive_path)

            with tarfile.open(archive_path, "r:gz") as archive:
                self.assertIn("local_service.py", archive.getnames())

    def test_export_portabilizes_known_build_script_path(self) -> None:
        exporter = load_exporter()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            script = root / "scripts" / "build_dmg.py"
            script.parent.mkdir(parents=True)
            script.write_text(
                f'CANONICAL_SOURCE_ROOT = Path("{Path.home()}/Documents/CHEN 内容采集助手")\n'
            )
            git(root, "init", "-b", "master")
            git(root, "config", "user.name", "Test")
            git(root, "config", "user.email", "test@example.com")
            git(root, "add", ".")
            git(root, "commit", "-m", "fixture")

            archive_path = Path(temporary) / "public-source.tar.gz"
            exporter.export_public_source(root, archive_path)

            with tarfile.open(archive_path, "r:gz") as archive:
                exported = archive.extractfile("scripts/build_dmg.py").read()
            self.assertIn(b"Path(__file__).resolve().parents[1]", exported)
            self.assertNotIn(str(Path.home()).encode() + b"/", exported)
