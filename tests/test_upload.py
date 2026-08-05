from __future__ import annotations

import gzip
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from rnaseq_agent.execution import CommandResult
from rnaseq_agent.run_agent import upload_project_fastqs
from rnaseq_agent.storage import load_json, save_json


def _write_fastq(path: Path, read_id: str) -> None:
    with gzip.open(path, "wt", encoding="ascii") as handle:
        handle.write(f"@{read_id}\nACGT\n+\nIIII\n")


class UploadOnlyTests(unittest.TestCase):
    @patch("rnaseq_agent.run_agent.create_remote_transport")
    def test_upload_only_ignores_pipeline_references_and_does_not_submit(
        self,
        create_transport: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            data_dir = root / "fastq"
            data_dir.mkdir()
            _write_fastq(data_dir / "sample_R1.fastq.gz", "read1/1")
            _write_fastq(data_dir / "sample_R2.fastq.gz", "read1/2")
            config_path = root / "project" / "project.json"
            save_json(
                config_path,
                {
                    "project": {"id": "upload_test", "title": "Upload test"},
                    "server": {
                        "host": "hpc.example.edu",
                        "user": "researcher",
                        "remote_workdir": "/remote/upload_test",
                    },
                    "samples": {
                        "local_data_dir": str(data_dir),
                        "remote_data_dir": "/remote/upload_test/raw",
                        "items": [
                            {
                                "sample_id": "sample",
                                "fastq_1": "sample_R1.fastq.gz",
                                "fastq_2": "sample_R2.fastq.gz",
                            }
                        ],
                    },
                    "sequencing": {"layout": "paired"},
                    "pipeline": {
                        "star": {"enabled": True},
                        "featurecounts": {"enabled": True},
                    },
                    "reference": {},
                },
            )
            transport = create_transport.return_value
            transport.execute.return_value = CommandResult([], 0, "", "")
            transport.upload.return_value = CommandResult([], 0, "uploaded", "")

            outcome = upload_project_fastqs(config_path)

            self.assertEqual(outcome.state, "uploaded")
            self.assertEqual(load_json(config_path)["status"]["state"], "uploaded")
            transport.execute.assert_called_once()
            transport.upload.assert_called_once()
            uploaded_paths = transport.upload.call_args.args[0]
            self.assertEqual(len(uploaded_paths), 2)


if __name__ == "__main__":
    unittest.main()
