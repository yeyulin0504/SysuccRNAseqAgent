"""Tests for the counts 直入 web entry and the CMS panel endpoint.

Covers the first-class parallel entry decided for the 2026-09-08 UI:
uploading a count matrix and running DE / CMS on it, without any FASTQ
pipeline.  Exercised through the same FastAPI surface the browser uses:

- ``POST /api/projects/{project_id}/counts`` persists ``counts_matrix.tsv``,
  builds a counts-upload session and returns the gate;
- ``POST /api/cms`` reads back / toggles the CMS conditional gate.

The session-level chain (gate -> plan -> confirm -> counts stage) is covered
here too so the whole pathway stays browser-drivable.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

fastapi_missing = False
try:
    from fastapi.testclient import TestClient

    from rnaseq_agent.webapp import create_app
except ImportError:
    fastapi_missing = True


pytestmark = pytest.mark.skipif(fastapi_missing, reason="fastapi/httpx not installed")

COUNT_MATRIX = (
    "gene\tt1\tt2\tt3\tn1\tn2\tn3\n"
    "ENSG00000186092\t10\t20\t30\t5\t6\t7\n"
    "ENSG00000279928\t1\t2\t3\t4\t5\t6\n"
)


@pytest.fixture
def client(tmp_path: Path):
    app = create_app(project_dir=tmp_path / "legacy")
    return TestClient(app)


def _token(client) -> str:
    page = client.get("/").text
    return re.search(r'const TOKEN = "([^"]+)"', page).group(1)


def _headers(token: str) -> dict:
    return {"x-session-token": token}


def _create_project(client, token: str, project_id: str, **extra) -> None:
    resp = client.post(
        "/api/projects",
        json={"project_id": project_id, "title": f"P {project_id}", **extra},
        headers=_headers(token),
    )
    assert resp.status_code == 200, resp.text


def _sample_rows() -> dict:
    """Multipart form fields for the counts 直入 sample table (6 rows)."""
    return {
        "sample_id": ["t1", "t2", "t3", "n1", "n2", "n3"],
        "condition": ["tumor", "tumor", "tumor", "normal", "normal", "normal"],
    }


class TestCountsUploadEndpoint:
    def test_counts_session_accepts_cms_only_and_persists_cancer_type(self, client) -> None:
        token = _token(client)
        h = _headers(token)
        _create_project(client, token, "cnt_cms_only")

        preview = client.post(
            "/api/projects/cnt_cms_only/counts/preview",
            files={"file": ("expr.tsv", COUNT_MATRIX.encode(), "text/tab-separated-values")},
            headers=h,
        ).json()
        assert preview["preview"]["matrix_type"] == "raw_counts"

        body = client.post(
            "/api/projects/cnt_cms_only/counts/session",
            json={
                "upload_id": preview["upload_id"],
                "enabled_cms": True,
                "cancer_type": "coad",
                "samples": [
                    {"sample_id": sample_id, "condition": condition}
                    for sample_id, condition in zip(
                        ["t1", "t2", "t3", "n1", "n2", "n3"],
                        ["tumor", "tumor", "tumor", "normal", "normal", "normal"],
                    )
                ],
            },
            headers=h,
        ).json()
        assert "error_code" not in body, body

        import json

        project_json = json.loads(
            (Path(client.get("/api/state?project=cnt_cms_only", headers=h).json()["project_dir"]) / "project.json").read_text(encoding="utf-8")
        )
        assert project_json["pipeline"]["diffexp"]["enabled"] is False
        assert project_json["pipeline"]["cms"]["enabled"] is True
        assert project_json["study"]["cancer_type"] == "coad"

    def test_counts_session_rejects_normalized_matrix_only_when_de_enabled(self, client) -> None:
        token = _token(client)
        h = _headers(token)
        _create_project(client, token, "cnt_cms_matrix")
        normalized = "gene\tt1\tt2\tt3\tn1\tn2\tn3\nG1\t1.1\t2.2\t3.3\t4.4\t5.5\t6.6\n"

        preview = client.post(
            "/api/projects/cnt_cms_matrix/counts/preview",
            files={"file": ("expr.tsv", normalized.encode(), "text/tab-separated-values")},
            headers=h,
        ).json()
        assert preview["preview"]["matrix_type"] == "normalized_expression"
        samples = [
            {"sample_id": sample_id, "condition": condition}
            for sample_id, condition in zip(
                ["t1", "t2", "t3", "n1", "n2", "n3"],
                ["tumor", "tumor", "tumor", "normal", "normal", "normal"],
            )
        ]

        cms_body = client.post(
            "/api/projects/cnt_cms_matrix/counts/session",
            json={"upload_id": preview["upload_id"], "enabled_cms": True, "cancer_type": "coad", "samples": samples},
            headers=h,
        ).json()
        assert "error_code" not in cms_body, cms_body

        de_body = client.post(
            "/api/projects/cnt_cms_matrix/counts/session",
            json={
                "upload_id": preview["upload_id"],
                "enabled_diffexp": True,
                "reference_condition": "normal",
                "samples": samples,
            },
            headers=h,
        ).json()
        assert de_body["error_code"] == "NOT_EVALUABLE"
        assert "raw counts" in de_body["message"]

    def test_counts_upload_persists_pair_id_for_structured_samples(self, client) -> None:
        token = _token(client)
        h = _headers(token)
        _create_project(client, token, "cnt_pair")
        sample_ids = ["t1", "t2", "t3", "n1", "n2", "n3"]
        fields = {
            "enabled_diffexp": "1",
            "reference_condition": "normal",
            "sample_id": sample_ids,
            "condition": ["tumor"] * 3 + ["normal"] * 3,
            "pair_id": ["p1", "p2", "p3", "p1", "p2", "p3"],
        }
        resp = client.post(
            "/api/projects/cnt_pair/counts",
            data=fields,
            files={"file": ("counts_matrix.tsv", COUNT_MATRIX.encode(), "text/tab-separated-values")},
            headers=h,
        )
        assert resp.status_code == 200, resp.text
        project_dir = Path(client.get("/api/state?project=cnt_pair", headers=h).json()["project_dir"])
        project_json = __import__("json").loads((project_dir / "project.json").read_text(encoding="utf-8"))
        assert [row["pair_id"] for row in project_json["samples"]["items"]] == ["p1", "p2", "p3", "p1", "p2", "p3"]

    def test_creates_counts_session_with_diffexp_only(self, client) -> None:
        token = _token(client)
        h = _headers(token)
        _create_project(client, token, "cnt_a")

        fields = {
            "enabled_diffexp": "1",
            "reference_condition": "normal",
            "cancer_type": "coad",
            **_sample_rows(),
        }
        resp = client.post(
            "/api/projects/cnt_a/counts",
            data=fields,
            files={"file": ("counts_matrix.tsv", COUNT_MATRIX.encode(), "text/tab-separated-values")},
            headers=h,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["state"] in {"drafting", "planned", "confirmed"}
        assert "counts_matrix.tsv" in body["counts_path"]

        # 上传的矩阵已持久化到项目 uploads 目录。
        counts_path = Path(body["counts_path"])
        assert counts_path.is_file()
        assert "ENSG00000186092" in counts_path.read_text(encoding="utf-8")

        # 会话配置标记为 counts 直入，FASTQ 主流程关闭。
        state = client.get("/api/state?project=cnt_a", headers=h).json()
        assert state["state"] == body["state"]
        cfg = state["config"]
        assert cfg["server"]  # editable config present

        # 通过 /api/new?project 路由的同目录配置读取（legacy 端点绑定项目）。
        session_dir = client.get("/api/state?project=cnt_a", headers=h).json()["project_dir"]
        import json

        project_json = json.loads(
            (Path(session_dir) / "project.json").read_text(encoding="utf-8")
        )
        assert project_json["samples"]["source"] == "counts_upload"
        assert project_json["samples"]["items"][0]["sample_id"] == "t1"
        assert "fastq_1" not in project_json["samples"]["items"][0]
        assert project_json["cms"]["run_mode"] == "counts"
        assert project_json["pipeline"]["diffexp"]["enabled"] is True
        assert project_json["pipeline"]["cms"]["enabled"] is False
        # FASTQ 主流程全部关闭。
        for step in ("fastp", "star", "arriba", "featurecounts", "rsem"):
            assert project_json["pipeline"][step]["enabled"] is False

    def test_requires_file(self, client) -> None:
        token = _token(client)
        h = _headers(token)
        _create_project(client, token, "cnt_b")
        resp = client.post(
            "/api/projects/cnt_b/counts",
            data={"enabled_diffexp": "1", "reference_condition": "normal", **_sample_rows()},
            headers=h,
        )
        assert resp.status_code == 200
        assert "缺少 counts 矩阵文件" in resp.json()["error"]

    def test_diffexp_requires_reference(self, client) -> None:
        token = _token(client)
        h = _headers(token)
        _create_project(client, token, "cnt_c")
        resp = client.post(
            "/api/projects/cnt_c/counts",
            data={"enabled_diffexp": "1", **_sample_rows()},
            files={"file": ("m.tsv", COUNT_MATRIX.encode(), "text/tab-separated-values")},
            headers=h,
        )
        assert resp.status_code == 200
        assert "reference_condition" in resp.json()["error"]

    def test_at_least_one_stage_required(self, client) -> None:
        token = _token(client)
        h = _headers(token)
        _create_project(client, token, "cnt_d")
        resp = client.post(
            "/api/projects/cnt_d/counts",
            data=_sample_rows(),
            files={"file": ("m.tsv", COUNT_MATRIX.encode(), "text/tab-separated-values")},
            headers=h,
        )
        assert resp.status_code == 200
        assert "至少启用" in resp.json()["error"]

    def test_counts_session_passes_gate_plan_confirm(self, client) -> None:
        """The counts session must be runnable through the session state machine."""
        token = _token(client)
        h = _headers(token)
        _create_project(client, token, "cnt_e")

        fields = {
            "enabled_diffexp": "1",
            "reference_condition": "normal",
            **_sample_rows(),
        }
        resp = client.post(
            "/api/projects/cnt_e/counts",
            data=fields,
            files={"file": ("counts_matrix.tsv", COUNT_MATRIX.encode(), "text/tab-separated-values")},
            headers=h,
        ).json()
        assert "error" not in resp, resp
        # Gate-A 应通过：counts 直入不要求 FASTQ / STAR / featureCounts。
        assert not [g for g in resp["gate"] if g.startswith("不适用")], resp["gate"]

        # plan -> confirm 一路可走通。
        plan = client.post("/api/plan?project=cnt_e", headers=h).json()
        assert plan["state"] == "planned", plan
        confirm = client.post("/api/confirm?project=cnt_e", headers=h).json()
        assert confirm["state"] == "confirmed", confirm


class TestCmsPanelEndpoint:
    def test_cms_status_read_only(self, client) -> None:
        token = _token(client)
        h = _headers(token)
        _create_project(client, token, "cnt_f")
        resp = client.post("/api/cms?project=cnt_f", headers=h)
        assert resp.status_code == 200
        body = resp.json()
        assert "error" in body  # 尚无会话。
        assert "还没有项目" in body["error"]

    def test_cms_toggle_requires_valid_run_mode(self, client) -> None:
        token = _token(client)
        h = _headers(token)
        _create_project(client, token, "cnt_g")

        # 先建一个普通（pipeline 入口）项目，走 /api/new 的 counts 之外的路径。
        samples = [
            {"sample_id": "s1", "condition": "t", "fastq_1": "s1_R1.fastq.gz", "fastq_2": "s1_R2.fastq.gz"},
        ]
        client.post("/api/new?project=cnt_g", json={"project_id": "cnt_g", "title": "G", "samples": samples}, headers=h)

        # 非法 run_mode 被忽略，保持默认 pipeline。
        body = client.post(
            "/api/cms?project=cnt_g",
            json={"enabled": True, "run_mode": "bogus"},
            headers=h,
        ).json()
        assert body["requested"] is True
        assert body["run_mode"] == "pipeline"
