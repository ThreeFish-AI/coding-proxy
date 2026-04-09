"""HTTP 错误分类与请求能力画像提取单元测试."""

import httpx

from coding.proxy.routing.error_classifier import (
    build_request_capabilities,
    extract_error_payload_from_http_status,
    is_semantic_rejection,
    is_structural_validation_error,
)

# --- is_structural_validation_error 测试 ---


class TestIsStructuralValidationError:
    """结构性验证错误判定测试."""

    def test_tool_result_role_error(self):
        assert (
            is_structural_validation_error(
                status_code=400,
                error_message="messages.105: tool_result blocks can only be in user messages",
            )
            is True
        )

    def test_tool_use_role_error(self):
        assert (
            is_structural_validation_error(
                status_code=400,
                error_message="tool_use blocks can only be in assistant messages",
            )
            is True
        )

    def test_message_alternation_error(self):
        assert (
            is_structural_validation_error(
                status_code=400,
                error_message="messages must alternate between user and assistant",
            )
            is True
        )

    def test_thinking_block_error(self):
        assert (
            is_structural_validation_error(
                status_code=400,
                error_message="thinking blocks can only be in assistant messages",
            )
            is True
        )

    def test_non_400_not_structural(self):
        assert (
            is_structural_validation_error(
                status_code=429,
                error_message="tool_result blocks can only be in user messages",
            )
            is False
        )

    def test_generic_400_not_structural(self):
        assert (
            is_structural_validation_error(
                status_code=400,
                error_message="something went wrong",
            )
            is False
        )

    def test_none_message_safe(self):
        assert (
            is_structural_validation_error(status_code=400, error_message=None) is False
        )


# --- is_semantic_rejection 测试 ---


class TestIsSemanticRejection:
    """语义拒绝判定测试 — 400 状态码 + 特征错误类型/消息."""

    def test_400_with_invalid_request_error_type(self):
        assert (
            is_semantic_rejection(status_code=400, error_type="invalid_request_error")
            is True
        )

    def test_400_with_validation_message(self):
        assert (
            is_semantic_rejection(
                status_code=400,
                error_message="should match pattern",
            )
            is True
        )

    def test_400_with_tool_use_id_message(self):
        assert (
            is_semantic_rejection(
                status_code=400,
                error_message="tool_use_id is invalid",
            )
            is True
        )

    def test_non_400_status_rejected(self):
        assert is_semantic_rejection(status_code=429) is False
        assert is_semantic_rejection(status_code=500) is False

    def test_400_generic_message_not_rejected(self):
        assert (
            is_semantic_rejection(
                status_code=400,
                error_message="something went wrong",
            )
            is False
        )

    def test_none_values_safe(self):
        assert (
            is_semantic_rejection(status_code=400, error_type=None, error_message=None)
            is False
        )

    def test_case_insensitive_type(self):
        assert (
            is_semantic_rejection(
                status_code=400,
                error_type="Invalid_Request_Error",
            )
            is True
        )

    def test_case_insensitive_message(self):
        assert (
            is_semantic_rejection(
                status_code=400,
                error_message="VALIDATION failed for field x",
            )
            is True
        )

    def test_structural_tool_result_error_not_semantic(self):
        """tool_result 角色错位是结构性错误，不应视为语义拒绝."""
        assert (
            is_semantic_rejection(
                status_code=400,
                error_type="invalid_request_error",
                error_message="messages.105: tool_result blocks can only be in user messages",
            )
            is False
        )

    def test_structural_alternation_error_not_semantic(self):
        """消息交替违规是结构性错误，不应视为语义拒绝."""
        assert (
            is_semantic_rejection(
                status_code=400,
                error_type="invalid_request_error",
                error_message="messages must alternate between user and assistant",
            )
            is False
        )

    def test_structural_tool_use_error_not_semantic(self):
        """tool_use 角色错位是结构性错误，不应视为语义拒绝."""
        assert (
            is_semantic_rejection(
                status_code=400,
                error_type="invalid_request_error",
                error_message="tool_use blocks can only be in assistant messages",
            )
            is False
        )


# --- extract_error_payload_from_http_status 测试 ---


class TestExtractErrorPayload:
    """从 HTTPStatusError 提取错误载荷."""


def test_extract_valid_json_payload():
    resp = httpx.Response(400, content=b'{"error":{"type":"bad","message":"oops"}}')
    exc = httpx.HTTPStatusError(
        "error", request=httpx.Request("POST", "http://x"), response=resp
    )
    result = extract_error_payload_from_http_status(exc)
    assert result is not None
    assert result["error"]["type"] == "bad"


def test_extract_empty_response():
    resp = httpx.Response(400, content=b"")
    exc = httpx.HTTPStatusError(
        "error", request=httpx.Request("POST", "http://x"), response=resp
    )
    assert extract_error_payload_from_http_status(exc) is None


def test_extract_invalid_json():
    resp = httpx.Response(400, content=b"not json")
    exc = httpx.HTTPStatusError(
        "error", request=httpx.Request("POST", "http://x"), response=resp
    )
    assert extract_error_payload_from_http_status(exc) is None


def test_extract_none_response():
    exc = httpx.HTTPStatusError(
        "error", request=httpx.Request("POST", "http://x"), response=None
    )
    assert extract_error_payload_from_http_status(exc) is None


def test_extract_non_dict_payload():
    resp = httpx.Response(400, content=b'"just a string"')
    exc = httpx.HTTPStatusError(
        "error", request=httpx.Request("POST", "http://x"), response=resp
    )
    assert extract_error_payload_from_http_status(exc) is None


# --- build_request_capabilities 测试 ---


class TestBuildRequestCapabilities:
    """从请求体提取能力画像."""


def test_basic_request():
    caps = build_request_capabilities(
        {"model": "claude-sonnet-4-20250514", "messages": []}
    )
    assert caps.has_tools is False
    assert caps.has_thinking is False
    assert caps.has_images is False
    assert caps.has_metadata is False


def test_tools_detected():
    caps = build_request_capabilities(
        {
            "model": "claude-sonnet-4-20250514",
            "messages": [],
            "tools": [{"name": "t1"}],
        }
    )
    assert caps.has_tools is True


def test_tool_choice_detected():
    caps = build_request_capabilities(
        {
            "model": "claude-sonnet-4-20250514",
            "messages": [],
            "tool_choice": "auto",
        }
    )
    assert caps.has_tools is True


def test_thinking_detected():
    caps = build_request_capabilities(
        {
            "model": "claude-sonnet-4-20250514",
            "messages": [],
            "thinking": {"type": "enabled"},
        }
    )
    assert caps.has_thinking is True


def test_extended_thinking_detected():
    caps = build_request_capabilities(
        {
            "model": "claude-sonnet-4-20250514",
            "messages": [],
            "extended_thinking": {"type": "enabled"},
        }
    )
    assert caps.has_thinking is True


def test_images_in_content():
    caps = build_request_capabilities(
        {
            "model": "claude-sonnet-4-20250514",
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "image", "source": {"type": "base64"}}],
                }
            ],
        }
    )
    assert caps.has_images is True


def test_metadata_detected():
    caps = build_request_capabilities(
        {
            "model": "claude-sonnet-4-20250514",
            "messages": [],
            "metadata": {"key": "val"},
        }
    )
    assert caps.has_metadata is True


def test_string_content_not_image():
    caps = build_request_capabilities(
        {
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "hello"}],
        }
    )
    assert caps.has_images is False


def test_empty_messages():
    caps = build_request_capabilities({"model": "m", "messages": []})
    assert caps.has_images is False
