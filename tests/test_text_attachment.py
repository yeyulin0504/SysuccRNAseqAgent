from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rnaseq_agent.text_attachment import AttachmentError, load_text_attachment


class TextAttachmentTests(unittest.TestCase):
    def test_loads_utf8_metadata_and_redacts_preview_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metadata.tsv"
            path.write_text("organism\tmouse\napi_key=private-value\n", encoding="utf-8")

            attachment = load_text_attachment(path)

        self.assertEqual(attachment.name, "metadata.tsv")
        self.assertIn("mouse", attachment.text)
        self.assertIn("api_key=<已遮蔽>", attachment.preview)
        self.assertNotIn("private-value", attachment.preview)

    def test_rejects_non_text_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reads.fastq.gz"
            path.write_text("not a supported attachment", encoding="utf-8")

            with self.assertRaisesRegex(AttachmentError, "仅支持"):
                load_text_attachment(path)


if __name__ == "__main__":
    unittest.main()
