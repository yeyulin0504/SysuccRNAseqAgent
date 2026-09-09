from pathlib import Path
import re

import pytest

fastapi_missing = False
try:
    from fastapi.testclient import TestClient
    from rnaseq_agent.webapp import create_app
except ImportError:
    fastapi_missing = True

pytestmark = pytest.mark.skipif(fastapi_missing, reason="fastapi/httpx not installed")


def _token(client) -> str:
    page = client.get("/").text
    return re.search(r'const TOKEN = "([^"]+)"', page).group(1)


def _headers(token: str) -> dict[str, str]:
    return {"x-session-token": token}


def _create_project(client, token: str, project_id: str) -> None:
    resp = client.post(
        "/api/projects",
        json={"project_id": project_id, "title": project_id},
        headers=_headers(token),
    )
    assert resp.status_code == 200, resp.text


def test_new_project_uses_setup_state(tmp_path: Path) -> None:
    client = TestClient(create_app(project_dir=tmp_path / "legacy"))
    token = _token(client)
    _create_project(client, token, "wiz_a")

    projects = client.get("/api/projects", headers=_headers(token)).json()["projects"]
    row = next(p for p in projects if p["project_id"] == "wiz_a")
    assert row["state"] == "setup"

    intake = client.get("/api/projects/wiz_a/intake", headers=_headers(token)).json()
    assert intake["state"] == "setup"
    assert intake["routes"]["bulk_rna"]["available"] is True
    assert intake["routes"]["wxs"]["available"] is False
    assert intake["routes"]["scrna"]["available"] is False


def test_set_reserved_route_records_reason_without_session(tmp_path: Path) -> None:
    client = TestClient(create_app(project_dir=tmp_path / "legacy"))
    token = _token(client)
    h = _headers(token)
    _create_project(client, token, "wiz_b")

    body = client.post("/api/projects/wiz_b/route", json={"route": "scrna"}, headers=h).json()

    assert body["route"] == "scrna"
    assert body["available"] is False
    assert body["state"] == "setup"
    assert "session.json" not in [p.name for p in (tmp_path / "wiz_b").glob("*")]


def test_counts_preview_records_history_and_blocks_deseq2_for_geo(tmp_path: Path) -> None:
    client = TestClient(create_app(project_dir=tmp_path / "legacy"))
    token = _token(client)
    h = _headers(token)
    _create_project(client, token, "wiz_c")
    geo = (
        b"!series_matrix_table_begin\n"
        b"ID_REF\tGSM1\tGSM2\n"
        b"1007_s_at\t5.1\t6.2\n"
        b"!series_matrix_table_end\n"
    )

    body = client.post(
        "/api/projects/wiz_c/counts/preview",
        files={"file": ("GSE_series_matrix.txt", geo, "text/plain")},
        headers=h,
    ).json()

    assert body["preview"]["matrix_type"] == "geo_series_matrix_like"
    assert body["preview"]["can_run_deseq2"] is False
    assert body["state"] == "input_ready"
    assert body["history"][-1]["type"] == "input_matrix"


def test_counts_session_refuses_deseq2_for_normalized_matrix(tmp_path: Path) -> None:
    client = TestClient(create_app(project_dir=tmp_path / "legacy"))
    token = _token(client)
    h = _headers(token)
    _create_project(client, token, "wiz_d")
    content = b"gene\tA1\tA2\tA3\tB1\tB2\tB3\nG1\t1.1\t1.2\t1.3\t2.1\t2.2\t2.3\n"

    preview = client.post(
        "/api/projects/wiz_d/counts/preview",
        files={"file": ("expr.tsv", content, "text/tab-separated-values")},
        headers=h,
    ).json()
    upload_id = preview["upload_id"]

    body = client.post(
        "/api/projects/wiz_d/counts/session",
        json={
            "upload_id": upload_id,
            "enabled_diffexp": True,
            "reference_condition": "normal",
            "samples": [
                {"sample_id": "A1", "condition": "tumor"},
                {"sample_id": "A2", "condition": "tumor"},
                {"sample_id": "A3", "condition": "tumor"},
                {"sample_id": "B1", "condition": "normal"},
                {"sample_id": "B2", "condition": "normal"},
                {"sample_id": "B3", "condition": "normal"},
            ],
        },
        headers=h,
    ).json()

    assert body["error_code"] == "NOT_EVALUABLE"
    assert "raw counts" in body["message"]


def _raw_matrix() -> bytes:
    return b"gene\tA1\tA2\tA3\tB1\tB2\tB3\nG1\t1\t2\t3\t4\t5\t6\n"


def _raw_samples() -> list[dict[str, str]]:
    return [
        {"sample_id": "A1", "condition": "A"},
        {"sample_id": "A2", "condition": "A"},
        {"sample_id": "A3", "condition": "A"},
        {"sample_id": "B1", "condition": "B"},
        {"sample_id": "B2", "condition": "B"},
        {"sample_id": "B3", "condition": "B"},
    ]


def _preview_raw(client, token: str, project_id: str) -> str:
    """Upload + preview a raw integer matrix; returns the upload_id."""
    h = _headers(token)
    body = client.post(
        f"/api/projects/{project_id}/counts/preview",
        files={"file": ("raw_counts.tsv", _raw_matrix(), "text/tab-separated-values")},
        headers=h,
    ).json()
    assert body["preview"]["matrix_type"] == "raw_counts", body
    assert body["preview"]["samples"] == ["A1", "A2", "A3", "B1", "B2", "B3"], body
    return body["upload_id"]


def test_counts_session_creates_raw_counts_session(tmp_path: Path) -> None:
    """Task 6 依赖：raw counts → counts/session → session.json 真正落盘."""
    client = TestClient(create_app(project_dir=tmp_path / "legacy"))
    token = _token(client)
    h = _headers(token)
    _create_project(client, token, "wiz_e")
    upload_id = _preview_raw(client, token, "wiz_e")

    resp = client.post(
        "/api/projects/wiz_e/counts/session",
        json={
            "upload_id": upload_id,
            "enabled_diffexp": True,
            "reference_condition": "B",
            "samples": _raw_samples(),
        },
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" not in body and "error_code" not in body, body
    assert body["state"] in {"drafting", "planned", "confirmed"}

    # helper 返回的 gate 必须一并透出，前端据此判断门禁是否真的通过。
    assert "gate" in body
    assert body["counts_path"].endswith("raw_counts.tsv")

    project_dir = tmp_path / "wiz_e"
    # 成功分支必须真正落盘：session.json 存在，且项目配置进入 counts 直入。
    import json

    assert (project_dir / "session.json").is_file()
    session = json.loads((project_dir / "session.json").read_text(encoding="utf-8"))
    assert session["state"] == body["state"]
    assert (project_dir / "project.json").is_file()
    project_json = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    assert project_json["samples"]["source"] == "counts_upload"
    assert project_json["samples"]["counts_path"].endswith("raw_counts.tsv")
    assert project_json["pipeline"]["diffexp"]["enabled"] is True
    assert project_json["diffexp"]["reference_condition"] == "B"
    assert [item["sample_id"] for item in project_json["samples"]["items"]] == [
        "A1", "A2", "A3", "B1", "B2", "B3",
    ]

    # intake 的 matrix_preview 保留，history 尾部记录样本分组。
    intake = json.loads((project_dir / "intake.json").read_text(encoding="utf-8"))
    assert intake["matrix_preview"]["matrix_type"] == "raw_counts"
    history = json.loads((project_dir / "history.json").read_text(encoding="utf-8"))["items"]
    assert history[-1]["type"] == "sample_design"
    assert history[-1]["details"]["count"] == 6


def test_counts_session_requires_reference_condition(tmp_path: Path) -> None:
    """Finding 1：enabled_diffexp 缺 reference_condition 必须报字段级错误."""
    client = TestClient(create_app(project_dir=tmp_path / "legacy"))
    token = _token(client)
    h = _headers(token)
    _create_project(client, token, "wiz_f")
    upload_id = _preview_raw(client, token, "wiz_f")

    body = client.post(
        "/api/projects/wiz_f/counts/session",
        json={
            "upload_id": upload_id,
            "enabled_diffexp": True,
            "reference_condition": "",
            "samples": _raw_samples(),
        },
        headers=h,
    ).json()

    assert body.get("error_code"), body
    assert "reference_condition" in body.get("message", "")
    # 失败时不得落盘成成功的 drafting 会话。
    assert not (tmp_path / "wiz_f" / "session.json").is_file()


def test_counts_session_rejects_reference_not_in_conditions(tmp_path: Path) -> None:
    """Finding 1：reference 不在 payload samples 的 condition 集合 → 字段级错误."""
    client = TestClient(create_app(project_dir=tmp_path / "legacy"))
    token = _token(client)
    h = _headers(token)
    _create_project(client, token, "wiz_g")
    upload_id = _preview_raw(client, token, "wiz_g")

    body = client.post(
        "/api/projects/wiz_g/counts/session",
        json={
            "upload_id": upload_id,
            "enabled_diffexp": True,
            "reference_condition": "control",
            "samples": _raw_samples(),
        },
        headers=h,
    ).json()

    assert body.get("error_code"), body
    assert "reference_condition" in body.get("message", "")
    assert not (tmp_path / "wiz_g" / "session.json").is_file()


def test_counts_session_requires_any_sample(tmp_path: Path) -> None:
    """Finding 1：samples 为空时同样拒绝而非 200 返回 drafting."""
    client = TestClient(create_app(project_dir=tmp_path / "legacy"))
    token = _token(client)
    h = _headers(token)
    _create_project(client, token, "wiz_h")
    upload_id = _preview_raw(client, token, "wiz_h")

    body = client.post(
        "/api/projects/wiz_h/counts/session",
        json={"upload_id": upload_id, "enabled_diffexp": True, "reference_condition": "A"},
        headers=h,
    ).json()

    assert body.get("error_code"), body
    assert not (tmp_path / "wiz_h" / "session.json").is_file()


def test_counts_session_rejects_sample_not_in_matrix(tmp_path: Path) -> None:
    """Finding 2：payload sample_id 必须 ⊆ preview 检测到的样本列."""
    client = TestClient(create_app(project_dir=tmp_path / "legacy"))
    token = _token(client)
    h = _headers(token)
    _create_project(client, token, "wiz_i")
    upload_id = _preview_raw(client, token, "wiz_i")

    body = client.post(
        "/api/projects/wiz_i/counts/session",
        json={
            "upload_id": upload_id,
            "enabled_diffexp": True,
            "reference_condition": "B",
            "samples": [
                *(_raw_samples()[:5]),
                {"sample_id": "NOT_IN_MATRIX", "condition": "B"},
            ],
        },
        headers=h,
    ).json()

    assert body.get("error_code"), body
    assert "NOT_IN_MATRIX" in body.get("message", "")
    assert not (tmp_path / "wiz_i" / "session.json").is_file()


def test_counts_session_rejects_duplicate_sample_id(tmp_path: Path) -> None:
    """Finding 2：重复 sample_id 一并拒绝."""
    client = TestClient(create_app(project_dir=tmp_path / "legacy"))
    token = _token(client)
    h = _headers(token)
    _create_project(client, token, "wiz_j")
    upload_id = _preview_raw(client, token, "wiz_j")

    body = client.post(
        "/api/projects/wiz_j/counts/session",
        json={
            "upload_id": upload_id,
            "enabled_diffexp": True,
            "reference_condition": "B",
            "samples": [*_raw_samples(), {"sample_id": "A1", "condition": "A"}],
        },
        headers=h,
    ).json()

    assert body.get("error_code"), body
    assert "A1" in body.get("message", "")
    assert not (tmp_path / "wiz_j" / "session.json").is_file()


def test_fastq_session_from_detected_samples_enters_input_ready(tmp_path: Path) -> None:
    client = TestClient(create_app(project_dir=tmp_path / "legacy"))
    token = _token(client)
    h = _headers(token)
    _create_project(client, token, "wiz_e")

    body = client.post(
        "/api/projects/wiz_e/fastq/session",
        json={
            "data_source": "remote_path",
            "remote_fastq_dir": "/hwdata/home/yeyulin/demo_fastq",
            "cancer_type": "crc",
            "design": "independent_two_group",
            "layout": "paired",
            "samples": [
                {
                    "sample_id": "CTRL_1",
                    "condition": "control",
                    "fastq_1": "CTRL_1_R1.fastq.gz",
                    "fastq_2": "CTRL_1_R2.fastq.gz",
                },
                {
                    "sample_id": "CASE_1",
                    "condition": "case",
                    "fastq_1": "CASE_1_R1.fastq.gz",
                    "fastq_2": "CASE_1_R2.fastq.gz",
                },
            ],
        },
        headers=h,
    ).json()

    assert body["state"] in {"input_ready", "drafting"}
    assert body["intake"]["route"] == "bulk_rna"
    assert body["intake"]["input_type"] == "remote_fastq"
    assert body["history"][-1]["type"] == "sample_design"

    # Task 6 plan 依赖：成功分支必须真正落盘 session.json，且与响应 state 一致。
    import json

    project_dir = tmp_path / "wiz_e"
    assert (project_dir / "session.json").is_file()
    session = json.loads((project_dir / "session.json").read_text(encoding="utf-8"))
    assert session["state"] == body["state"]
    # project.json 进入 remote_path 直入：数据源与样本表落盘。
    assert (project_dir / "project.json").is_file()
    project_json = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    assert project_json["samples"]["source"] == "remote_path"
    assert project_json["samples"]["remote_prestaged"] is True
    assert len(project_json["samples"]["items"]) == 2
    # intake 意图态 input_ready 与 session 真实态分层保留，fastq 目录不丢。
    intake = json.loads((project_dir / "intake.json").read_text(encoding="utf-8"))
    assert intake["state"] == "input_ready"
    assert intake["route"] == "bulk_rna"
    assert intake["fastq"]["remote_fastq_dir"] == "/hwdata/home/yeyulin/demo_fastq"


def test_fastq_session_rejects_samples_not_a_list(tmp_path: Path) -> None:
    """Finding 1：samples 传 dict 而非 list → INVALID_SAMPLES，不得 500."""
    client = TestClient(create_app(project_dir=tmp_path / "legacy"))
    token = _token(client)
    h = _headers(token)
    _create_project(client, token, "wiz_k")

    body = client.post(
        "/api/projects/wiz_k/fastq/session",
        json={
            "data_source": "remote_path",
            "remote_fastq_dir": "/data/demo_fastq",
            "samples": {"sample_id": "A", "condition": "ctrl"},
        },
        headers=h,
    ).json()

    assert body.get("error_code") == "INVALID_SAMPLES", body
    assert "样本对象列表" in body.get("message", "")
    assert not (tmp_path / "wiz_k" / "session.json").is_file()


def test_fastq_session_skips_non_dict_samples(tmp_path: Path) -> None:
    """Finding 1：samples 列表含非 dict 元素必须被跳过而不是 .get 崩溃 → 500."""
    client = TestClient(create_app(project_dir=tmp_path / "legacy"))
    token = _token(client)
    h = _headers(token)
    _create_project(client, token, "wiz_l")

    body = client.post(
        "/api/projects/wiz_l/fastq/session",
        json={
            "data_source": "remote_path",
            "remote_fastq_dir": "/data/demo_fastq",
            "samples": ["CTRL_1", 123],
        },
        headers=h,
    ).json()

    assert body.get("error_code") == "EMPTY_SAMPLES", body
    assert not (tmp_path / "wiz_l" / "session.json").is_file()


def test_fastq_session_mixed_samples_keeps_only_dict_rows(tmp_path: Path) -> None:
    """Finding 1：混合列表中非 dict 元素被剔除，剩余合法样本仍能建 session."""
    client = TestClient(create_app(project_dir=tmp_path / "legacy"))
    token = _token(client)
    h = _headers(token)
    _create_project(client, token, "wiz_m")

    body = client.post(
        "/api/projects/wiz_m/fastq/session",
        json={
            "data_source": "remote_path",
            "remote_fastq_dir": "/hwdata/home/yeyulin/demo_fastq",
            "cancer_type": "crc",
            "design": "independent_two_group",
            "layout": "paired",
            "samples": [
                "IGNORED_NOT_A_DICT",
                123,
                {
                    "sample_id": "CTRL_1",
                    "condition": "control",
                    "fastq_1": "CTRL_1_R1.fastq.gz",
                    "fastq_2": "CTRL_1_R2.fastq.gz",
                },
                {
                    "sample_id": "CASE_1",
                    "condition": "case",
                    "fastq_1": "CASE_1_R1.fastq.gz",
                    "fastq_2": "CASE_1_R2.fastq.gz",
                },
            ],
        },
        headers=h,
    ).json()

    assert "error_code" not in body, body
    assert (tmp_path / "wiz_m" / "session.json").is_file()
    import json

    project_json = json.loads((tmp_path / "wiz_m" / "project.json").read_text(encoding="utf-8"))
    assert [item["sample_id"] for item in project_json["samples"]["items"]] == ["CTRL_1", "CASE_1"]


def test_fastq_session_duplicate_submit_returns_exists_without_rewrite(tmp_path: Path) -> None:
    """Minor：重复提交返回 SESSION_EXISTS 且不改写 intake/session 落盘."""
    client = TestClient(create_app(project_dir=tmp_path / "legacy"))
    token = _token(client)
    h = _headers(token)
    _create_project(client, token, "wiz_n")
    payload = {
        "data_source": "remote_path",
        "remote_fastq_dir": "/hwdata/home/yeyulin/demo_fastq",
        "cancer_type": "crc",
        "design": "independent_two_group",
        "layout": "paired",
        "samples": [
            {"sample_id": "CTRL_1", "condition": "control", "fastq_1": "CTRL_1_R1.fastq.gz", "fastq_2": "CTRL_1_R2.fastq.gz"},
            {"sample_id": "CASE_1", "condition": "case", "fastq_1": "CASE_1_R1.fastq.gz", "fastq_2": "CASE_1_R2.fastq.gz"},
        ],
    }
    first = client.post("/api/projects/wiz_n/fastq/session", json=payload, headers=h).json()
    assert "error_code" not in first, first

    import json

    project_dir = tmp_path / "wiz_n"
    session_before = json.loads((project_dir / "session.json").read_text(encoding="utf-8"))
    intake_before = json.loads((project_dir / "intake.json").read_text(encoding="utf-8"))

    body = client.post("/api/projects/wiz_n/fastq/session", json=payload, headers=h).json()
    assert body.get("error_code") == "SESSION_EXISTS", body
    assert "已存在" in body.get("error", "")

    session_after = json.loads((project_dir / "session.json").read_text(encoding="utf-8"))
    intake_after = json.loads((project_dir / "intake.json").read_text(encoding="utf-8"))
    assert session_after == session_before
    assert intake_after == intake_before


def test_workbench_renders_galaxy_style_regions(tmp_path: Path) -> None:
    client = TestClient(create_app(project_dir=tmp_path / "legacy"))
    page = client.get("/workbench").text

    assert 'id="toolPanel"' in page
    assert 'id="mainWorkspace"' in page
    assert 'id="historyPanel"' in page
    assert 'id="agentConversation"' in page
    assert "History" in page


def test_index_labels_setup_state_for_new_projects(tmp_path: Path) -> None:
    client = TestClient(create_app(project_dir=tmp_path / "legacy"))
    token = _token(client)
    h = _headers(token)
    _create_project(client, token, "wiz_home")

    projects = client.get("/api/projects", headers=h).json()["projects"]
    assert next(p for p in projects if p["project_id"] == "wiz_home")["state"] == "setup"
