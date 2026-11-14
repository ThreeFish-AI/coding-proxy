"""旧 YAML → config.db 导入：等价矩阵（I1）与启动引导 boot()."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from coding.proxy.config import importer
from coding.proxy.config.home import (
    config_db_path,
    legacy_home_yaml_path,
    proxy_home,
)
from coding.proxy.config.importer import (
    ImportPlanError,
    LegacyConfigError,
    StaleYamlError,
    _expand_paths,
    apply_import,
    boot,
    file_sha256,
    make_marker,
    plan_from_raw,
    plan_import,
    read_marker,
    verify_equivalence,
)
from coding.proxy.config.loader import _get_default_config_path, load_config
from coding.proxy.config.normalize import VENDOR_SECRET_FIELD_NAMES
from coding.proxy.config.resolver import resolve_config
from coding.proxy.config.secrets import LEGACY_VENDOR_SECTIONS, SecretCipher
from coding.proxy.config.service import ConfigService
from coding.proxy.config.store import ConfigStore

FIXTURES = Path(__file__).parent / "fixtures"
LITERAL_KEY = "sk-literal-zhipu-0123456789-PLANTED"
LITERAL_GH = "ghp_literal_github_token_PLANTED_42"
LITERAL_GOOGLE = "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D"

VENDORS_ONLY = """
vendors:
  - vendor: anthropic
    base_url: "https://api.anthropic.com"
  - vendor: zhipu
    api_key: "${MY_ZHIPU_KEY}"
    timeout_ms: 1000
"""

LEGACY_FLAT = """
primary:
  enabled: true
  timeout_ms: 1000
copilot:
  enabled: true
  github_token: "${GITHUB_TOKEN}"
fallback:
  api_key: "literal-fallback-key-0123456789"
circuit_breaker:
  failure_threshold: 5
quota_guard:
  enabled: true
  token_budget: 100
"""

ALIAS_KEYS = """
anthropic:
  timeout_ms: 1234
zhipu:
  api_key: "${ZHIPU_ALIAS_KEY}"
server:
  port: 4000
"""

PRICING = """
pricing:
  - vendor: zhipu
    model: glm-x
    input_cost_per_mtok: ¥2.00
    output_cost_per_mtok: ¥8
  - vendor: anthropic
    model: claude-x
    input_cost_per_mtok: $2
    cache_read_cost_per_mtok: 0.1
"""

NATIVE = """
native_api:
  gemini:
    base_url: "https://gemini.yaml.example"
"""

TEMPLATE_NON_SECRET = """
native_api:
  openai:
    base_url: "https://${OPENAI_HOST}/x"
logging:
  file: "${LOG_DIR}/proxy.log"
vendors:
  - vendor: zhipu
    base_url: "${ZHIPU_BASE}"
    api_key: "${ZHIPU_API_KEY}"
tiers: ["zhipu"]
"""

LITERAL_SECRETS = f"""
vendors:
  - vendor: zhipu
    api_key: "{LITERAL_KEY}"
  - vendor: copilot
    github_token: ""
auth:
  google_client_secret: "{LITERAL_GOOGLE}"
copilot:
  enabled: true
  github_token: "{LITERAL_GH}"
"""


# legacy / alias section 中的 vendor 密钥字段（旧 validator 会把它们带进 vendor）
SECTION_SECRETS = {
    "primary": "sk-ant-PLANTED-primary-0123456789abcdef",
    "copilot": "sk-PLANTED-copilot-extra-api-key-0123",
    "fallback": "PLANTED-fallback-client-secret-0123456",
    "anthropic": "sk-ant-PLANTED-alias-0123456789abcdef",
    "zhipu": "ghp_PLANTED_zhipu_alias_github_token_01",
}
LEGACY_SECTION_SECRETS = {
    "legacy_flat": f"""
primary:
  enabled: true
  api_key: "{SECTION_SECRETS["primary"]}"
copilot:
  enabled: true
  github_token: "{LITERAL_GH}"
  api_key: "{SECTION_SECRETS["copilot"]}"
fallback:
  client_secret: "{SECTION_SECRETS["fallback"]}"
""",
    "alias_keys": f"""
anthropic:
  api_key: "{SECTION_SECRETS["anthropic"]}"
zhipu:
  github_token: "{SECTION_SECRETS["zhipu"]}"
""",
    # 最常见的 alias 形态：目标模型（ZhipuConfig）确有 api_key 字段
    "alias_zhipu_api_key": f"""
zhipu:
  api_key: "{SECTION_SECRETS["zhipu"]}"
""",
    # alias 与目标字段同时存在：alias 被 ProxyConfig 忽略
    "alias_and_target": f"""
anthropic:
  api_key: "{SECTION_SECRETS["anthropic"]}"
primary:
  api_key: "{SECTION_SECRETS["primary"]}"
""",
}


def _write(text: str, name: str = "user.yaml") -> Path:
    path = Path.cwd().parent / name
    path.write_text(text, encoding="utf-8")
    return path


def _assert_equivalent(path: Path) -> None:
    plan = plan_import(path)
    assert verify_equivalence(plan, path) == []
    apply_import(
        ConfigStore(config_db_path()),
        SecretCipher(proxy_home()),
        plan,
        make_marker(path, plan.source_sha256),
    )
    resolved = resolve_config()
    assert resolved.mode == "store"
    assert _expand_paths(resolved.config) == _expand_paths(load_config(path))


MATRIX = {
    "current_default_verbatim": None,
    "old_full_copy_v052": None,
    "vendors_only_without_tiers": VENDORS_ONLY,
    "legacy_flat": LEGACY_FLAT,
    "alias_keys": ALIAS_KEYS,
    "pricing_currency": PRICING,
    "native_yaml_value": NATIVE,
    "template_in_non_secret": TEMPLATE_NON_SECRET,
    "literal_secrets": LITERAL_SECRETS,
}


@pytest.fixture
def matrix_env(monkeypatch):
    monkeypatch.setenv("MY_ZHIPU_KEY", "zhipu-from-env")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-from-env")
    monkeypatch.setenv("ZHIPU_ALIAS_KEY", "alias-from-env")
    monkeypatch.setenv("OPENAI_HOST", "openai.example")
    monkeypatch.setenv("LOG_DIR", "/tmp/logs")
    monkeypatch.setenv("ZHIPU_BASE", "https://zhipu.example")
    monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-default-env")


class TestEquivalenceMatrix:
    @pytest.mark.parametrize("case", list(MATRIX))
    def test_import_equivalent_to_legacy(self, case: str, matrix_env):
        if case == "current_default_verbatim":
            path = Path.cwd().parent / "user.yaml"
            shutil.copy(_get_default_config_path(), path)
        elif case == "old_full_copy_v052":
            path = Path.cwd().parent / "user.yaml"
            shutil.copy(FIXTURES / "legacy_config_default_v052.yaml", path)
        else:
            path = _write(MATRIX[case])
        _assert_equivalent(path)

    def test_current_default_copy_has_no_overrides(self):
        path = Path.cwd().parent / "user.yaml"
        shutil.copy(_get_default_config_path(), path)
        plan = plan_import(path)
        assert plan.sections == {} and plan.secrets == {}

    def test_old_full_copy_only_overrides_real_differences(self):
        path = Path.cwd().parent / "user.yaml"
        shutil.copy(FIXTURES / "legacy_config_default_v052.yaml", path)
        plan = plan_import(path)
        # 旧拷贝只与当前默认在 zhipu 配额与 pricing 上不同；路径字面量被丢弃
        assert set(plan.sections) == {"vendors", "pricing"}
        assert set(plan.sections["vendors"]) == {"zhipu"}
        assert "database" not in plan.sections and "auth" not in plan.sections
        assert plan.secrets["vendors.zhipu.api_key"].env_var == "ZHIPU_API_KEY"

    def test_vendors_without_tiers_materialises_null(self):
        plan = plan_import(_write(VENDORS_ONLY))
        assert plan.sections["tiers"] is None
        assert set(plan.sections["vendors"]["$absent"]) >= {"copilot", "minimax"}

    def test_legacy_flat_keeps_hidden_sections_without_secrets(self, matrix_env):
        plan = plan_import(_write(LEGACY_FLAT))
        assert plan.sections["tiers"] is None
        assert "github_token" not in plan.sections["copilot"]
        assert "api_key" not in plan.sections.get("fallback", {})
        assert plan.secrets["copilot.github_token"].env_var == "GITHUB_TOKEN"
        assert plan.secrets["fallback.api_key"].kind == "stored"

    def test_currency_difference_is_detected(self):
        path = _write(PRICING)
        plan = plan_import(path)
        plan.sections["pricing"][0]["input_cost_per_mtok"] = "$2.00"
        plan.sections["pricing"][0]["output_cost_per_mtok"] = "$8"
        assert any(p.startswith("pricing") for p in verify_equivalence(plan, path))

    def test_native_env_override_preserved(self, monkeypatch):
        monkeypatch.setenv("NATIVE_OPENAI_BASE_URL", "https://env.example")
        path = _write(NATIVE)
        _assert_equivalent(path)
        resolved = resolve_config()
        assert resolved.config.native_api.openai.base_url == "https://env.example"
        assert resolved.sources["native_api.openai.base_url"] == "env"

    def test_non_secret_template_stays_dynamic(self, matrix_env, monkeypatch):
        path = _write(TEMPLATE_NON_SECRET)
        _assert_equivalent(path)
        stored = ConfigStore(config_db_path()).snapshot().sections
        assert stored["native_api"]["openai"]["base_url"] == {
            "$tmpl": "https://${OPENAI_HOST}/x"
        }
        monkeypatch.setenv("OPENAI_HOST", "changed.example")
        cfg = resolve_config().config
        assert cfg.native_api.openai.base_url == "https://changed.example/x"

    def test_literal_secrets_never_plaintext_in_db(self):
        path = _write(LITERAL_SECRETS)
        _assert_equivalent(path)
        data = config_db_path().read_bytes()
        for secret in (LITERAL_KEY, LITERAL_GH, LITERAL_GOOGLE):
            assert secret.encode() not in data
        cfg = resolve_config().config
        assert (
            next(v for v in cfg.vendors if v.vendor == "zhipu").api_key == LITERAL_KEY
        )
        assert cfg.copilot.github_token == LITERAL_GH
        assert cfg.auth.google_client_secret == LITERAL_GOOGLE

    def test_plan_repr_hides_plaintext(self):
        plan = plan_import(_write(LITERAL_SECRETS))
        assert LITERAL_KEY not in repr(plan)

    def test_mixed_template_secret_rejected(self):
        with pytest.raises(ImportPlanError) as exc_info:
            plan_from_raw({"vendors": [{"vendor": "zhipu", "api_key": "Bearer ${X}"}]})
        assert "vendors.zhipu.api_key" in str(exc_info.value)

    def test_duplicate_vendor_rejected(self):
        with pytest.raises(ImportPlanError, match="重复"):
            plan_from_raw({"vendors": [{"vendor": "zhipu"}, {"vendor": "zhipu"}]})

    def test_unknown_only_keys_rejected(self):
        with pytest.raises(ImportPlanError):
            plan_from_raw({"foo": 1})
        plan = plan_from_raw({"foo": 1, "server": {"port": 1}})
        assert plan.sections == {"server": {"port": 1}}
        assert plan.warnings


class TestLegacySectionSecrets:
    @pytest.mark.parametrize("case", list(LEGACY_SECTION_SECRETS))
    def test_never_plaintext_in_db_or_export(self, case: str):
        home_yaml = legacy_home_yaml_path()
        home_yaml.parent.mkdir(parents=True)
        home_yaml.write_text(LEGACY_SECTION_SECRETS[case], encoding="utf-8")
        plan = plan_import(home_yaml)
        for section in LEGACY_VENDOR_SECTIONS:
            assert not set(plan.sections.get(section) or {}) & set(
                VENDOR_SECRET_FIELD_NAMES
            )

        result = boot(None)
        assert result.resolved.mode == "store", result.resolved.errors
        assert _expand_paths(result.resolved.config) == _expand_paths(
            load_config(home_yaml)
        )
        data = config_db_path().read_bytes()
        export = ConfigService().export_yaml()
        for secret in [*SECTION_SECRETS.values(), LITERAL_GH]:
            assert secret.encode() not in data
            assert secret not in export

        # 导出（<redacted>）→ 再导入：保留已存密钥，配置与存储均不变
        before = resolve_config().config
        snap_before = ConfigStore(config_db_path()).snapshot()
        exported = _write(export, "export.yaml")
        summary = ConfigService().import_yaml(exported, dry_run=False)
        assert (summary["diffs"], summary["changes"]) == ([], [])
        assert resolve_config().config == before
        snap_after = ConfigStore(config_db_path()).snapshot()
        assert snap_after.sections == snap_before.sections
        assert snap_after.secrets == snap_before.secrets

    def test_secret_only_section_keeps_no_row_and_survives_rewrite(self):
        """仅含密钥的 alias section 不保留空行；回写有效值不会丢失密钥注入."""
        legacy_home_yaml_path().parent.mkdir(parents=True)
        legacy_home_yaml_path().write_text(
            LEGACY_SECTION_SECRETS["alias_zhipu_api_key"], encoding="utf-8"
        )
        assert boot(None).resolved.mode == "store"
        snap = ConfigStore(config_db_path()).snapshot()
        assert "zhipu" not in snap.sections
        assert snap.secrets["zhipu.api_key"].kind == "stored"
        assert resolve_config().config.fallback.api_key == SECTION_SECRETS["zhipu"]

        service = ConfigService()
        service.set_path("zhipu", {})
        assert "zhipu" not in ConfigStore(config_db_path()).snapshot().sections
        assert resolve_config().config.fallback.api_key == SECTION_SECRETS["zhipu"]


class TestBoot:
    def test_no_source_creates_empty_store(self):
        result = boot(None)
        assert result.resolved.mode == "store"
        snap = ConfigStore(config_db_path()).snapshot()
        assert snap.sections == {}
        assert read_marker(snap)["source"] is None
        assert result.resolved.config == load_config(None)

    def test_imports_home_yaml_without_touching_it(self):
        legacy_home_yaml_path().parent.mkdir(parents=True)
        legacy_home_yaml_path().write_text("server:\n  port: 4567\n")
        before = legacy_home_yaml_path().read_bytes()
        result = boot(None)
        assert result.resolved.config.server.port == 4567
        assert legacy_home_yaml_path().read_bytes() == before
        marker = read_marker(ConfigStore(config_db_path()).snapshot())
        assert marker["sha256"] == file_sha256(legacy_home_yaml_path())
        assert any("可删除" in n for n in result.notices)
        # 同内容再次启动：只给提示
        again = boot(None)
        assert again.resolved.config.server.port == 4567
        assert any("已迁移" in n for n in again.notices)

    def test_stale_yaml_rejected(self):
        boot(None)
        legacy_home_yaml_path().write_text("server:\n  port: 1111\n")
        with pytest.raises(StaleYamlError) as exc_info:
            boot(None)
        assert "coding-proxy config import" in str(exc_info.value)

    def test_cwd_config_never_auto_imported(self):
        Path("config.yaml").write_text("server:\n  port: 7777\n")
        with pytest.raises(StaleYamlError) as exc_info:
            boot(None)
        assert exc_info.value.first_boot
        assert "已导入" not in str(exc_info.value)
        assert "start -c" in str(exc_info.value)
        assert not config_db_path().exists()

    def test_third_party_cwd_yaml_is_ignored(self):
        Path("config.yaml").write_text("foo: bar\nlint: {rules: []}\n")
        assert boot(None).resolved.mode == "store"
        again = boot(None)
        assert again.resolved.mode == "store"
        assert not any("config.yaml" in n for n in again.notices)

    def test_edited_home_yaml_source_rejected(self):
        legacy_home_yaml_path().parent.mkdir(parents=True)
        legacy_home_yaml_path().write_text("server:\n  port: 4567\n")
        boot(None)
        legacy_home_yaml_path().write_text("server:\n  port: 4568\n")
        with pytest.raises(StaleYamlError) as exc_info:
            boot(None)
        assert exc_info.value.path == legacy_home_yaml_path()
        assert not exc_info.value.first_boot

    def test_explicit_path_ignores_other_candidates(self):
        home_yaml = legacy_home_yaml_path()
        home_yaml.parent.mkdir(parents=True)
        shutil.copy(FIXTURES / "legacy_config_default_v052.yaml", home_yaml)
        Path("config.yaml").write_text("server:\n  port: 1\n")
        mine = _write("server:\n  port: 4998\n", "mine.yaml")
        # 首启只导入 -c 文件；旧版 -c 本就不读 home / 当前目录的 YAML
        assert boot(mine).resolved.config.server.port == 4998
        assert boot(mine).resolved.config.server.port == 4998
        marker = read_marker(ConfigStore(config_db_path()).snapshot())
        assert marker["source"] == str(mine.resolve())

        # 不带 -c：其它候选与导入源不同只提示，不拒绝启动
        result = boot(None)
        assert result.resolved.config.server.port == 4998
        for path in (Path("config.yaml"), home_yaml):
            assert any(str(path) in n and "不一致" in n for n in result.notices)
        assert any(str(mine) in n and "已迁移" in n for n in result.notices)

        mine.write_text("server:\n  port: 4999\n")
        with pytest.raises(StaleYamlError) as exc_info:
            boot(mine)
        assert exc_info.value.path == mine

    def test_redacted_secret_at_first_boot_runs_legacy_readonly(self):
        path = _write(
            'vendors:\n  - vendor: zhipu\n    api_key: "<redacted>"\n'
            "    timeout_ms: 1\n",
            "exported.yaml",
        )
        result = boot(path)
        assert result.resolved.mode == "legacy_readonly"
        assert result.resolved.errors[0]["loc"] == ["<import>"]
        assert "vendors.zhipu.api_key" in result.resolved.errors[0]["msg"]
        assert result.resolved.config == load_config(path)
        assert not config_db_path().exists()

    def test_explicit_path_import_and_change_detection(self):
        path = _write("server:\n  port: 5555\n", "explicit.yaml")
        assert boot(path).resolved.config.server.port == 5555
        assert boot(path).resolved.config.server.port == 5555
        path.write_text("server:\n  port: 5556\n")
        with pytest.raises(StaleYamlError):
            boot(path)

    def test_missing_explicit_path_uses_defaults(self):
        result = boot(Path.cwd() / "missing.yaml")
        assert result.resolved.config.server.port == 3392

    def test_plan_error_falls_back_to_legacy_readonly(self):
        path = _write(
            'vendors:\n  - vendor: zhipu\n    api_key: "Bearer ${X}"\n', "bad.yaml"
        )
        result = boot(path)
        assert result.resolved.mode == "legacy_readonly"
        assert result.resolved.config == load_config(path)
        assert result.resolved.errors and "Bearer" not in str(result.resolved.errors)
        assert not config_db_path().exists()

    def test_equivalence_failure_leaves_store_untouched(self, monkeypatch):
        path = _write("server:\n  port: 6000\n")
        monkeypatch.setattr(
            importer, "verify_equivalence", lambda *a, **k: ["server.port"]
        )
        result = boot(path)
        assert result.resolved.mode == "legacy_readonly"
        assert result.resolved.errors == [
            {"loc": ["server.port"], "msg": "import mismatch"}
        ]
        assert result.resolved.config.server.port == 6000
        assert not config_db_path().exists()

    def test_invalid_legacy_yaml_raises(self):
        path = _write("server:\n  port: not-a-port\n")
        with pytest.raises(LegacyConfigError) as exc_info:
            boot(path)
        assert "not-a-port" not in str(exc_info.value)
        assert not config_db_path().exists()

    def test_invalid_stored_config_is_degraded(self):
        boot(None)
        with ConfigStore(config_db_path()).transaction() as txn:
            txn.put_section("server", {"port": "PLANTED-not-int"})
        result = boot(None)
        assert result.resolved.mode == "degraded"
        assert result.resolved.config is None
        assert result.resolved.errors[0]["loc"] == ["server", "port"]
        assert "PLANTED" not in str(result.resolved.errors)
