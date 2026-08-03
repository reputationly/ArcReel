"""NewAPIVideoBackend 单元测试（mock httpx）。"""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from lib.providers import PROVIDER_NEWAPI
from lib.video_backends.base import VideoGenerationRequest


def _make_response(status_code: int, json_body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.raise_for_status = MagicMock()
    return resp


def _make_http_error(status_code: int, message: str) -> httpx.HTTPStatusError:
    """构造 httpx.HTTPStatusError，用于模拟 raise_for_status() 抛出的 5xx。"""
    request = httpx.Request("POST", "https://x/v1/video/generations")
    response = httpx.Response(status_code, request=request, text=message)
    return httpx.HTTPStatusError(f"Server error '{status_code}'", request=request, response=response)


def _fake_download_factory(payload: bytes = b"mp4-bytes"):
    """返回一个模拟 `download_video` 的异步函数，写入 payload 到 output_path。"""

    async def _fake(url: str, output_path: Path, *, timeout: int = 120) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(payload)

    return _fake


class TestNewAPIVideoBackend:
    def test_name_and_model(self):
        from lib.video_backends.newapi import NewAPIVideoBackend

        backend = NewAPIVideoBackend(api_key="sk-test", base_url="https://example.com/v1", model="kling-v1")
        assert backend.name == PROVIDER_NEWAPI
        assert backend.model == "kling-v1"

    def test_capabilities(self):
        from lib.video_backends.newapi import NewAPIVideoBackend

        backend = NewAPIVideoBackend(api_key="sk-test", base_url="https://x/v1", model="m")
        assert backend.video_capabilities.max_reference_images == 0

    async def test_text_to_video_happy_path(self, tmp_path: Path):
        create_resp = _make_response(200, {"task_id": "task-42", "status": "queued"})
        poll_resp = _make_response(
            200,
            {
                "task_id": "task-42",
                "status": "completed",
                "url": "https://cdn.example.com/out.mp4",
                "format": "mp4",
                "metadata": {"duration": 5, "fps": 24, "width": 720, "height": 1280, "seed": 0},
            },
        )

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=create_resp)
        mock_client.get = AsyncMock(return_value=poll_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        fake_download = AsyncMock(side_effect=_fake_download_factory(b"mp4-bytes"))

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("lib.video_backends.newapi._POLL_INTERVAL_SECONDS", 0.0),
            patch("lib.video_backends.newapi.download_video", fake_download),
        ):
            from lib.video_backends.newapi import NewAPIVideoBackend

            backend = NewAPIVideoBackend(api_key="sk-test", base_url="https://example.com/v1", model="kling-v1")
            request = VideoGenerationRequest(
                prompt="A cat running",
                output_path=tmp_path / "out.mp4",
                aspect_ratio="9:16",
                resolution="720p",
                duration_seconds=5,
            )
            result = await backend.generate(request)

        assert result.video_path == tmp_path / "out.mp4"
        assert result.video_path.read_bytes() == b"mp4-bytes"
        assert result.provider == PROVIDER_NEWAPI
        assert result.model == "kling-v1"
        assert result.duration_seconds == 5
        assert result.task_id == "task-42"

        post_call = mock_client.post.call_args
        assert post_call.args[0].endswith("/video/generations")
        assert post_call.kwargs["json"]["model"] == "kling-v1"
        assert post_call.kwargs["json"]["prompt"] == "A cat running"
        assert post_call.kwargs["json"]["width"] == 720
        assert post_call.kwargs["json"]["height"] == 1280
        assert post_call.kwargs["json"]["duration"] == 5
        assert post_call.kwargs["json"]["n"] == 1
        assert "image" not in post_call.kwargs["json"]
        assert post_call.kwargs["headers"]["Authorization"] == "Bearer sk-test"

        # 下载走 base.download_video，URL 正确且不带 auth（base.download_video 不接 headers 参数）
        fake_download.assert_called_once()
        download_call = fake_download.call_args
        assert download_call.args[0] == "https://cdn.example.com/out.mp4"
        assert download_call.args[1] == tmp_path / "out.mp4"

    async def test_image_to_video_encodes_base64(self, tmp_path: Path):
        img_bytes = b"\x89PNG\r\nfake"
        img_path = tmp_path / "start.png"
        img_path.write_bytes(img_bytes)

        create_resp = _make_response(200, {"task_id": "t1", "status": "queued"})
        poll_resp = _make_response(
            200,
            {
                "task_id": "t1",
                "status": "completed",
                "url": "https://cdn/x.mp4",
                "metadata": {"duration": 5},
            },
        )

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=create_resp)
        mock_client.get = AsyncMock(return_value=poll_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        fake_download = AsyncMock(side_effect=_fake_download_factory(b"v"))

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("lib.video_backends.newapi._POLL_INTERVAL_SECONDS", 0.0),
            patch("lib.video_backends.newapi.download_video", fake_download),
        ):
            from lib.video_backends.newapi import NewAPIVideoBackend

            backend = NewAPIVideoBackend(api_key="k", base_url="https://x/v1", model="kling-v1")
            await backend.generate(
                VideoGenerationRequest(
                    prompt="p",
                    output_path=tmp_path / "o.mp4",
                    start_image=img_path,
                    resolution="720p",
                    aspect_ratio="9:16",
                    duration_seconds=5,
                )
            )

        sent_image = mock_client.post.call_args.kwargs["json"]["image"]
        expected = "data:image/png;base64," + base64.b64encode(img_bytes).decode()
        assert sent_image == expected

    async def test_start_image_missing_is_ignored(self, tmp_path: Path, caplog):
        """start_image 文件不存在时应 warning 并走纯文生路径。"""
        create_resp = _make_response(200, {"task_id": "t-missing", "status": "queued"})
        poll_resp = _make_response(
            200,
            {"task_id": "t-missing", "status": "completed", "url": "https://cdn/v.mp4", "metadata": {"duration": 5}},
        )

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=create_resp)
        mock_client.get = AsyncMock(return_value=poll_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        fake_download = AsyncMock(side_effect=_fake_download_factory(b"v"))

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("lib.video_backends.newapi._POLL_INTERVAL_SECONDS", 0.0),
            patch("lib.video_backends.newapi.download_video", fake_download),
            caplog.at_level("WARNING", logger="lib.video_backends.newapi"),
        ):
            from lib.video_backends.newapi import NewAPIVideoBackend

            backend = NewAPIVideoBackend(api_key="k", base_url="https://x/v1", model="m")
            await backend.generate(
                VideoGenerationRequest(
                    prompt="p",
                    output_path=tmp_path / "o.mp4",
                    start_image=tmp_path / "does_not_exist.png",
                    resolution="720p",
                    aspect_ratio="9:16",
                    duration_seconds=5,
                )
            )

        assert "image" not in mock_client.post.call_args.kwargs["json"]
        assert any("start_image 文件不存在" in rec.message for rec in caplog.records)

    async def test_failed_status_raises(self, tmp_path: Path):
        create_resp = _make_response(200, {"task_id": "t2", "status": "queued"})
        poll_resp = _make_response(
            200,
            {
                "task_id": "t2",
                "status": "failed",
                "error": {"code": 500, "message": "upstream down"},
            },
        )
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=create_resp)
        mock_client.get = AsyncMock(return_value=poll_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        fake_download = AsyncMock()

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("lib.video_backends.newapi._POLL_INTERVAL_SECONDS", 0.0),
            patch("lib.video_backends.newapi.download_video", fake_download),
        ):
            from lib.video_backends.newapi import NewAPIVideoBackend

            backend = NewAPIVideoBackend(api_key="k", base_url="https://x/v1", model="m")
            with pytest.raises(RuntimeError, match="upstream down"):
                await backend.generate(
                    VideoGenerationRequest(
                        prompt="p",
                        output_path=tmp_path / "o.mp4",
                        resolution="720p",
                        aspect_ratio="9:16",
                        duration_seconds=5,
                    )
                )

        fake_download.assert_not_called()

    async def test_polls_through_in_progress(self, tmp_path: Path):
        create_resp = _make_response(200, {"task_id": "t3", "status": "queued"})
        in_progress = _make_response(200, {"task_id": "t3", "status": "in_progress"})
        completed = _make_response(
            200,
            {
                "task_id": "t3",
                "status": "completed",
                "url": "https://cdn/v.mp4",
                "metadata": {"duration": 5},
            },
        )

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=create_resp)
        mock_client.get = AsyncMock(side_effect=[in_progress, in_progress, completed])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        fake_download = AsyncMock(side_effect=_fake_download_factory(b"v"))

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("lib.video_backends.newapi._POLL_INTERVAL_SECONDS", 0.0),
            patch("lib.video_backends.newapi.download_video", fake_download),
        ):
            from lib.video_backends.newapi import NewAPIVideoBackend

            backend = NewAPIVideoBackend(api_key="k", base_url="https://x/v1", model="m")
            result = await backend.generate(
                VideoGenerationRequest(
                    prompt="p",
                    output_path=tmp_path / "o.mp4",
                    resolution="720p",
                    aspect_ratio="9:16",
                    duration_seconds=5,
                )
            )

        assert result.task_id == "t3"
        # 3 次 poll（in_progress → in_progress → completed），下载不经过 mock_client
        assert mock_client.get.call_count == 3
        fake_download.assert_called_once()

    async def test_polling_timeout_raises(self, tmp_path: Path):
        """轮询超时应抛 TimeoutError 且不触发下载。"""
        create_resp = _make_response(200, {"task_id": "t-timeout", "status": "queued"})
        in_progress = _make_response(200, {"task_id": "t-timeout", "status": "in_progress"})

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=create_resp)
        mock_client.get = AsyncMock(return_value=in_progress)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        fake_download = AsyncMock()

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("lib.video_backends.newapi._POLL_INTERVAL_SECONDS", 0.0),
            patch("lib.video_backends.newapi._MIN_POLL_TIMEOUT_SECONDS", 0.01),
            patch("lib.video_backends.newapi._POLL_TIMEOUT_PER_SECOND", 0),
            patch("lib.video_backends.newapi.download_video", fake_download),
        ):
            from lib.video_backends.newapi import NewAPIVideoBackend

            backend = NewAPIVideoBackend(api_key="k", base_url="https://x/v1", model="m")
            with pytest.raises(TimeoutError, match="NewAPI"):
                await backend.generate(
                    VideoGenerationRequest(
                        prompt="p",
                        output_path=tmp_path / "o.mp4",
                        resolution="720p",
                        aspect_ratio="9:16",
                        duration_seconds=5,
                    )
                )

        fake_download.assert_not_called()

    async def test_zero_duration_from_api_is_preserved(self, tmp_path: Path):
        """回归: API 返回 duration=0 时不应被 falsy 回退到请求值（is None 判空）。"""
        create_resp = _make_response(200, {"task_id": "t-zero", "status": "queued"})
        poll_resp = _make_response(
            200,
            {
                "task_id": "t-zero",
                "status": "completed",
                "url": "https://cdn/v.mp4",
                "metadata": {"duration": 0},
            },
        )

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=create_resp)
        mock_client.get = AsyncMock(return_value=poll_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        fake_download = AsyncMock(side_effect=_fake_download_factory(b"v"))

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("lib.video_backends.newapi._POLL_INTERVAL_SECONDS", 0.0),
            patch("lib.video_backends.newapi.download_video", fake_download),
        ):
            from lib.video_backends.newapi import NewAPIVideoBackend

            backend = NewAPIVideoBackend(api_key="k", base_url="https://x/v1", model="m")
            result = await backend.generate(
                VideoGenerationRequest(
                    prompt="p",
                    output_path=tmp_path / "o.mp4",
                    resolution="720p",
                    aspect_ratio="9:16",
                    duration_seconds=5,
                )
            )

        # API 明确返回 0，应如实保留，不是回退到 request.duration_seconds=5
        assert result.duration_seconds == 0

    async def test_create_retries_on_5xx(self, tmp_path: Path):
        """5xx HTTPStatusError 应通过 should_retry_submit 的 status_code 闸门重试。"""
        failing_resp = MagicMock()
        failing_resp.status_code = 503
        failing_resp.raise_for_status = MagicMock(side_effect=_make_http_error(503, "upstream busy"))

        create_resp = _make_response(200, {"task_id": "t-retry", "status": "queued"})
        poll_resp = _make_response(
            200,
            {"task_id": "t-retry", "status": "completed", "url": "https://cdn/v.mp4", "metadata": {"duration": 5}},
        )

        mock_client = AsyncMock()
        # 前两次创建任务 503，第三次成功
        mock_client.post = AsyncMock(side_effect=[failing_resp, failing_resp, create_resp])
        mock_client.get = AsyncMock(return_value=poll_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        fake_download = AsyncMock(side_effect=_fake_download_factory(b"v"))

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("lib.video_backends.newapi._POLL_INTERVAL_SECONDS", 0.0),
            # 压缩重试退避时间到 0，避免测试变慢
            patch("lib.video_backends.newapi.DEFAULT_BACKOFF_SECONDS", (0, 0, 0)),
            patch("lib.retry._compute_wait", lambda attempt, backoff: 0.0),
            patch("lib.video_backends.newapi.download_video", fake_download),
        ):
            from lib.video_backends.newapi import NewAPIVideoBackend

            backend = NewAPIVideoBackend(api_key="k", base_url="https://x/v1", model="m")
            result = await backend.generate(
                VideoGenerationRequest(
                    prompt="p",
                    output_path=tmp_path / "o.mp4",
                    resolution="720p",
                    aspect_ratio="9:16",
                    duration_seconds=5,
                )
            )

        assert result.task_id == "t-retry"
        assert mock_client.post.call_count == 3

    async def test_create_non_retryable_4xx_fails_fast(self, tmp_path: Path):
        """创建任务遇确定性 4xx（400）应一次失败，不重试。"""
        bad_resp = _make_response(400, {"error": "bad request"})
        bad_resp.raise_for_status = MagicMock(side_effect=_make_http_error(400, "bad request"))

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=bad_resp)
        mock_client.get = AsyncMock(side_effect=AssertionError("4xx 应在创建阶段失败，不该轮询"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("lib.retry._compute_wait", lambda attempt, backoff: 0.0),
        ):
            from lib.video_backends.newapi import NewAPIVideoBackend

            backend = NewAPIVideoBackend(api_key="k", base_url="https://x/v1", model="m")
            with pytest.raises(httpx.HTTPStatusError):
                await backend.generate(
                    VideoGenerationRequest(
                        prompt="p", output_path=tmp_path / "o.mp4", aspect_ratio="9:16", duration_seconds=5
                    )
                )

        assert mock_client.post.call_count == 1, "确定性 4xx 不该被 retry"

    async def test_create_read_timeout_fails_fast_with_manual_retry_hint(self, tmp_path: Path):
        """create 阶段 ReadTimeout（请求可能已送达）→ 不重试、单次失败、错误信息含手动重试提示。"""
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ReadTimeout("read timed out"))
        mock_client.get = AsyncMock(side_effect=AssertionError("歧义态应在创建阶段失败，不该轮询"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("lib.retry._compute_wait", lambda attempt, backoff: 0.0),
        ):
            from lib.video_backends.base import AmbiguousSubmitError
            from lib.video_backends.newapi import NewAPIVideoBackend

            backend = NewAPIVideoBackend(api_key="k", base_url="https://x/v1", model="m")
            with pytest.raises(AmbiguousSubmitError, match="手动重试"):
                await backend.generate(
                    VideoGenerationRequest(
                        prompt="p", output_path=tmp_path / "o.mp4", aspect_ratio="9:16", duration_seconds=5
                    )
                )

        assert mock_client.post.call_count == 1, "歧义态不该被 retry"

    async def test_create_connect_error_retries(self, tmp_path: Path):
        """create 阶段 ConnectError（请求确定未送达）→ 重试，第三次成功。"""
        create_resp = _make_response(200, {"task_id": "t-conn", "status": "queued"})
        poll_resp = _make_response(
            200,
            {"task_id": "t-conn", "status": "completed", "url": "https://cdn/v.mp4", "metadata": {"duration": 5}},
        )
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=[httpx.ConnectError("refused"), httpx.ConnectError("refused"), create_resp]
        )
        mock_client.get = AsyncMock(return_value=poll_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        fake_download = AsyncMock(side_effect=_fake_download_factory(b"v"))

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("lib.video_backends.newapi._POLL_INTERVAL_SECONDS", 0.0),
            patch("lib.retry._compute_wait", lambda attempt, backoff: 0.0),
            patch("lib.video_backends.newapi.download_video", fake_download),
        ):
            from lib.video_backends.newapi import NewAPIVideoBackend

            backend = NewAPIVideoBackend(api_key="k", base_url="https://x/v1", model="m")
            result = await backend.generate(
                VideoGenerationRequest(
                    prompt="p", output_path=tmp_path / "o.mp4", aspect_ratio="9:16", duration_seconds=5
                )
            )

        assert result.task_id == "t-conn"
        assert mock_client.post.call_count == 3, "ConnectError 请求确定未送达，应重试"

    async def test_poll_read_timeout_retries(self, tmp_path: Path):
        """poll 阶段 ReadTimeout（幂等 GET）→ 重试，不回归。"""
        create_resp = _make_response(200, {"task_id": "t-pr", "status": "queued"})
        poll_resp = _make_response(
            200,
            {"task_id": "t-pr", "status": "completed", "url": "https://cdn/v.mp4", "metadata": {"duration": 5}},
        )
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=create_resp)
        mock_client.get = AsyncMock(side_effect=[httpx.ReadTimeout("read timed out"), poll_resp])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        fake_download = AsyncMock(side_effect=_fake_download_factory(b"v"))

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("lib.video_backends.newapi._POLL_INTERVAL_SECONDS", 0.0),
            patch("lib.video_backends.newapi.download_video", fake_download),
        ):
            from lib.video_backends.newapi import NewAPIVideoBackend

            backend = NewAPIVideoBackend(api_key="k", base_url="https://x/v1", model="m")
            result = await backend.generate(
                VideoGenerationRequest(
                    prompt="p", output_path=tmp_path / "o.mp4", aspect_ratio="9:16", duration_seconds=5
                )
            )

        assert result.task_id == "t-pr"
        assert mock_client.get.call_count == 2, "poll 网络超时应重试"

    async def test_poll_non_retryable_4xx_fails_fast(self, tmp_path: Path):
        """轮询遇确定性 4xx（401，如 token 失效）应一次失败，不重试到 max_wait 超时。"""
        create_resp = _make_response(200, {"task_id": "t-401", "status": "queued"})
        unauthorized_resp = _make_response(401, {"error": "unauthorized"})
        unauthorized_resp.raise_for_status = MagicMock(side_effect=_make_http_error(401, "unauthorized"))

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=create_resp)
        mock_client.get = AsyncMock(return_value=unauthorized_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("lib.video_backends.newapi._POLL_INTERVAL_SECONDS", 0.0),
        ):
            from lib.video_backends.newapi import NewAPIVideoBackend

            backend = NewAPIVideoBackend(api_key="k", base_url="https://x/v1", model="m")
            with pytest.raises(httpx.HTTPStatusError):
                await backend.generate(
                    VideoGenerationRequest(
                        prompt="p", output_path=tmp_path / "o.mp4", aspect_ratio="9:16", duration_seconds=5
                    )
                )

        assert mock_client.get.call_count == 1, "轮询确定性 4xx 应一击失败，不重试到超时"

    async def test_resume_video_polls_existing_job(self, tmp_path: Path):
        """resume_video 仅 poll + 下载,不 POST create (ADR 0007)。"""
        poll_resp = _make_response(
            200,
            {
                "task_id": "task-resume",
                "status": "completed",
                "url": "https://cdn/resumed.mp4",
                "metadata": {"duration": 5},
            },
        )
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=AssertionError("resume 不应 POST create"))
        mock_client.get = AsyncMock(return_value=poll_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        fake_download = AsyncMock(side_effect=_fake_download_factory(b"resumed"))

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("lib.video_backends.newapi._POLL_INTERVAL_SECONDS", 0.0),
            patch("lib.video_backends.newapi.download_video", fake_download),
        ):
            from lib.video_backends.newapi import NewAPIVideoBackend

            backend = NewAPIVideoBackend(api_key="k", base_url="https://x/v1", model="m")
            result = await backend.resume_video(
                "task-resume",
                VideoGenerationRequest(
                    prompt="p", output_path=tmp_path / "out.mp4", aspect_ratio="9:16", duration_seconds=5
                ),
            )

        mock_client.post.assert_not_called()
        # 应该 GET 到 .../video/generations/task-resume
        assert mock_client.get.call_args.args[0].endswith("/task-resume")
        assert result.task_id == "task-resume"
        assert (tmp_path / "out.mp4").read_bytes() == b"resumed"

    async def test_poll_recognizes_expired_status(self, tmp_path: Path):
        """fix #647 #5：poll 返回 status='expired' → 抛 ResumeExpiredError。"""
        from lib.video_backends.base import ResumeExpiredError

        expired_resp = _make_response(200, {"task_id": "task-x", "status": "expired"})
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=expired_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("lib.video_backends.newapi._POLL_INTERVAL_SECONDS", 0.0),
        ):
            from lib.video_backends.newapi import NewAPIVideoBackend

            backend = NewAPIVideoBackend(api_key="k", base_url="https://x/v1", model="m")
            with pytest.raises(ResumeExpiredError) as ei:
                await backend.resume_video(
                    "task-x",
                    VideoGenerationRequest(
                        prompt="p", output_path=tmp_path / "out.mp4", aspect_ratio="9:16", duration_seconds=5
                    ),
                )
            assert ei.value.job_id == "task-x"
            assert ei.value.provider == PROVIDER_NEWAPI

    async def test_resume_404_raises_resume_expired_without_retry(self, tmp_path: Path):
        """resume 路径下 GET 返 404 应立即转 ResumeExpiredError，不被 retryable 框架重试到超时。"""
        from lib.video_backends.base import ResumeExpiredError

        # 构造 404 response 让 raise_for_status 真抛 HTTPStatusError（_make_response 默认 mock 空，需手工设）
        not_found_resp = _make_response(404, {"error": "task not found"})
        not_found_resp.raise_for_status = MagicMock(side_effect=_make_http_error(404, "task not found"))
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=not_found_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("lib.video_backends.newapi._POLL_INTERVAL_SECONDS", 0.0),
        ):
            from lib.video_backends.newapi import NewAPIVideoBackend

            backend = NewAPIVideoBackend(api_key="k", base_url="https://x/v1", model="m")
            with pytest.raises(ResumeExpiredError) as ei:
                await backend.resume_video(
                    "task-404",
                    VideoGenerationRequest(
                        prompt="p", output_path=tmp_path / "out.mp4", aspect_ratio="9:16", duration_seconds=5
                    ),
                )
            assert ei.value.job_id == "task-404"
            assert ei.value.provider == PROVIDER_NEWAPI
            # 不应被 retry 框架重试多次（应仅 1 次 GET 调用立即抛错）
            assert mock_client.get.call_count == 1, "404 应一击转 ResumeExpiredError，不该被 retry"

    async def test_generate_expired_status_raises_runtime_error_not_resume_expired(self, tmp_path: Path):
        """generate 路径下 status='expired' 抛 RuntimeError，不带 [resume_expired] 语义。"""
        from lib.video_backends.base import ResumeExpiredError

        create_resp = _make_response(200, {"task_id": "task-new"})
        expired_resp = _make_response(200, {"task_id": "task-new", "status": "expired"})
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=create_resp)
        mock_client.get = AsyncMock(return_value=expired_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("lib.video_backends.newapi._POLL_INTERVAL_SECONDS", 0.0),
        ):
            from lib.video_backends.newapi import NewAPIVideoBackend

            backend = NewAPIVideoBackend(api_key="k", base_url="https://x/v1", model="m")
            with pytest.raises(RuntimeError) as ei:
                await backend.generate(
                    VideoGenerationRequest(
                        prompt="p",
                        output_path=tmp_path / "out.mp4",
                        aspect_ratio="9:16",
                        duration_seconds=5,
                    ),
                )
            assert "expired" in str(ei.value).lower()
            assert not isinstance(ei.value, ResumeExpiredError), "generate 路径不应抛 ResumeExpiredError"


class TestNewAPITaskEnvelope:
    """上游 new-api 现行实现把任务包在 {"code","data"} 信封里，状态还是大写内部态。

    只认扁平形态的话，轮询永远等不到终态，会一路跑到 max_wait 超时。
    """

    def test_normalize_flat_shape(self):
        from lib.video_backends.newapi import normalize_newapi_task_state

        state = normalize_newapi_task_state(
            {"task_id": "t", "status": "completed", "url": "https://cdn/a.mp4", "metadata": {"duration": 5}},
        )
        assert state["status"] == "completed"
        assert state["url"] == "https://cdn/a.mp4"
        assert state["metadata"] == {"duration": 5}

    def test_normalize_task_envelope(self):
        from lib.video_backends.newapi import normalize_newapi_task_state

        state = normalize_newapi_task_state(
            {
                "code": "success",
                "data": {"task_id": "t", "status": "SUCCESS", "result_url": "https://obs/a.mp4", "fail_reason": ""},
            },
        )
        assert state["status"] == "completed"
        assert state["url"] == "https://obs/a.mp4"
        assert state["error"] is None

    def test_normalize_envelope_failure_reason(self):
        from lib.video_backends.newapi import normalize_newapi_task_state

        state = normalize_newapi_task_state(
            {"code": "success", "data": {"status": "FAILURE", "fail_reason": "上游拒绝"}},
        )
        assert state["status"] == "failed"
        assert state["error"] == {"message": "上游拒绝"}

    def test_normalize_openai_metadata_url(self):
        from lib.video_backends.newapi import normalize_newapi_task_state

        state = normalize_newapi_task_state(
            {"id": "t", "status": "completed", "metadata": {"url": "https://obs/b.mp4"}}
        )
        assert state["url"] == "https://obs/b.mp4"

    def test_unknown_status_is_not_terminal(self):
        from lib.video_backends.newapi import normalize_newapi_task_state

        # IN_PROGRESS / QUEUED / 没见过的串都要继续轮询，不能当成终态。
        for raw in ("IN_PROGRESS", "QUEUED", "NOT_START", "weird"):
            state = normalize_newapi_task_state({"status": raw})
            assert state["status"] not in ("completed", "failed", "expired")

    async def test_generate_completes_through_envelope(self, tmp_path: Path):
        create_resp = _make_response(200, {"task_id": "task-env", "status": "queued"})
        running = _make_response(200, {"code": "success", "data": {"task_id": "task-env", "status": "IN_PROGRESS"}})
        done = _make_response(
            200,
            {
                "code": "success",
                "data": {"task_id": "task-env", "status": "SUCCESS", "result_url": "https://obs/out.mp4"},
            },
        )

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=create_resp)
        mock_client.get = AsyncMock(side_effect=[running, done])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        fake_download = AsyncMock(side_effect=_fake_download_factory(b"mp4"))

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("lib.video_backends.newapi._POLL_INTERVAL_SECONDS", 0.0),
            patch("lib.video_backends.newapi.download_video", fake_download),
        ):
            from lib.video_backends.newapi import NewAPIVideoBackend

            backend = NewAPIVideoBackend(api_key="k", base_url="https://x/v1", model="wan2.2-i2v")
            result = await backend.generate(
                VideoGenerationRequest(
                    prompt="p",
                    output_path=tmp_path / "out.mp4",
                    aspect_ratio="16:9",
                    resolution="720p",
                    duration_seconds=5,
                    seed=7,
                ),
            )

        assert result.task_id == "task-env"
        assert fake_download.call_args.args[0] == "https://obs/out.mp4"

        # 统一契约:new-api 的 TaskSubmitReq 只读 size 与 metadata，width/height/顶层 seed 会被丢掉。
        body = mock_client.post.call_args.kwargs["json"]
        assert body["size"] == "1280x720"
        # wan2.2-i2v 命中插帧白名单，故 metadata 除 seed 外还带 target_fps（见 TestFrameInterpolation）。
        assert body["metadata"] == {"seed": 7, "target_fps": 32}


class TestNewAPIImageModes:
    """上游三种图片模式互斥：单图首帧 / 首尾帧 / 多张参考图。"""

    def test_capabilities_by_model(self):
        from lib.video_backends.newapi import NewAPIVideoBackend

        seedance2 = NewAPIVideoBackend.video_capabilities_for_model("doubao-seedance-2-0-260128")
        assert seedance2.last_frame is True
        assert seedance2.max_reference_images == 9

        seedance15 = NewAPIVideoBackend.video_capabilities_for_model("doubao-seedance-1-5-pro")
        assert seedance15.last_frame is True
        assert seedance15.max_reference_images == 0

        # 非 Seedance 家族保持保守默认，不臆测网关背后的能力。
        other = NewAPIVideoBackend.video_capabilities_for_model("wan2.2-i2v")
        assert other.max_reference_images == 0

    def _backend(self, model: str):
        from lib.video_backends.newapi import NewAPIVideoBackend

        return NewAPIVideoBackend(api_key="k", base_url="https://x/v1", model=model)

    def _png(self, tmp_path: Path, name: str) -> Path:
        p = tmp_path / name
        p.write_bytes(b"\x89PNG\r\nfake")
        return p

    def test_first_and_last_frame(self, tmp_path: Path):
        backend = self._backend("doubao-seedance-1-5-pro")
        request = VideoGenerationRequest(
            prompt="p",
            output_path=tmp_path / "o.mp4",
            aspect_ratio="16:9",
            duration_seconds=5,
            start_image=self._png(tmp_path, "a.png"),
            end_image=self._png(tmp_path, "b.png"),
        )
        images, role = backend._collect_images(request)
        assert len(images) == 2
        assert role is None  # 按位置推断首帧/尾帧

    def test_reference_images_carry_explicit_role(self, tmp_path: Path):
        backend = self._backend("doubao-seedance-2-0-260128")
        request = VideoGenerationRequest(
            prompt="p",
            output_path=tmp_path / "o.mp4",
            aspect_ratio="16:9",
            duration_seconds=5,
            reference_images=[self._png(tmp_path, "r1.png"), self._png(tmp_path, "r2.png")],
        )
        images, role = backend._collect_images(request)
        assert len(images) == 2
        # 两张图不带 role 会被网关误判成首尾帧，必须显式钉住。
        assert role == "reference_image"

    def test_gpustack_capabilities_expand_by_task_type(self):
        """自建引擎的能力按门面 task_type 契约展开，而非一律保守判 0。"""
        from lib.video_backends.newapi import NewAPIVideoBackend

        # 首尾帧是独立部署的模型（wan2.2-flf2v），不是 i2v 的一种任务类型：门面按模型名推断
        # task_type，给 i2v 下发 flf2v 只会把两张图物化后发给一个不认这个任务的引擎。
        flf2v = NewAPIVideoBackend.video_capabilities_for_model("wan2.2-flf2v")
        assert flf2v.last_frame is True
        assert flf2v.max_reference_images == 0

        # i2v 只吃首帧，不声明尾帧能力——声明了会让界面开放一个用不了的入口
        i2v = NewAPIVideoBackend.video_capabilities_for_model("wan2.2-i2v")
        assert i2v.last_frame is False
        assert i2v.max_reference_images == 0

        # bernini 走 r2v，支持纯参考图；它没有首尾帧语义
        bernini = NewAPIVideoBackend.video_capabilities_for_model("bernini")
        assert bernini.max_reference_images > 0
        assert bernini.last_frame is False

        # 同名不同货的第三方模型不得被子串误伤（阿里云 wan2.2-i2v-plus 非自建）
        third_party = NewAPIVideoBackend.video_capabilities_for_model("wan2.2-i2v-plus")
        assert third_party.last_frame is False
        assert third_party.max_reference_images == 0

    def test_gpustack_flf2v_declares_task_type(self, tmp_path: Path):
        """首尾帧模型显式声明 flf2v：显式 task_type 优先于门面按模型名的推断，语义不靠猜。"""
        backend = self._backend("wan2.2-flf2v")
        payload: dict = {}
        metadata: dict = {}
        backend._apply_gpustack_images(payload, metadata, ["a", "b"], None)

        assert payload["images"] == ["a", "b"]
        assert metadata["task_type"] == "flf2v"
        # 裸键会被门面当作「原始输入」整单 400，一个都不能有
        assert "image" not in payload
        assert "last_frame" not in payload and "image_tail" not in metadata

    def test_gpustack_single_frame_leaves_task_type_inferred(self, tmp_path: Path):
        """单张首帧由模型名推断成 i2v，不必显式指定——少发一个键少一处出错面。"""
        backend = self._backend("wan2.2-i2v")
        payload: dict = {}
        metadata: dict = {}
        backend._apply_gpustack_images(payload, metadata, ["a"], None)

        assert payload["images"] == ["a"]
        assert "task_type" not in metadata

    def test_gpustack_reference_images_use_r2v_keys(self, tmp_path: Path):
        """参考直出走 r2v + src_ref_images；顶层 images 仍要留（门面据它判定有输入）。"""
        backend = self._backend("bernini")
        payload: dict = {}
        metadata: dict = {}
        backend._apply_gpustack_images(payload, metadata, ["r1", "r2"], "reference_image")

        assert metadata["task_type"] == "r2v"
        assert metadata["src_ref_images"] == ["r1", "r2"]
        assert payload["images"] == ["r1", "r2"]
        # 通用中转的写法不能混进来
        assert "image_urls" not in metadata and "image_role" not in metadata

    def test_gpustack_warns_when_tail_frame_would_be_dropped(self, tmp_path: Path, caplog):
        """未登记 flf2v 的自建模型收到尾帧时告警——门面会静默丢弃，无声降级最难查。"""
        backend = self._backend("wan2.2-t2v")
        payload: dict = {}
        metadata: dict = {}
        with caplog.at_level("WARNING"):
            backend._apply_gpustack_images(payload, metadata, ["a", "b"], None)

        assert "task_type" not in metadata
        assert "flf2v" in caplog.text

    def test_third_party_relay_keeps_dual_write(self, tmp_path: Path):
        """非自建模型保持「两套都发」——中转站事实标准那套不能丢。"""
        backend = self._backend("doubao-seedance-2-0-260128")
        request = VideoGenerationRequest(
            prompt="p",
            output_path=tmp_path / "o.mp4",
            aspect_ratio="16:9",
            duration_seconds=5,
            reference_images=[self._png(tmp_path, "r1.png")],
        )
        images, role = backend._collect_images(request)
        assert role == "reference_image"
        # 自建方言不应作用于第三方模型
        from lib.video_backends.newapi import is_gpustack_model

        assert is_gpustack_model("doubao-seedance-2-0-260128") is False

    def test_backend_does_not_veto_capabilities(self, tmp_path: Path):
        """能力门控在上层（系统判定 ⊕ 用户覆盖），backend 不按模型名二次否决。

        自定义供应商允许把 last_frame / reference_images 覆盖为 True；backend 若再按
        模型名默认能力过滤，用户显式开启的尾帧与参考图会被静默丢弃。
        """
        backend = self._backend("wan2.2-i2v")  # 系统默认判定：无参考图、无尾帧

        refs_only = VideoGenerationRequest(
            prompt="p",
            output_path=tmp_path / "o.mp4",
            aspect_ratio="16:9",
            duration_seconds=5,
            reference_images=[self._png(tmp_path, "r1.png")],
        )
        images, role = backend._collect_images(refs_only)
        assert len(images) == 1
        assert role == "reference_image"

        with_end = VideoGenerationRequest(
            prompt="p",
            output_path=tmp_path / "o.mp4",
            aspect_ratio="16:9",
            duration_seconds=5,
            start_image=self._png(tmp_path, "a.png"),
            end_image=self._png(tmp_path, "b.png"),
        )
        frames, role = backend._collect_images(with_end)
        assert len(frames) == 2
        assert role is None

    def test_start_image_wins_over_references(self, tmp_path: Path):
        backend = self._backend("doubao-seedance-2-0-260128")
        request = VideoGenerationRequest(
            prompt="p",
            output_path=tmp_path / "o.mp4",
            aspect_ratio="16:9",
            duration_seconds=5,
            start_image=self._png(tmp_path, "a.png"),
            reference_images=[self._png(tmp_path, "r1.png")],
        )
        images, role = backend._collect_images(request)
        assert len(images) == 1
        assert role is None

    def test_end_image_without_start_is_ignored(self, tmp_path: Path):
        backend = self._backend("doubao-seedance-2-0-260128")
        request = VideoGenerationRequest(
            prompt="p",
            output_path=tmp_path / "o.mp4",
            aspect_ratio="16:9",
            duration_seconds=5,
            end_image=self._png(tmp_path, "b.png"),
        )
        assert backend._collect_images(request) == ([], None)

    async def test_payload_carries_images_and_role(self, tmp_path: Path):
        create_resp = _make_response(200, {"task_id": "t-ref", "status": "queued"})
        done = _make_response(
            200,
            {"code": "success", "data": {"task_id": "t-ref", "status": "SUCCESS", "result_url": "https://obs/o.mp4"}},
        )
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=create_resp)
        mock_client.get = AsyncMock(return_value=done)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("lib.video_backends.newapi._POLL_INTERVAL_SECONDS", 0.0),
            patch("lib.video_backends.newapi.download_video", AsyncMock(side_effect=_fake_download_factory(b"m"))),
        ):
            from lib.video_backends.newapi import NewAPIVideoBackend

            backend = NewAPIVideoBackend(api_key="k", base_url="https://x/v1", model="doubao-seedance-2-0-260128")
            await backend.generate(
                VideoGenerationRequest(
                    prompt="p",
                    output_path=tmp_path / "o.mp4",
                    aspect_ratio="16:9",
                    duration_seconds=5,
                    reference_images=[self._png(tmp_path, "r1.png")],
                ),
            )

        body = mock_client.post.call_args.kwargs["json"]
        assert len(body["images"]) == 1
        assert body["images"][0].startswith("data:image/")
        assert body["metadata"]["image_role"] == "reference_image"
        # 中转站只认 metadata 黑盒里的参考数组（即梦语义），两套都要发。
        assert body["metadata"]["image_urls"] == body["images"]
        # 参考图模式不发老中转的单图键，避免上游把它当首帧。
        assert "image" not in body

    async def test_payload_carries_tail_frame_for_relay_stations(self, tmp_path: Path):
        """尾帧同样要发中转站认的 metadata.image_tail（可灵语义），只发 images[1] 会被忽略。"""
        create_resp = _make_response(200, {"task_id": "t-fl", "status": "queued"})
        done = _make_response(
            200,
            {"code": "success", "data": {"task_id": "t-fl", "status": "SUCCESS", "result_url": "https://obs/o.mp4"}},
        )
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=create_resp)
        mock_client.get = AsyncMock(return_value=done)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("lib.video_backends.newapi._POLL_INTERVAL_SECONDS", 0.0),
            patch("lib.video_backends.newapi.download_video", AsyncMock(side_effect=_fake_download_factory(b"m"))),
        ):
            from lib.video_backends.newapi import NewAPIVideoBackend

            backend = NewAPIVideoBackend(api_key="k", base_url="https://x/v1", model="doubao-seedance-1-5-pro")
            await backend.generate(
                VideoGenerationRequest(
                    prompt="p",
                    output_path=tmp_path / "o.mp4",
                    aspect_ratio="16:9",
                    duration_seconds=5,
                    start_image=self._png(tmp_path, "a.png"),
                    end_image=self._png(tmp_path, "b.png"),
                ),
            )

        body = mock_client.post.call_args.kwargs["json"]
        assert len(body["images"]) == 2
        assert body["image"] == body["images"][0]
        assert body["metadata"]["image_tail"] == body["images"][1]
        assert "image_role" not in body["metadata"]


class TestFrameInterpolation:
    """插帧只对白名单里的自建模型下发 metadata.target_fps，且受请求开关控制。"""

    async def _submit(self, tmp_path: Path, *, model: str, frame_interpolation: bool = True) -> dict:
        create_resp = _make_response(200, {"task_id": "t-vfi", "status": "queued"})
        done = _make_response(200, {"task_id": "t-vfi", "status": "completed", "url": "https://x/o.mp4"})
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=create_resp)
        mock_client.get = AsyncMock(return_value=done)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("lib.video_backends.newapi._POLL_INTERVAL_SECONDS", 0.0),
            patch("lib.video_backends.newapi.download_video", AsyncMock(side_effect=_fake_download_factory(b"v"))),
        ):
            from lib.video_backends.newapi import NewAPIVideoBackend

            backend = NewAPIVideoBackend(api_key="k", base_url="https://x/v1", model=model)
            await backend.generate(
                VideoGenerationRequest(
                    prompt="p",
                    output_path=tmp_path / "o.mp4",
                    aspect_ratio="9:16",
                    duration_seconds=5,
                    frame_interpolation=frame_interpolation,
                )
            )
        return mock_client.post.call_args.kwargs["json"]

    async def test_whitelisted_model_sends_target_fps(self, tmp_path: Path):
        for model in ("wan2.2-i2v", "wan2.2-t2v"):
            body = await self._submit(tmp_path, model=model)
            assert body["metadata"]["target_fps"] == 32, model

    async def test_model_id_is_case_and_space_insensitive(self, tmp_path: Path):
        body = await self._submit(tmp_path, model=" WAN2.2-I2V ")
        assert body["metadata"]["target_fps"] == 32

    async def test_third_party_lookalikes_are_excluded(self, tmp_path: Path):
        """阿里云的 wan2.2-i2v-plus / -flash 是同名不同货的第三方模型，不能靠子串匹配命中。"""
        for model in ("wan2.2-i2v-plus", "wan2.2-i2v-flash", "wan2.2-t2v-plus", "kling-v1"):
            body = await self._submit(tmp_path, model=model)
            assert "target_fps" not in (body.get("metadata") or {}), model

    async def test_switch_off_skips_target_fps(self, tmp_path: Path):
        body = await self._submit(tmp_path, model="wan2.2-i2v", frame_interpolation=False)
        assert "target_fps" not in (body.get("metadata") or {})

    def test_supports_frame_interpolation_helper(self):
        from lib.video_backends.newapi import supports_frame_interpolation

        assert supports_frame_interpolation("wan2.2-i2v") is True
        assert supports_frame_interpolation("wan2.2-i2v-plus") is False
        assert supports_frame_interpolation("") is False
