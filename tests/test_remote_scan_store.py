from dataclasses import replace

import pytest

from rnaseq_agent.remote_browse import (
    BrowseContext,
    BrowseDirectoryGroup,
    BrowseResult,
    BrowseSampleRow,
    _result,
)
from rnaseq_agent.remote_scan_store import (
    RemoteScanReference,
    StoredRemoteScan,
    store_remote_scan,
)


def test_store_remote_scan_returns_opaque_reference(tmp_path):
    ctx = BrowseContext("p1", "t1", "llm_tool")
    result = _result(
        context=ctx,
        event_id="e1" * 16,
        started_at="2026-09-17T12:00:00Z",
        identity=None,
        requested_path="/srv/team/run-1",
        canonical_target="/srv/team/run-1",
        root_id="root_1",
        revision="sha256:policy",
        payload=__import__("rnaseq_agent.remote_browse", fromlist=["BrowseScanPayload"]).BrowseScanPayload(
            (BrowseDirectoryGroup("g1", "/srv/team/run-1", (BrowseSampleRow("s1", "s1_R1.fastq.gz", "s1_R2.fastq.gz"),), ()),),
            1,
            0,
            False,
        ),
        authorization={"root_id": "root_1", "canonical_root": "/srv/team", "canonical_target": "/srv/team/run-1", "policy_revision": "sha256:policy"},
    )
    ref = store_remote_scan(tmp_path, result)
    assert isinstance(ref, RemoteScanReference)
    assert ref.project_id == "p1"
    assert ref.source_ref.startswith("src_")
    assert ref.scan_id.startswith("scan_")

