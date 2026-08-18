import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from pypdf import PdfWriter

from mineru_textbook_to_md import cli as SKILL

ROOT = Path(__file__).resolve().parents[1]
CLI = [sys.executable, "-m", "mineru_textbook_to_md"]


LONG_TEXT = "\u672c\u7ae0\u7528\u4e8e\u6d4b\u8bd5\u7ed3\u6784\u5316\u7ae0\u8282\u8fb9\u754c\u3002" * 20


class ChapterPlannerTests(unittest.TestCase):
    def plan(self, text: str):
        return SKILL.build_split_plan(SKILL.normalize_md(text))

    def test_part_chapter_section_defaults_to_chapters(self):
        text = (
            "# \u76ee\u5f55\n\n"
            "\u7b2c\u4e00\u7bc7 \u57fa\u7840  1\n"
            "\u7b2c\u4e00\u7ae0 \u603b\u8bba  3\n"
            "\u7b2c\u4e00\u8282 \u6982\u5ff5  4\n"
            "\u7b2c\u4e8c\u7ae0 \u65b9\u6cd5  18\n"
            "\u7b2c\u4e00\u8282 \u65b9\u6cd5\u6982\u89c8  19\n\n"
            "# \u7b2c\u4e00\u7bc7 \u57fa\u7840\n\n"
            "# \u7b2c\u4e00\u7ae0 \u603b\u8bba\n\n## \u7b2c\u4e00\u8282 \u6982\u5ff5\n"
            + LONG_TEXT
            + "\n\n# \u7b2c\u4e8c\u7ae0 \u65b9\u6cd5\n\n## \u7b2c\u4e00\u8282 \u65b9\u6cd5\u6982\u89c8\n"
            + LONG_TEXT
        )
        plan = self.plan(text)
        self.assertEqual(plan.method, "structure-tree:chapter")
        self.assertEqual([chapter.number for chapter in plan.chapters], ["\u7b2c\u4e00\u7ae0", "\u7b2c\u4e8c\u7ae0"])
        self.assertFalse(plan.unmatched_toc_nodes)
        self.assertFalse(plan.needs_review)

    def test_section_only_book_uses_sections(self):
        text = (
            "# \u76ee\u5f55\n\n"
            "\u7b2c\u4e00\u8282 \u7eea\u8bba  1\n"
            "\u7b2c\u4e8c\u8282 \u673a\u5236  10\n"
            "\u7b2c\u4e09\u8282 \u5e94\u7528  21\n\n"
            "# \u7b2c\u4e00\u8282 \u7eea\u8bba\n"
            + LONG_TEXT
            + "\n\n# \u7b2c\u4e8c\u8282 \u673a\u5236\n"
            + LONG_TEXT
            + "\n\n# \u7b2c\u4e09\u8282 \u5e94\u7528\n"
            + LONG_TEXT
        )
        plan = self.plan(text)
        self.assertEqual(plan.method, "structure-tree:section")
        self.assertEqual(len(plan.chapters), 3)
        self.assertFalse(plan.needs_review)

    def test_incomplete_toc_is_flagged_instead_of_silently_truncated(self):
        text = (
            "# \u76ee\u5f55\n\n"
            "\u7b2c\u4e00\u7ae0 \u8d77\u70b9  1\n"
            "\u7b2c\u4e8c\u7ae0 \u53d1\u5c55  22\n"
            "\u7b2c\u4e09\u7ae0 \u5e94\u7528  44\n\n"
            "# \u7b2c\u4e00\u7ae0 \u8d77\u70b9\n"
            + LONG_TEXT
            + "\n\n# \u7b2c\u4e8c\u7ae0 \u53d1\u5c55\n"
            + LONG_TEXT
        )
        plan = self.plan(text)
        self.assertTrue(plan.needs_review)
        self.assertEqual(len(plan.chapters), 2)
        self.assertEqual([node.number for node in plan.unmatched_toc_nodes], ["\u7b2c\u4e09\u7ae0"])

    def test_override_moves_a_chapter_boundary(self):
        text = (
            "# \u7b2c\u4e00\u7ae0 \u8d77\u70b9\n"
            + LONG_TEXT
            + "\n\n\u4eba\u5de5\u8fb9\u754c\n"
            + LONG_TEXT
            + "\n\n# \u7b2c\u4e8c\u7ae0 \u53d1\u5c55\n"
            + LONG_TEXT
        )
        markdown = SKILL.normalize_md(text)
        plan = SKILL.build_split_plan(markdown)
        marks = SKILL.collect_page_marks(markdown)
        SKILL.apply_overrides(markdown, plan, [{"title": "\u7b2c\u4e8c\u7ae0 \u53d1\u5c55", "startAnchor": "\u4eba\u5de5\u8fb9\u754c"}], marks)
        self.assertTrue(plan.chapters[1].content.startswith("\u4eba\u5de5\u8fb9\u754c"))
        self.assertEqual(plan.chapters[1].confidence, 1.0)

    def test_page_markers_are_carried_into_the_mapping(self):
        text = (
            "<!-- page: 3 -->\n# \u7b2c\u4e00\u7ae0 \u8d77\u70b9\n"
            + LONG_TEXT
            + "\n\n<!-- page: 22 -->\n# \u7b2c\u4e8c\u7ae0 \u53d1\u5c55\n"
            + LONG_TEXT
        )
        plan = self.plan(text)
        mapping = SKILL.split_mapping(plan, "MinerU/full.md", 40)
        self.assertEqual([item["sourcePageStart"] for item in mapping["chapters"]], [3, 22])

    def test_outline_page_anchors_recover_missing_chapter_titles(self):
        alpha = "Alpha chapter prose. " * 12
        beta = "Beta chapter prose. " * 12
        markdown = f"{alpha}\n\n{beta}\n"
        outline = [
            SKILL.StructureNode("outline-1", "1", "Alpha", "chapter", 1, 0, 0, 10),
            SKILL.StructureNode("outline-2", "2", "Beta", "chapter", 1, 0, 0, 20),
        ]
        plan = SKILL.build_split_plan(
            markdown,
            split_by="chapter",
            outline_nodes=outline,
            page_marks=[(0, 10), (markdown.index("Beta"), 20)],
        )
        self.assertEqual([chapter.title for chapter in plan.chapters], ["Alpha", "Beta"])
        self.assertEqual([chapter.page_start for chapter in plan.chapters], [10, 20])
        self.assertTrue(plan.chapters[0].content.startswith("Alpha chapter prose."))
        self.assertFalse(plan.needs_review)

    def test_dry_run_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "Book"
            raw.mkdir(parents=True)
            raw.joinpath("full.md").write_text(
                "# \u7b2c\u4e00\u7ae0 \u8d77\u70b9\n" + LONG_TEXT + "\n\n# \u7b2c\u4e8c\u7ae0 \u53d1\u5c55\n" + LONG_TEXT,
                encoding="utf-8",
            )
            result = subprocess.run(
                [*CLI, "--from-full-md", str(raw), "--output", str(root), "--dry-run"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((raw / "00-\u7ae0\u8282\u6620\u5c04.json").exists())

    def test_existing_output_is_preserved_without_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "Book"
            raw.mkdir(parents=True)
            full = raw / "full.md"
            full.write_text(
                "# \u7b2c\u4e00\u7ae0 \u8d77\u70b9\n" + LONG_TEXT + "\n\n# \u7b2c\u4e8c\u7ae0 \u53d1\u5c55\n" + LONG_TEXT,
                encoding="utf-8",
            )
            raw.joinpath("keep.md").write_text("keep", encoding="utf-8")
            result = subprocess.run(
                [*CLI, "--from-full-md", str(full)],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(raw.joinpath("keep.md").exists())
            self.assertTrue(raw.joinpath("00-\u7ae0\u8282\u6620\u5c04.json").exists())
            self.assertTrue(raw.joinpath("00-\u5f85\u4eba\u5de5\u590d\u6838.md").exists())


    def test_absolute_image_reference_is_not_copied(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "raw"
            source.mkdir()
            secret = root / "outside.txt"
            secret.write_text("must not copy", encoding="utf-8")
            markdown, copied, missing = SKILL.copy_chapter_images(
                f"![]({secret.as_posix()})", source, root / "output" / "images"
            )
            self.assertEqual(copied, 0)
            self.assertEqual(missing, [secret.as_posix()])
            self.assertEqual(markdown, f"![]({secret.as_posix()})")
            self.assertFalse((root / "output" / "images" / secret.name).exists())

    def test_zip_extraction_rejects_absolute_member(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outside = root / "outside.txt"
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr(outside.as_posix(), "must not write")
            SKILL.safe_extract_zip(archive.getvalue(), root / "extract")
            self.assertFalse(outside.exists())

    def test_force_cache_cleanup_keeps_pdf_and_reports(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "Book.pdf").write_bytes(b"pdf")
            (root / "00-解析质量总报告.md").write_text("report", encoding="utf-8")
            (root / "full.md").write_text("old", encoding="utf-8")
            (root / "00-MinerU分块计划.json").write_text("{}", encoding="utf-8")
            (root / "00-MinerU原始分块").mkdir()
            (root / "images").mkdir()
            SKILL.clear_mineru_cache(root)
            self.assertTrue((root / "Book.pdf").exists())
            self.assertTrue((root / "00-解析质量总报告.md").exists())
            self.assertFalse((root / "full.md").exists())
            self.assertFalse((root / "00-MinerU原始分块").exists())
            self.assertFalse((root / "images").exists())

    def test_external_full_md_conflict_requires_force(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.md"
            target = root / "book" / "full.md"
            source.write_text("new", encoding="utf-8")
            target.parent.mkdir()
            target.write_text("old", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Refusing to replace"):
                SKILL.copy_full_md_input(source, target, force=False)
            SKILL.copy_full_md_input(source, target, force=True)
            self.assertEqual(target.read_text(encoding="utf-8"), "new")

    def test_source_pdf_conflict_requires_force(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.pdf"
            output = root / "book"
            output.mkdir()
            source.write_bytes(b"new")
            (output / "source.pdf").write_bytes(b"old")
            with self.assertRaisesRegex(RuntimeError, "Destination PDF"):
                SKILL.copy_source_pdf(source, output)
            SKILL.copy_source_pdf(source, output, force=True)
            self.assertEqual((output / "source.pdf").read_bytes(), b"new")

    def test_plan_only_uses_synthetic_pdf_bookmarks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pdf = root / "synthetic.pdf"
            writer = PdfWriter()
            for _ in range(4):
                writer.add_blank_page(width=612, height=792)
            part = writer.add_outline_item("第一篇 基础", 0)
            writer.add_outline_item("第一章 总论", 1, parent=part)
            writer.add_outline_item("第二章 方法", 2, parent=part)
            with pdf.open("wb") as handle:
                writer.write(handle)
            result = subprocess.run(
                [*CLI, "--pdf", str(pdf), "--base-name", "Synthetic", "--output", str(root), "--plan-only"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            output = root / "Synthetic"
            self.assertTrue((output / "synthetic.pdf").exists())
            plan = json.loads((output / "00-MinerU分块计划.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["method"], "pdf-bookmark-balanced")
            self.assertEqual(plan["ranges"], [[1, 4]])

    def test_pdf_part_bookmark_page_digits_are_removed(self):
        self.assertEqual(
            SKILL.clean_pdf_outline_title("第二篇　麻醉与围手术期处理1 2"),
            "第二篇 麻醉与围手术期处理",
        )
        self.assertEqual(
            SKILL.clean_pdf_outline_title("第十章 3D 打印技术"),
            "第十章 3D 打印技术",
        )

    def test_mineru_run_reuses_completed_chunk(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            chunk = root / "00-MinerU原始分块" / "chunk-001-1-2"
            chunk.mkdir(parents=True)
            chunk.joinpath("full.md").write_text("# 第一章 测试\n" + LONG_TEXT, encoding="utf-8")
            settings = {"apiTokenEnv": "TOKEN", "pageRangeSize": 200}
            with (
                mock.patch.object(SKILL, "resolve_token", return_value="token"),
                mock.patch.object(SKILL, "request_upload_url", side_effect=AssertionError("upload should not run")),
            ):
                result = SKILL.run_mineru_official(
                    root / "book.pdf", "Book", root, settings, page_count=2
                )
            self.assertEqual(result, root / "full.md")
            self.assertIn("MinerU chunk-001-1-2", result.read_text(encoding="utf-8"))

    def test_invalid_plan_only_full_md_combination_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            full = Path(temp) / "full.md"
            full.write_text("# 第一章 测试\n" + LONG_TEXT, encoding="utf-8")
            result = subprocess.run(
                [*CLI, "--from-full-md", str(full), "--plan-only"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("cannot be combined", result.stdout)

    def test_negative_page_count_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            full = Path(temp) / "full.md"
            full.write_text("# 第一章 测试\n" + LONG_TEXT, encoding="utf-8")
            result = subprocess.run(
                [*CLI, "--from-full-md", str(full), "--page-count", "-1", "--dry-run"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("zero or greater", result.stdout)

    def test_module_version_command(self):
        result = subprocess.run(
            [*CLI, "--version"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0.1.0", result.stdout)


if __name__ == "__main__":
    unittest.main()
