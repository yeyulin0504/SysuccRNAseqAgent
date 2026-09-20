from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest


SRC = Path(__file__).parents[1] / "src" / "rnaseq_agent"
GATEWAY = "model_provider.py"


@dataclass(frozen=True)
class TransportCall:
    path: str
    function: str
    lineno: int
    family: str
    spelling: str


ALLOWED_NON_GENERATIVE = {
    ("bkbio_eval_adapter.py", "_git_commit", "subprocess.run"),
    ("execution.py", "run_command", "subprocess.run"),
    ("execution.py", "run_command_bounded", "subprocess.Popen"),
    ("llm.py", "codex_login_status", "subprocess.run"),
    ("llm.py", "launch_codex_device_login", "subprocess.Popen"),
}

ALLOWED_GATEWAY = {
    (GATEWAY, "ModelProviderGateway.complete"),
    (GATEWAY, "ModelProviderGateway.stream"),
    (GATEWAY, "ModelProviderGateway.responses"),
    (GATEWAY, "ModelProviderGateway.codex_exec"),
    (GATEWAY, "ModelProviderGateway.dispatch_exact"),
    (GATEWAY, "ModelProviderGateway.list_models"),
}


class TransportVisitor(ast.NodeVisitor):
    """Resolve direct transport calls while retaining their exact owner."""

    HTTP = {
        "requests.get",
        "requests.post",
        "requests.request",
        "requests.Session().get",
        "requests.Session().post",
        "requests.Session().request",
        "httpx.get",
        "httpx.post",
        "httpx.request",
        "httpx.Client().get",
        "httpx.Client().post",
        "httpx.Client().request",
        "httpx.AsyncClient().get",
        "httpx.AsyncClient().post",
        "httpx.AsyncClient().request",
        "urllib.request.urlopen",
    }
    PROCESS = {
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.check_output",
        "subprocess.check_call",
    }

    def __init__(self, path: str) -> None:
        self.path = path
        self.aliases: dict[str, str] = {}
        self.instances: dict[str, str] = {}
        self.scope: list[str] = []
        self.calls: list[TransportCall] = []

    def visit_Import(self, node: ast.Import) -> None:
        for item in node.names:
            if item.asname:
                self.aliases[item.asname] = item.name
            else:
                root = item.name.split(".")[0]
                self.aliases[root] = root

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for item in node.names:
            if item.name == "*":
                continue
            self.aliases[item.asname or item.name] = f"{module}.{item.name}".strip(".")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Assign(self, node: ast.Assign) -> None:
        created = self._resolve(node.value)
        if created in {"requests.Session()", "httpx.Client()", "httpx.AsyncClient()"}:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.instances[target.id] = created
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            created = self._resolve(node.value)
            if created in {"requests.Session()", "httpx.Client()", "httpx.AsyncClient()"}:
                if isinstance(node.target, ast.Name):
                    self.instances[node.target.id] = created
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        spelling = self._resolve(node.func)
        family = "http" if spelling in self.HTTP else "process" if spelling in self.PROCESS else ""
        if family:
            self.calls.append(
                TransportCall(
                    path=self.path,
                    function=".".join(self.scope) or "<module>",
                    lineno=node.lineno,
                    family=family,
                    spelling=spelling,
                )
            )
        self.generic_visit(node)

    def _resolve(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return self.instances.get(node.id, self.aliases.get(node.id, node.id))
        if isinstance(node, ast.Attribute):
            return f"{self._resolve(node.value)}.{node.attr}"
        if isinstance(node, ast.Call):
            return self._resolve(node.func) + "()"
        return ""


def scan_source(source: str, *, path: str) -> tuple[TransportCall, ...]:
    tree = ast.parse(source, filename=path)
    visitor = TransportVisitor(path)
    visitor.visit(tree)
    return tuple(visitor.calls)


def scan_package() -> tuple[TransportCall, ...]:
    return tuple(
        call
        for path in sorted(SRC.rglob("*.py"))
        for call in scan_source(
            path.read_text(encoding="utf-8"),
            path=str(path.relative_to(SRC)),
        )
    )


def test_all_transport_calls_have_an_exact_owner() -> None:
    violations = []
    for call in scan_package():
        owner = (call.path, call.function)
        allowed_non_gen = (call.path, call.function, call.spelling) in ALLOWED_NON_GENERATIVE
        if owner not in ALLOWED_GATEWAY and not allowed_non_gen:
            violations.append(call)
    assert violations == []


def test_known_generative_entry_paths_reference_gateway() -> None:
    required = {
        "chat_graph.py": ("ModelContextBuilder", "ModelProviderGateway", "PreparedModelRequest"),
        "webapp.py": ("ModelContextBuilder", "ModelProviderGateway", "context_free_prepared_request"),
        "llm.py": ("ModelContextBuilder", "ModelProviderGateway", "context_free_prepared_request"),
    }
    for name, symbols in required.items():
        source = (SRC / name).read_text(encoding="utf-8")
        assert all(symbol in source for symbol in symbols), (name, symbols)


@pytest.mark.parametrize(
    "source",
    [
        "import requests as rq\ndef leak(): rq.request('POST', 'https://x')",
        "from requests import post as send\ndef leak(): send('https://x')",
        "import requests\ndef leak(): requests.Session().post('https://x')",
        "import requests\ndef leak():\n s = requests.Session()\n s.get('https://x')",
        "import httpx as hx\ndef leak(): hx.Client().request('POST', 'https://x')",
        "import httpx\ndef leak():\n c = httpx.AsyncClient()\n c.post('https://x')",
        "import urllib.request\ndef leak(): urllib.request.urlopen('https://x')",
        "from urllib.request import urlopen as open_url\ndef leak(): open_url('https://x')",
        "import subprocess\ndef leak(): subprocess.Popen(['codex', 'exec'])",
        "import subprocess as sp\ndef leak(): sp.check_output(['codex', 'exec'])",
        "from subprocess import check_call as invoke\ndef leak(): invoke(['codex', 'exec'])",
    ],
)
def test_detector_rejects_synthetic_aliases_and_alternate_transports(source: str) -> None:
    assert scan_source(source, path="synthetic.py")
