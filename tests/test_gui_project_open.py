from __future__ import annotations

import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from rnaseq_agent.gui import ConfigApp


class Variable:
    def __init__(self, value: object = "") -> None:
        self.value = value

    def get(self) -> object:
        return self.value

    def set(self, value: object) -> None:
        self.value = value


class TextArea:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def delete(self, _start: str, _end: str) -> None:
        self.value = ""

    def insert(self, _at: str, value: str) -> None:
        self.value = value


class GuiProjectOpenTests(unittest.TestCase):
    def make_app(self) -> ConfigApp:
        app = object.__new__(ConfigApp)
        for name in (
            "project_id", "project_title", "owner", "server_profile", "server_host",
            "server_user", "remote_base_dir", "scheduler", "threads", "memory_gb",
            "ssh_auth_mode", "ssh_key_path", "layout", "reads_per_sample_million",
            "strandedness", "local_data_dir", "remote_data_dir", "poll_interval",
            "poll_timeout", "recipient", "smtp_host", "smtp_port", "smtp_user",
            "smtp_password_env", "ssh_status", "status_text", "downstream_min_count",
            "downstream_min_samples", "downstream_padj", "downstream_abs_log2fc",
            "downstream_organism", "downstream_gmt_path", "downstream_gmt_sha256",
            "downstream_runtime_image", "downstream_runtime_sha256",
        ):
            setattr(app, name, Variable())
        app.email_enabled = Variable(False)
        app.downstream_enabled = Variable(False)
        app.downstream_use_batch = Variable(False)
        app.downstream_go_ora = Variable(True)
        app.downstream_kegg_ora = Variable(True)
        app.downstream_gsea = Variable(True)
        app.downstream_gmt_enabled = Variable(False)
        app.init_text = TextArea("stale init")
        app.sample_text = TextArea("stale sample")
        app.downstream_metadata_text = TextArea("stale downstream metadata")
        app.downstream_contrasts_text = TextArea("stale downstream contrasts")
        app.ref_vars = {"genome": Variable("stale")}
        app.pipeline_vars = {"fastqc": Variable(False), "star": Variable(False)}
        app.output_dir = Path("/tmp/runs")
        app.current_config_path = None
        app.loaded_config = None
        app.pending_proposals = ()
        return app

    def config(self) -> dict:
        return {
            "schema_version": 1,
            "created_at": "2026-08-05T10:00:00",
            "project": {"id": "saved_project", "title": "Saved project", "owner": "researcher"},
            "server": {
                "profile": "sysu", "host": "hpc.example.edu", "user": "alice",
                "remote_base_dir": "/work/alice/projects", "remote_workdir": "/work/alice/projects/saved_project",
                "scheduler": "slurm", "threads": 24, "memory_gb": 96, "auth_mode": "password",
                "key_path": "", "init_commands": ["module load apptainer", "module load star"],
            },
            "reference": {"genome": "GRCh38"},
            "sequencing": {"layout": "paired", "reads_per_sample_million": 55, "strandedness": "reverse"},
            "samples": {
                "source": "local_upload", "local_data_dir": "D:/fastq", "remote_data_dir": "/work/alice/raw",
                "items": [{"sample_id": "S1", "condition": "tumor", "fastq_1": "S1_R1.fastq.gz", "fastq_2": "S1_R2.fastq.gz"}],
            },
            "pipeline": {"fastqc": {"enabled": True, "version": "0.12"}, "star": {"enabled": False, "version": "2.7"}},
            "downstream": {
                "enabled": True,
                "metadata": {"samples": [
                    {"sample_id": "S1", "condition": "tumor", "batch": "batch_1"},
                ]},
                "design": {"batch_column": "batch"},
                "contrasts": [{"id": "tumor_vs_normal", "numerator": "tumor", "denominator": "normal"}],
                "filtering": {"min_count": 25, "min_samples": 3},
                "differential_expression": {"padj_threshold": 0.01, "abs_log2_fold_change": 1.5},
                "enrichment": {
                    "go_ora": False, "kegg_ora": True, "gsea": False, "organism": "human",
                    "gmt": {"enabled": True, "source_path": "D:/sets/hallmark.gmt", "sha256": "a" * 64},
                },
                "runtime": {"image_path": "/shared/rnaseq.sif", "image_sha256": "b" * 64},
            },
            "polling": {"interval_seconds": 60, "timeout_hours": 24},
            "notification": {"email_enabled": True, "recipient": "a@example.edu", "smtp_host": "smtp.example.edu", "smtp_port": 465, "smtp_user": "a", "password_env": "SMTP_SECRET"},
            "execution": {"mode": "contract", "skill_id": "rna"},
            "container": {"image": "/images/rna.sif"},
            "status": {"state": "submitted", "message": "12345"},
            "custom_metadata": {"cohort": "pilot"},
        }

    def test_hydrates_fields_replaces_text_and_never_restores_password(self) -> None:
        app = self.make_app()
        config = self.config()
        app.server_host.set("previous.example.edu")
        app.server_user.set("previous_user")
        with patch("rnaseq_agent.gui.clear_ssh_credential") as clear:
            ConfigApp._apply_loaded_config(app, config)

        clear.assert_called_once()
        self.assertEqual(app.server_host.get(), "hpc.example.edu")
        self.assertEqual(app.init_text.value, "module load apptainer\nmodule load star")
        self.assertEqual(app.sample_text.value, "S1,tumor,S1_R1.fastq.gz,S1_R2.fastq.gz")
        self.assertTrue(app.downstream_enabled.get())
        self.assertTrue(app.downstream_use_batch.get())
        self.assertEqual(app.downstream_metadata_text.value, "S1,tumor,batch_1")
        self.assertEqual(app.downstream_contrasts_text.value, "tumor_vs_normal,tumor,normal")
        self.assertEqual(app.downstream_runtime_image.get(), "/shared/rnaseq.sif")
        self.assertEqual(app.ssh_status.get(), "已载入服务器设置；SSH 密码不会保存，请在本次会话中重新输入。")
        self.assertNotIn("password", app.__dict__)

    def test_save_path_uses_opened_file_and_rejects_id_change(self) -> None:
        app = self.make_app()
        app.current_config_path = Path("D:/saved/project.json")
        app.loaded_config = self.config()
        self.assertEqual(ConfigApp._config_save_path(app, self.config()), app.current_config_path)
        changed = deepcopy(self.config())
        changed["project"]["id"] = "other_project"
        with self.assertRaisesRegex(ValueError, "项目 ID 已变更"):
            ConfigApp._config_save_path(app, changed)

    def test_merge_preserves_non_form_fields(self) -> None:
        app = self.make_app()
        merged = ConfigApp._merge_config(app, self.config(), {"server": {"host": "new.example.edu"}})
        self.assertEqual(merged["server"]["host"], "new.example.edu")
        self.assertEqual(merged["execution"]["mode"], "contract")
        self.assertEqual(merged["custom_metadata"], {"cohort": "pilot"})

    def test_notification_config_is_separate_from_downstream_apply(self) -> None:
        app = self.make_app()
        app.email_enabled.set(False)
        self.assertEqual(ConfigApp._notification_config(app), {"email_enabled": False})
        app.email_enabled.set(True)
        app.smtp_port.set(587)
        self.assertEqual(
            ConfigApp._notification_config(app),
            {
                "email_enabled": True,
                "recipient": "",
                "smtp_host": "",
                "smtp_port": 587,
                "smtp_user": "",
                "password_env": "",
                "notify_on": ["completed", "failed", "download_failed", "run_failed"],
            },
        )


if __name__ == "__main__":
    unittest.main()
