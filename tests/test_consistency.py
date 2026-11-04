"""验证依赖记录、配置模板、文档与实际接口不会相互分叉。"""
import ast
import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import main


class ConsistencyTests(unittest.TestCase):
    def test_configuration_template_defaults_match_code(self):
        code = ast.parse((ROOT / "app/main.py").read_text(encoding="utf-8"))

        def integer(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, int):
                return node.value
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
                return integer(node.left) * integer(node.right)
            raise AssertionError("Unexpected default expression")

        template = (ROOT / ".env.example").read_text(encoding="utf-8")
        for node in ast.walk(code):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_int_env":
                key, default = node.args
                match = re.search(r"^#?" + re.escape(key.value) + r"=(\d+)\b", template, re.M)
                self.assertIsNotNone(match, key.value)
                self.assertEqual(int(match.group(1)), integer(default), key.value)

    def test_documented_routes_exist_and_health_is_not_a_proxy_check(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        routes = {(method, route.path) for route in main.app.routes for method in route.methods}
        for method, path in re.findall(r"\| (GET|POST) \| `([^`]+)`", readme):
            self.assertIn((method, path.split("?", 1)[0]), routes)
        self.assertIn("不检测代理或 Telegram", readme)

    def test_dependency_versions_and_vendored_integrity_match_manifest(self):
        record = json.loads((ROOT / "docs/dependencies.json").read_text(encoding="utf-8"))
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        dependencies = record["python_dependencies"] + [record["additional_python_dependency"]]
        for dependency in dependencies:
            name = re.escape(dependency["distribution"]).replace(r"\-", "[-_]")
            self.assertRegex(requirements, name + r"(?:\[standard\])?==" + re.escape(dependency["version"]))
        vendored = record["vendored_javascript"]
        asset = (ROOT / vendored["file"]).read_bytes()
        self.assertEqual(hashlib.sha256(asset).hexdigest(), vendored["sha256"])
        self.assertIn(("@version " + vendored["version"]).encode(), asset)
        self.assertTrue((ROOT / vendored["license_file"]).is_file())

    def test_dotenv_comments_quotes_precedence_and_literal_dollars(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                'TTL_SECONDS=123 # supported inline comment\n'
                'PROXY_PASS="literal ${NO_EXPANSION}"\n'
                'FLOW_SLOTS=4\n', encoding="utf-8")
            with patch.object(main, "PROJECT_ROOT", root), patch.dict(os.environ, {"FLOW_SLOTS": "7"}, clear=True):
                main._load_env()
                self.assertEqual(main._int_env("TTL_SECONDS", 600), 123)
                self.assertEqual(os.environ["PROXY_PASS"], "literal ${NO_EXPANSION}")
                self.assertEqual(main._int_env("FLOW_SLOTS", 5), 7)


if __name__ == "__main__":
    unittest.main()
