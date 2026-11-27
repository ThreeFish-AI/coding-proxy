"""ConfigService：写入边界、密钥操作、目标地址规则、错误脱敏、导出."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
import yaml

from coding.proxy.config.defaults import default_vendor_dict
from coding.proxy.config.home import config_db_path, legacy_home_yaml_path
from coding.proxy.config.importer import ImportPlanError, boot, ensure_store_ready
from coding.proxy.config.normalize import strip_vendor_secrets
from coding.proxy.config.resolver import resolve_config
from coding.proxy.config.service import (
    BOOTSTRAP_MSG,
    ConfigService,
    ConfigValidationError,
)
from coding.proxy.config.store import ConfigStore, RevisionConflict

PLANTED = "sk-PLANTED-secret-value-0123456789"


@pytest.fixture
def service() -> ConfigService:
    ensure_store_ready()
    return ConfigService()


def _http(service: ConfigService, payload: dict, **kw):
    return service.apply_changes(
        payload, actor="dashboard", allow_bootstrap=False, allow_env_any=False, **kw
    )


def _cli(service: ConfigService, payload: dict):
    return service.apply_changes(
        payload, actor="cli", allow_bootstrap=True, allow_env_any=True
    )


def _vendors(service: ConfigService) -> list[dict]:
    return service.view()["values"]["vendors"]


def _with_vendor(service: ConfigService, vendor_type: str, **changes) -> list[dict]:
    vendors = _vendors(service)
    for vendor in vendors:
        if vendor["vendor"] == vendor_type:
            vendor.update(changes)
    return vendors


def _errors(exc_info) -> list[dict]:
    return exc_info.value.errors


def _locs(exc_info) -> list[list]:
    return [e["loc"] for e in _errors(exc_info)]


def _api_key(cfg, vendor_type: str = "zhipu") -> str:
    return next(v for v in cfg.vendors if v.vendor == vendor_type).api_key


def _boot_home_yaml(text: str) -> ConfigService:
    legacy_home_yaml_path().parent.mkdir(parents=True)
    legacy_home_yaml_path().write_text(text, encoding="utf-8")
    assert boot(None).resolved.mode == "store"
    return ConfigService()


def _snapshot():
    return ConfigStore(config_db_path()).snapshot()


ZHIPU_OVERRIDDEN_NO_KEY = """
vendors:
  - vendor: anthropic
  - vendor: zhipu
    timeout_ms: 1234
tiers: [zhipu, anthropic]
"""


class TestBoundary:
    def test_bootstrap_rejected_over_http(self, service):
        for section in ("server", "logging", "database", "auth", "primary", "zhipu"):
            with pytest.raises(ConfigValidationError) as exc_info:
                _http(
                    service,
                    {"revisions": {section: 0}, "sections": {section: {}}},
                )
            assert _errors(exc_info) == [{"loc": [section], "msg": BOOTSTRAP_MSG}]
        result = _cli(
            service,
            {"revisions": {"server": 0}, "sections": {"server": {"port": 4001}}},
        )
        assert result.config.server.port == 4001

    def test_missing_revision_and_unknown_section(self, service):
        with pytest.raises(ConfigValidationError) as exc_info:
            _http(service, {"sections": {"tiers": None, "nope": 1}})
        assert ["revisions", "tiers"] in _locs(exc_info)
        assert ["nope"] in _locs(exc_info)

    def test_revision_conflict(self, service):
        _http(service, {"revisions": {"tiers": 0}, "sections": {"tiers": ["zhipu"]}})
        with pytest.raises(RevisionConflict) as exc_info:
            _http(service, {"revisions": {"tiers": 0}, "sections": {"tiers": None}})
        assert exc_info.value.sections == ["tiers"]

    @pytest.mark.parametrize(
        ("url", "ok"),
        [
            ("https://api.example.com/x", True),
            ("http://127.0.0.1:8080", True),
            ("http://localhost", True),
            ("http://[::1]:9000", True),
            ("http://api.example.com", False),
            ("https://user:pw@api.example.com", False),
            ("ftp://api.example.com", False),
        ],
    )
    def test_url_rule(self, service, url, ok):
        payload = {
            "revisions": {"vendors": 0},
            "sections": {"vendors": _with_vendor(service, "zhipu", base_url=url)},
        }
        if ok:
            _http(service, payload)
        else:
            with pytest.raises(ConfigValidationError) as exc_info:
                _http(service, payload)
            assert _locs(exc_info) == [["vendors", "zhipu", "base_url"]]

    def test_field_rules(self, service):
        vendors = _with_vendor(
            service,
            "copilot",
            account_type="personal",
            unknown_field=1,
            quota_guard={"threshold_percent": 120},
            circuit_breaker={"failure_threshold": 0},
            timeout_ms=-5,
        )
        with pytest.raises(ConfigValidationError) as exc_info:
            _http(
                service, {"revisions": {"vendors": 0}, "sections": {"vendors": vendors}}
            )
        locs = _locs(exc_info)
        assert ["vendors", "copilot", "account_type"] in locs
        assert ["vendors", "copilot", "unknown_field"] in locs
        assert ["vendors", "copilot", "quota_guard", "threshold_percent"] in locs
        assert ["vendors", "copilot", "circuit_breaker", "failure_threshold"] in locs
        assert ["vendors", "copilot", "timeout_ms"] in locs

    def test_tiers_and_mapping_rules(self, service):
        with pytest.raises(ConfigValidationError) as exc_info:
            _http(
                service,
                {
                    "revisions": {"tiers": 0, "model_mapping": 0},
                    "sections": {
                        "tiers": ["zhipu", "zhipu"],
                        "model_mapping": [
                            {"pattern": "(", "target": "x", "is_regex": True}
                        ],
                    },
                },
            )
        assert ["tiers"] in _locs(exc_info)
        assert ["model_mapping", 0, "pattern"] in _locs(exc_info)
        vendors = [v for v in _vendors(service) if v["vendor"] != "minimax"]
        with pytest.raises(ConfigValidationError) as exc_info:
            _http(
                service,
                {
                    "revisions": {"tiers": 0, "vendors": 0},
                    "sections": {"tiers": ["minimax"], "vendors": vendors},
                },
            )
        assert _locs(exc_info) == [["tiers"]]

    def test_template_creation_rejected(self, service):
        with pytest.raises(ConfigValidationError) as exc_info:
            _http(
                service,
                {
                    "revisions": {"native_api": 0},
                    "sections": {
                        "native_api": {"openai": {"base_url": {"$tmpl": "${HOME}"}}}
                    },
                },
            )
        assert _locs(exc_info) == [["native_api"]]

    @pytest.mark.parametrize(
        ("section", "loc"),
        [
            ("failover", ["failover", "error_message_patterns", 0]),
            ("model_mapping", ["model_mapping", 0, "target"]),
            ("vendors", ["vendors", "kimi", "base_url"]),
        ],
    )
    def test_literal_dollar_brace_rejected(self, service, section, loc):
        """含 ${ 的字面量经 YAML 导出→导入会变成模板展开，写入侧直接拒绝."""
        values = service.view()["values"]
        if section == "failover":
            value = {"error_message_patterns": ["${HOME}"]}
        elif section == "model_mapping":
            value = values["model_mapping"]
            value[0]["target"] = "${GITHUB_TOKEN}"
        else:
            value = _with_vendor(
                service, "kimi", base_url="https://${GITHUB_TOKEN}.evil.example/api"
            )
        with pytest.raises(ConfigValidationError) as exc_info:
            _http(service, {"revisions": {section: 0}, "sections": {section: value}})
        assert _locs(exc_info) == [loc]
        assert section not in _snapshot().sections

    def test_untouched_imported_template_does_not_block(self, monkeypatch):
        """已导入的 $tmpl 节点原样回写不受字面量规则影响."""
        monkeypatch.setenv("ZB", "https://zb.example.invalid/api")
        service = _boot_home_yaml(
            'vendors:\n  - vendor: zhipu\n    base_url: "${ZB}"\n'
            "  - vendor: anthropic\n"
        )
        vendors = _with_vendor(service, "zhipu", timeout_ms=7)
        result = _http(
            service,
            {
                "revisions": {"vendors": service.revision("vendors")},
                "sections": {"vendors": vendors},
            },
        )
        zhipu = next(v for v in result.config.vendors if v.vendor == "zhipu")
        assert (zhipu.base_url, zhipu.timeout_ms) == (
            "https://zb.example.invalid/api",
            7,
        )

    def test_vendor_secret_fields_in_section_are_rejected(self, service):
        """vendor 内联密钥与非 vendors section 一致：422 拒绝（不静默丢弃）."""
        vendors = _with_vendor(service, "zhipu", api_key=PLANTED, timeout_ms=5)
        with pytest.raises(ConfigValidationError) as exc_info:
            _http(
                service, {"revisions": {"vendors": 0}, "sections": {"vendors": vendors}}
            )
        assert _errors(exc_info) == [
            {
                "loc": ["vendors", "zhipu", "api_key"],
                "msg": "密钥请使用 secret_ops / `config set-secret`",
            }
        ]
        assert PLANTED not in str(exc_info.value)
        assert PLANTED.encode() not in config_db_path().read_bytes()


class TestUntouchedImportedValues:
    YAML = """
vendors:
  - vendor: anthropic
  - vendor: zhipu
    base_url: "http://192.168.1.10:8080/anthropic"
    obsolete_field: 1
"""

    def test_do_not_block_saves(self):
        service = _boot_home_yaml(self.YAML)
        for vendor_type, changes in (
            ("anthropic", {"timeout_ms": 123456}),
            ("zhipu", {"timeout_ms": 5}),
        ):
            vendors = _with_vendor(service, vendor_type, **changes)
            _http(
                service,
                {
                    "revisions": {"vendors": service.revision("vendors")},
                    "sections": {"vendors": vendors},
                },
            )
        service.set_path("vendors.anthropic.timeout_ms", 7)
        cfg = resolve_config().config
        zhipu = next(v for v in cfg.vendors if v.vendor == "zhipu")
        assert zhipu.timeout_ms == 5
        assert zhipu.base_url == "http://192.168.1.10:8080/anthropic"

    @pytest.mark.parametrize(
        ("changes", "loc"),
        [
            ({"base_url": "http://192.168.1.11/x"}, ["vendors", "zhipu", "base_url"]),
            ({"another_field": 1}, ["vendors", "zhipu", "another_field"]),
            ({"timeout_ms": -1}, ["vendors", "zhipu", "timeout_ms"]),
        ],
    )
    def test_changed_values_still_strict(self, changes, loc):
        service = _boot_home_yaml(self.YAML)
        vendors = _with_vendor(service, "zhipu", **changes)
        with pytest.raises(ConfigValidationError) as exc_info:
            _http(
                service,
                {
                    "revisions": {"vendors": service.revision("vendors")},
                    "sections": {"vendors": vendors},
                },
            )
        assert _locs(exc_info) == [loc]


class TestValidationErrorsAreRedacted:
    def test_pydantic_errors_contain_no_input(self, service):
        vendors = _with_vendor(service, "zhipu", timeout_ms=PLANTED)
        payload = {
            "revisions": {"vendors": 0, "server": 0},
            "sections": {"vendors": vendors, "server": {"port": PLANTED}},
            "secret_ops": {"vendors.kimi.api_key": {"op": "set", "value": PLANTED}},
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            _cli(service, payload)
        dumped = json.dumps(_errors(exc_info), ensure_ascii=False)
        assert PLANTED not in dumped and PLANTED not in str(exc_info.value)
        assert ["vendors", "zhipu", "timeout_ms"] in _locs(exc_info)
        assert ["server", "port"] in _locs(exc_info)
        assert PLANTED.encode() not in config_db_path().read_bytes()

    def test_boundary_errors_contain_no_input(self, service):
        vendors = _with_vendor(
            service, "zhipu", base_url=f"http://evil.example/{PLANTED}"
        )
        payload = {
            "revisions": {"vendors": 0},
            "sections": {"vendors": vendors},
            "secret_ops": {
                "vendors.zhipu.api_key": {"op": "set", "value": PLANTED},
                f"vendors.{PLANTED}.api_key": {"op": "set", "value": PLANTED},
            },
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            _http(service, payload)
        messages = json.dumps([e["msg"] for e in _errors(exc_info)], ensure_ascii=False)
        assert PLANTED not in messages

    def test_preflight_failure_persists_nothing(self, service):
        def _boom(cfg):
            raise RuntimeError(PLANTED)

        with pytest.raises(ConfigValidationError) as exc_info:
            _http(
                service,
                {"revisions": {"tiers": 0}, "sections": {"tiers": ["zhipu"]}},
                preflight=_boom,
            )
        assert PLANTED not in json.dumps(_errors(exc_info))
        assert "tiers" not in ConfigStore(config_db_path()).snapshot().sections


class TestSecretOps:
    def test_set_keep_clear_default_env(self, service, monkeypatch):
        monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-env")
        result = _http(
            service,
            {
                "secret_ops": {
                    "vendors.zhipu.api_key": {"op": "set", "value": f" {PLANTED} "}
                }
            },
        )
        assert _api_key(result.config) == PLANTED
        assert PLANTED.encode() not in config_db_path().read_bytes()
        state = service.view()["secrets"]["vendors.zhipu.api_key"]
        assert state["kind"] == "stored" and state["hint"] == "••••6789"

        result = _http(
            service, {"secret_ops": {"vendors.zhipu.api_key": {"op": "keep"}}}
        )
        assert _api_key(result.config) == PLANTED

        result = _http(
            service, {"secret_ops": {"vendors.zhipu.api_key": {"op": "clear"}}}
        )
        assert _api_key(result.config) == ""

        result = _http(
            service, {"secret_ops": {"vendors.zhipu.api_key": {"op": "default_env"}}}
        )
        assert _api_key(result.config) == "zhipu-env"
        record = (
            ConfigStore(config_db_path()).snapshot().secrets["vendors.zhipu.api_key"]
        )
        assert (record.kind, record.env_var) == ("env", "ZHIPU_API_KEY")

    def test_env_binding_cli_only(self, service, monkeypatch):
        monkeypatch.setenv("CUSTOM_VAR", "custom")
        op = {"vendors.kimi.api_key": {"op": "env", "var": "CUSTOM_VAR"}}
        with pytest.raises(ConfigValidationError) as exc_info:
            _http(service, {"secret_ops": op})
        assert _locs(exc_info) == [["secret_ops", "vendors.kimi.api_key"]]
        result = _cli(service, {"secret_ops": op})
        assert _api_key(result.config, "kimi") == "custom"

    def test_invalid_ops(self, service):
        cases = {
            "vendors.zhipu.api_key": {"op": "set", "value": "   "},
            "vendors.anthropic.api_key": {"op": "set", "value": "x"},
            "vendors.kimi.api_key": {"op": "bogus"},
            "auth.google_client_secret": {"op": "default_env"},
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            _http(service, {"secret_ops": cases})
        assert len(_errors(exc_info)) == 4

    def test_set_rejects_control_characters(self, service):
        """含控制字符 / 非 Latin-1 的密钥在写入边界被拒（会成为非法 HTTP header，I2）."""
        cases = ["abc\ndef", "abc\x00def", "abc\x7fdef", "abc\x9bdef", "abc€def"]
        for value in cases:
            with pytest.raises(ConfigValidationError) as exc_info:
                _http(
                    service,
                    {
                        "secret_ops": {
                            "vendors.zhipu.api_key": {"op": "set", "value": value}
                        }
                    },
                )
            assert _locs(exc_info) == [["secret_ops", "vendors.zhipu.api_key", "value"]]
            assert value not in json.dumps(_errors(exc_info), ensure_ascii=False)
            assert value.encode() not in config_db_path().read_bytes()

    def test_set_accepts_printable_latin1(self, service):
        result = _http(
            service,
            {
                "secret_ops": {
                    "vendors.zhipu.api_key": {"op": "set", "value": "caf\xe9-key"}
                }
            },
        )
        assert _api_key(result.config) == "caf\xe9-key"

    def test_target_address_change_clears_secrets(self, service):
        _http(
            service,
            {"secret_ops": {"vendors.zhipu.api_key": {"op": "set", "value": PLANTED}}},
        )
        vendors = _with_vendor(service, "zhipu", base_url="https://other.example")
        result = _http(
            service, {"revisions": {"vendors": 0}, "sections": {"vendors": vendors}}
        )
        assert result.cleared_secrets == ["vendors.zhipu.api_key"]
        assert _api_key(result.config) == ""
        assert service.view()["secrets"]["vendors.zhipu.api_key"]["kind"] == "empty"

        vendors = _with_vendor(service, "zhipu", base_url="https://third.example")
        result = _http(
            service,
            {
                "revisions": {"vendors": service.revision("vendors")},
                "sections": {"vendors": vendors},
                "secret_ops": {
                    "vendors.zhipu.api_key": {"op": "set", "value": "new-key"}
                },
            },
        )
        assert result.cleared_secrets == []
        assert _api_key(result.config) == "new-key"

    def test_target_address_change_clears_default_env_binding(
        self, service, monkeypatch
    ):
        monkeypatch.setenv("GITHUB_TOKEN", "gh-env")
        vendors = _with_vendor(
            service, "copilot", token_url="https://evil-but-https.example"
        )
        result = _http(
            service, {"revisions": {"vendors": 0}, "sections": {"vendors": vendors}}
        )
        assert result.cleared_secrets == ["vendors.copilot.github_token"]
        copilot = next(v for v in result.config.vendors if v.vendor == "copilot")
        assert copilot.github_token == ""

    def test_http_default_env_rejected_for_non_default_target(
        self, service, monkeypatch
    ):
        """HTTP 只能在目标地址为包内默认值时绑定默认 env（防止把进程 env 发往新地址）."""
        monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-env-value")
        _http(
            service,
            {"secret_ops": {"vendors.zhipu.api_key": {"op": "set", "value": PLANTED}}},
        )
        vendors = _with_vendor(service, "zhipu", base_url="https://other.example")
        payload = {
            "revisions": {"vendors": 0},
            "sections": {"vendors": vendors},
            "secret_ops": {"vendors.zhipu.api_key": {"op": "default_env"}},
        }
        with pytest.raises(ConfigValidationError) as exc_info:
            _http(service, payload)
        assert _locs(exc_info) == [["secret_ops", "vendors.zhipu.api_key"]]
        assert "vendors" not in _snapshot().sections
        assert _snapshot().secrets["vendors.zhipu.api_key"].kind == "stored"

        # 已在非默认地址上时同样拒绝（无论本次是否修改地址）
        _http(service, {**payload, "secret_ops": {}})
        with pytest.raises(ConfigValidationError):
            _http(
                service,
                {
                    "secret_ops": {"vendors.zhipu.api_key": {"op": "default_env"}},
                },
            )
        # CLI（文件系统信任域）允许
        result = _cli(service, {"secret_ops": payload["secret_ops"]})
        assert _api_key(result.config) == "zhipu-env-value"

    def test_target_address_change_keeps_explicit_set_and_clear(self, service):
        vendors = _with_vendor(service, "zhipu", base_url="https://other.example")
        result = _http(
            service,
            {
                "revisions": {"vendors": 0},
                "sections": {"vendors": vendors},
                "secret_ops": {"vendors.zhipu.api_key": {"op": "clear"}},
            },
        )
        assert result.cleared_secrets == []
        assert _snapshot().secrets["vendors.zhipu.api_key"].kind == "empty"

    def test_remove_then_readd_with_new_target_clears_secret(
        self, service, monkeypatch
    ):
        """移除 vendor 后以新地址重新添加：遗留的密钥记录不得随之发往新地址."""
        monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-env-value-0123456789")
        _http(
            service,
            {"secret_ops": {"vendors.zhipu.api_key": {"op": "set", "value": PLANTED}}},
        )
        original = _vendors(service)
        view = service.view()
        _http(
            service,
            {
                "revisions": {
                    "vendors": view["revisions"]["vendors"],
                    "tiers": view["revisions"]["tiers"],
                },
                "sections": {
                    "vendors": [v for v in original if v["vendor"] != "zhipu"],
                    "tiers": [t for t in view["values"]["tiers"] if t != "zhipu"],
                },
            },
        )
        assert _snapshot().secrets["vendors.zhipu.api_key"].kind == "stored"
        readd = next(v for v in original if v["vendor"] == "zhipu")
        readd["base_url"] = "https://attacker.example/api"
        result = _http(
            service,
            {
                "revisions": {"vendors": service.revision("vendors")},
                "sections": {
                    "vendors": [*(v for v in original if v["vendor"] != "zhipu"), readd]
                },
            },
        )
        assert result.cleared_secrets == ["vendors.zhipu.api_key"]
        assert _api_key(result.config) == ""
        assert _api_key(resolve_config().config) == ""

    def test_readd_with_default_target_keeps_secret(self, service):
        """以包内默认地址重新添加不算目标变化，密钥照常保留."""
        _http(
            service,
            {"secret_ops": {"vendors.zhipu.api_key": {"op": "set", "value": PLANTED}}},
        )
        original = _vendors(service)
        view = service.view()
        _http(
            service,
            {
                "revisions": {
                    "vendors": view["revisions"]["vendors"],
                    "tiers": view["revisions"]["tiers"],
                },
                "sections": {
                    "vendors": [v for v in original if v["vendor"] != "zhipu"],
                    "tiers": [t for t in view["values"]["tiers"] if t != "zhipu"],
                },
            },
        )
        result = _http(
            service,
            {
                "revisions": {"vendors": service.revision("vendors")},
                "sections": {"vendors": original},
            },
        )
        assert result.cleared_secrets == []
        assert _api_key(result.config) == PLANTED

    def test_adding_absent_vendor_with_new_target_clears_env(self, monkeypatch):
        """$absent vendor 以非默认地址加入：物化出的默认 env 记录被清空."""
        monkeypatch.setenv("KIMI_API_KEY", "kimi-env-value-0123456789")
        service = _boot_home_yaml(
            "vendors:\n  - vendor: anthropic\n  - vendor: zhipu\n"
            '    api_key: "${ZHIPU_API_KEY}"\n'
        )
        view = service.view()
        assert "kimi" in view["values"]["absent_vendors"]
        kimi = next(v for v in view["defaults"]["vendors"] if v["vendor"] == "kimi")
        kimi.update(enabled=True, base_url="https://attacker.example/api")
        result = _http(
            service,
            {
                "revisions": {"vendors": view["revisions"]["vendors"]},
                "sections": {"vendors": [*view["values"]["vendors"], kimi]},
            },
        )
        assert result.cleared_secrets == ["vendors.kimi.api_key"]
        assert _api_key(result.config, "kimi") == ""
        assert _snapshot().secrets["vendors.kimi.api_key"].kind == "empty"
        # 以默认地址加入则保留默认 env 绑定
        kimi_default = next(
            v for v in service.view()["defaults"]["vendors"] if v["vendor"] == "kimi"
        )
        vendors = [v for v in _vendors(service) if v["vendor"] != "kimi"]
        _http(
            service,
            {
                "revisions": {"vendors": service.revision("vendors")},
                "sections": {"vendors": vendors},
            },
        )
        result = _http(
            service,
            {
                "revisions": {"vendors": service.revision("vendors")},
                "sections": {"vendors": [*vendors, kimi_default]},
                "secret_ops": {"vendors.kimi.api_key": {"op": "default_env"}},
            },
        )
        assert result.cleared_secrets == []
        assert _api_key(result.config, "kimi") == "kimi-env-value-0123456789"

    def test_section_secret_fields_rejected(self, service):
        for section, value in (
            ("auth", {"google_client_secret": PLANTED}),
            ("primary", {"enabled": True, "api_key": PLANTED}),
        ):
            with pytest.raises(ConfigValidationError) as exc_info:
                _cli(
                    service,
                    {"revisions": {section: 0}, "sections": {section: value}},
                )
            assert _locs(exc_info) == [
                [section, next(k for k in value if k != "enabled")]
            ]
        assert PLANTED.encode() not in config_db_path().read_bytes()
        _cli(
            service,
            {"secret_ops": {"primary.api_key": {"op": "set", "value": PLANTED}}},
        )
        assert _snapshot().secrets["primary.api_key"].kind == "stored"


class TestSecretSourcePinning:
    """无记录的密钥来源随 vendor 覆盖状态变化；移除覆盖不得悄悄改用默认 env."""

    def _default_zhipu(self) -> dict:
        return strip_vendor_secrets(default_vendor_dict("zhipu"))

    def test_removing_override_keeps_empty_source(self, monkeypatch):
        monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-env-value-0123456789")
        service = _boot_home_yaml(ZHIPU_OVERRIDDEN_NO_KEY)
        assert _api_key(resolve_config().config) == ""
        vendors = [
            self._default_zhipu() if v["vendor"] == "zhipu" else v
            for v in _vendors(service)
        ]
        result = _http(
            service,
            {
                "revisions": {"vendors": service.revision("vendors")},
                "sections": {"vendors": vendors},
            },
        )
        assert "zhipu" not in _snapshot().sections["vendors"]
        assert result.pinned_secrets == ["vendors.zhipu.api_key"]
        assert _api_key(result.config) == ""
        assert _snapshot().secrets["vendors.zhipu.api_key"].kind == "empty"
        assert service.view()["secrets"]["vendors.zhipu.api_key"]["kind"] == "empty"

        result = _http(
            service, {"secret_ops": {"vendors.zhipu.api_key": {"op": "default_env"}}}
        )
        assert _api_key(result.config) == "zhipu-env-value-0123456789"

    def test_reset_vendors_keeps_secret_sources(self, monkeypatch):
        monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-env-value-0123456789")
        service = _boot_home_yaml(ZHIPU_OVERRIDDEN_NO_KEY)
        result = service.reset_section("vendors")
        assert result.pinned_secrets == ["vendors.zhipu.api_key"]
        assert _api_key(result.config) == ""
        assert resolve_config().config == result.config


class TestConcurrency:
    FAILOVER = {"status_codes": [429, 503]}

    def test_preflight_runs_outside_write_lock(self, service):
        started, release = threading.Event(), threading.Event()
        calls: list = []
        out: dict = {}

        def preflight(cfg):
            calls.append(cfg)
            started.set()
            assert release.wait(5)

        def slow_writer():
            try:
                out["result"] = _http(
                    ConfigService(),
                    {
                        "revisions": {"failover": 0},
                        "sections": {"failover": self.FAILOVER},
                    },
                    preflight=preflight,
                )
            except Exception as exc:  # noqa: BLE001
                out["error"] = exc

        thread = threading.Thread(target=slow_writer)
        thread.start()
        try:
            assert started.wait(5)
            began = time.monotonic()
            rules = service.view()["values"]["model_mapping"][:-1]
            _http(
                ConfigService(),
                {
                    "revisions": {"model_mapping": 0},
                    "sections": {"model_mapping": rules},
                },
            )
            assert time.monotonic() - began < 1.5
        finally:
            release.set()
            thread.join(10)
        assert "error" not in out, out.get("error")
        # 存储在 preflight 期间被改动 → 基于新快照重算并再次 preflight
        assert len(calls) == 2
        assert {"failover", "model_mapping"} <= set(_snapshot().sections)
        assert resolve_config().config == out["result"].config

    def test_persistent_concurrent_writes_raise_conflict(self, service):
        other = ConfigService()
        ports = iter(range(4000, 4010))

        def preflight(cfg):
            other.set_path("server.port", next(ports))

        with pytest.raises(RevisionConflict) as exc_info:
            _http(
                service,
                {"revisions": {"failover": 0}, "sections": {"failover": self.FAILOVER}},
                preflight=preflight,
            )
        assert exc_info.value.sections == ["server"]
        assert "failover" not in _snapshot().sections


def test_paths_follow_coding_proxy_home(service, monkeypatch, tmp_path):
    service.set_path("server.port", 4321)
    other = (tmp_path / "other-home").resolve()
    monkeypatch.setenv("CODING_PROXY_HOME", str(other))
    resolved = resolve_config()
    assert resolved.mode == "defaults"
    assert resolved.config.server.port == 3392
    assert Path(resolved.config.database.path).parent == other
    assert Path(resolved.config.auth.token_store_path).parent == other
    assert ConfigService().store.path == other / "config.db"


class TestSectionsAndView:
    def test_minimised_write_and_reset(self, service):
        vendors = _with_vendor(service, "zhipu", timeout_ms=42)
        result = _http(
            service, {"revisions": {"vendors": 0}, "sections": {"vendors": vendors}}
        )
        stored = ConfigStore(config_db_path()).snapshot().sections["vendors"]
        assert set(stored) == {"zhipu"} and stored["zhipu"]["timeout_ms"] == 42
        assert result.changed_sections == ["vendors"]
        assert resolve_config().config == result.config

        result = _http(
            service,
            {
                "revisions": {"vendors": result.revisions["vendors"]},
                "reset": ["vendors"],
            },
        )
        assert result.revisions["vendors"] == 2
        assert "vendors" not in ConfigStore(config_db_path()).snapshot().sections

    def test_default_values_produce_no_override(self, service):
        view = service.view()
        result = _http(
            service,
            {
                "revisions": {"vendors": 0, "pricing": 0},
                "sections": {
                    "vendors": view["values"]["vendors"],
                    "pricing": view["values"]["pricing"],
                },
            },
        )
        assert result.changed_sections == []
        assert ConfigStore(config_db_path()).snapshot().sections == {}

    def test_tiers_null_roundtrip(self, service):
        result = _http(
            service, {"revisions": {"tiers": 0}, "sections": {"tiers": None}}
        )
        assert result.config.tiers is None
        view = service.view()
        assert view["values"]["tiers"] is None
        assert "tiers" in view["overridden"]

    def test_view_payload_shape(self, service, monkeypatch):
        monkeypatch.setenv("NATIVE_GEMINI_BASE_URL", "https://env.example")
        _http(
            service,
            {"secret_ops": {"vendors.zhipu.api_key": {"op": "set", "value": PLANTED}}},
        )
        view = service.view({"tiers": ["zhipu"], "apply_supported": True})
        dumped = json.dumps(view, ensure_ascii=False)
        assert PLANTED not in dumped
        assert view["mode"] == "store"
        assert view["apply_supported"] is True
        assert view["running"]["tiers"] == ["zhipu"]
        assert view["revisions"]["vendors"] == 0
        assert view["sources"]["native_api.gemini.base_url"] == "env"
        assert "google_client_secret" not in view["values"]["auth"]
        assert view["values"]["database"]["path"].endswith("usage.db")
        assert all("api_key" not in v for v in view["values"]["vendors"])
        assert all("api_key" not in v for v in view["defaults"]["vendors"])
        assert view["registry"]["fields"] and view["registry"]["hidden"]
        zhipu_cb = next(v for v in view["values"]["vendors"] if v["vendor"] == "zhipu")
        assert "circuit_breaker" in zhipu_cb

    def test_set_path_and_reset_section(self, service):
        result, bootstrap = service.set_path("vendors.zhipu.quota_guard.enabled", False)
        assert bootstrap is False
        zhipu = next(v for v in result.config.vendors if v.vendor == "zhipu")
        assert zhipu.quota_guard.enabled is False
        result, bootstrap = service.set_path("server.port", 4321)
        assert bootstrap is True and result.config.server.port == 4321
        result, _ = service.set_path("model_mapping.0.target", "x-model")
        assert result.config.model_mapping[0].target == "x-model"
        with pytest.raises(ConfigValidationError):
            service.set_path("vendors.zhipu.api_key", "x")
        with pytest.raises(ConfigValidationError):
            service.set_path("vendors.zhipu.retry.max_retries", 1)
        service.reset_section("server")
        assert resolve_config().config.server.port == 3392


class TestExportImport:
    def test_export_never_contains_stored_plaintext(self, service, monkeypatch):
        _cli(
            service,
            {
                "secret_ops": {
                    "vendors.zhipu.api_key": {"op": "set", "value": PLANTED},
                    "vendors.kimi.api_key": {"op": "env", "var": "KIMI_CUSTOM"},
                    "auth.google_client_secret": {"op": "set", "value": PLANTED + "2"},
                }
            },
        )
        text = service.export_yaml()
        assert PLANTED not in text
        doc = yaml.safe_load(text)
        vendors = {v["vendor"]: v for v in doc["vendors"]}
        assert vendors["zhipu"]["api_key"] == "<redacted>"
        assert vendors["kimi"]["api_key"] == "${KIMI_CUSTOM}"
        assert vendors["copilot"]["github_token"] == "${GITHUB_TOKEN}"
        assert doc["auth"]["google_client_secret"] == "<redacted>"

    def test_export_import_roundtrip(self, service):
        service.set_path("vendors.zhipu.timeout_ms", 99)
        _http(
            service,
            {"secret_ops": {"vendors.zhipu.api_key": {"op": "set", "value": PLANTED}}},
        )
        before = resolve_config().config
        path = Path.cwd().parent / "export.yaml"
        path.write_text(service.export_yaml(), encoding="utf-8")
        summary = service.import_yaml(path, dry_run=True)
        assert summary["diffs"] == [] and summary["applied"] is False
        assert summary["secrets"]["vendors.zhipu.api_key"] == "keep"
        summary = service.import_yaml(path, dry_run=False)
        assert summary["applied"] is True
        assert summary["changes"] == []
        assert resolve_config().config == before

    def test_import_reports_changes_against_current_store(self, service, monkeypatch):
        """导入摘要列出相对当前存储改变的生效字段与密钥来源（不含值）."""
        monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-env-value-0123456789")
        _http(
            service,
            {"secret_ops": {"vendors.zhipu.api_key": {"op": "set", "value": PLANTED}}},
        )
        doc = yaml.safe_load(service.export_yaml())
        doc["server"]["port"] = 4555
        next(v for v in doc["vendors"] if v["vendor"] == "zhipu")["api_key"] = (
            "${ZHIPU_API_KEY}"
        )
        path = Path.cwd().parent / "other.yaml"
        path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
        summary = service.import_yaml(path, dry_run=True)
        assert summary["diffs"] == []
        assert summary["changes"] == ["server.port", "vendors[zhipu].api_key"]
        assert PLANTED not in json.dumps(summary, ensure_ascii=False)

    def test_export_refuses_literal_template_strings(self, service):
        """防御：存储中残留的含 ${ 字面量不得被导出为会展开的模板."""
        with ConfigStore(config_db_path()).transaction() as txn:
            txn.put_section("failover", {"error_message_patterns": ["${HOME}"]})
        with pytest.raises(ConfigValidationError) as exc_info:
            service.export_yaml()
        assert _locs(exc_info) == [["failover", "error_message_patterns", 0]]

    def test_import_redacted_without_record_fails(self, service):
        path = Path.cwd().parent / "in.yaml"
        path.write_text('vendors:\n  - vendor: zhipu\n    api_key: "<redacted>"\n')
        with pytest.raises(ImportPlanError, match="vendors.zhipu.api_key"):
            service.import_yaml(path, dry_run=True)


class TestTargetAddressDefaultExemption:
    """清空规则收紧：新目标 == 包内默认端点时保留密钥（reset / 改回默认）."""

    def test_reset_vendors_keeps_stored_secret(self, service):
        """自定义 URL + 已存密钥 → reset vendors：密钥保留（文档承诺「Reset 不丢密钥」）."""
        # 同一请求内改目标 + 显式 set：密钥保留（显式 op 优先于清空规则）
        _http(
            service,
            {
                "revisions": {"vendors": 0},
                "sections": {
                    "vendors": _with_vendor(
                        service, "zhipu", base_url="https://custom.example"
                    )
                },
                "secret_ops": {
                    "vendors.zhipu.api_key": {"op": "set", "value": PLANTED}
                },
            },
        )
        assert service.view()["secrets"]["vendors.zhipu.api_key"]["kind"] == "stored"
        result = service.reset_section("vendors")
        assert result.cleared_secrets == []
        state = service.view()["secrets"]["vendors.zhipu.api_key"]
        assert state["kind"] == "stored" and state["recorded"] is True

    def test_edit_back_to_default_target_keeps_secret(self, service):
        """改到非默认目标清空；显式重设后改回包内默认目标保留."""
        _http(
            service,
            {"secret_ops": {"vendors.zhipu.api_key": {"op": "set", "value": PLANTED}}},
        )
        vendors = _with_vendor(service, "zhipu", base_url="https://evil.example")
        r1 = _http(
            service, {"revisions": {"vendors": 0}, "sections": {"vendors": vendors}}
        )
        assert r1.cleared_secrets == ["vendors.zhipu.api_key"]
        _http(
            service,
            {
                "revisions": {"vendors": service.revision("vendors")},
                "sections": {"vendors": _vendors(service)},
                "secret_ops": {
                    "vendors.zhipu.api_key": {"op": "set", "value": PLANTED}
                },
            },
        )
        vendors = _with_vendor(
            service, "zhipu", base_url=default_vendor_dict("zhipu")["base_url"]
        )
        r2 = _http(
            service,
            {
                "revisions": {"vendors": service.revision("vendors")},
                "sections": {"vendors": vendors},
            },
        )
        assert r2.cleared_secrets == []
        assert service.view()["secrets"]["vendors.zhipu.api_key"]["kind"] == "stored"


class TestSessionPoliciesListNormalization:
    """list 形态 session_policies 归一为 dict 落库（gate F-1：避免 Config 页死区）."""

    def test_put_list_form_is_wrapped(self, service):
        result = _http(
            service,
            {
                "revisions": {"session_policies": 0},
                "sections": {"session_policies": []},
            },
        )
        assert result.changed_sections == []
        snap = ConfigStore(config_db_path()).snapshot()
        assert (
            "session_policies" not in snap.sections
        )  # 与默认（空 policies）等价 → 无覆盖

    def test_put_list_with_policy_wraps_to_dict(self, service):
        result = _http(
            service,
            {
                "revisions": {"session_policies": 0},
                "sections": {
                    "session_policies": [
                        {
                            "name": "vip",
                            "match": {"session_keys": ["s1"]},
                            "tiers": ["zhipu"],
                        }
                    ]
                },
            },
        )
        assert "session_policies" in result.changed_sections
        snap = ConfigStore(config_db_path()).snapshot()
        assert snap.sections["session_policies"] == {
            "policies": [
                {"name": "vip", "match": {"session_keys": ["s1"]}, "tiers": ["zhipu"]}
            ]
        }
        view = service.view()["values"]["session_policies"]
        assert isinstance(view, dict) and len(view["policies"]) == 1


# ── pricing 行不可删除（下线开关替代删除） ──────────────────────


def _pricing_rows(service: ConfigService) -> list[dict]:
    return service.view()["values"]["pricing"]


def _set_pricing(service: ConfigService, rows: list[dict]):
    return _http(
        service,
        {
            "revisions": {"pricing": service.revision("pricing")},
            "sections": {"pricing": rows},
        },
    )


class TestPricingNoDelete:
    """pricing 行不可删除：删除 / 改名 / 恢复默认丢行一律 422，online 开关放行."""

    def _rows_with_custom(self, service: ConfigService) -> list[dict]:
        rows = _pricing_rows(service)
        rows.append(
            {"vendor": "zhipu", "model": "glm-5.5", "input_cost_per_mtok": "$1.0"}
        )
        return rows

    def test_add_row_allowed(self, service):
        result = _set_pricing(service, self._rows_with_custom(service))
        assert "pricing" in result.changed_sections
        models = {r["model"] for r in _pricing_rows(service)}
        assert "glm-5.5" in models

    def test_delete_custom_row_rejected(self, service):
        _set_pricing(service, self._rows_with_custom(service))
        rows = [r for r in _pricing_rows(service) if r.get("model") != "glm-5.5"]
        with pytest.raises(ConfigValidationError) as ei:
            _set_pricing(service, rows)
        err = _errors(ei)[0]
        assert err["loc"] == ["pricing"]
        assert "不可删除" in err["msg"]

    def test_delete_default_row_rejected(self, service):
        rows = _pricing_rows(service)
        assert len(rows) >= 2  # 包内默认定价表非空
        with pytest.raises(ConfigValidationError) as ei:
            _set_pricing(service, rows[1:])
        assert "不可删除" in _errors(ei)[0]["msg"]

    def test_rename_model_rejected(self, service):
        rows = _pricing_rows(service)
        rows[0] = dict(rows[0], model="renamed-model")
        with pytest.raises(ConfigValidationError) as ei:
            _set_pricing(service, rows)
        assert "不可删除" in _errors(ei)[0]["msg"]

    def test_online_toggle_roundtrip(self, service):
        rows = _pricing_rows(service)
        rows[0] = dict(rows[0], online=False)
        _set_pricing(service, rows)
        assert _pricing_rows(service)[0]["online"] is False
        rows = _pricing_rows(service)
        rows[0] = {k: v for k, v in rows[0].items() if k != "online"}
        _set_pricing(service, rows)
        assert "online" not in _pricing_rows(service)[0]

    def test_reset_section_with_custom_row_rejected(self, service):
        _set_pricing(service, self._rows_with_custom(service))
        with pytest.raises(ConfigValidationError) as ei:
            service.reset_section("pricing")
        assert "不可删除" in _errors(ei)[0]["msg"]

    def test_error_does_not_leak_values(self, service):
        rows = _pricing_rows(service)
        dropped = rows[0]
        with pytest.raises(ConfigValidationError) as ei:
            _set_pricing(service, rows[1:])
        msg = _errors(ei)[0]["msg"]
        assert str(dropped.get("model")) not in msg
        assert str(dropped.get("vendor")) not in msg
