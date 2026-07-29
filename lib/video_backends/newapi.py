"""NewAPIVideoBackend — NewAPI 统一视频生成端点后端。

对接 NewAPI 的 /v1/video/generations 接口，支持 Sora / Kling / 即梦 / Wan / Veo
等多家厂商模型，靠请求体的 model 字段分发。
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from lib.aspect_size import VIDEO_TIER_SHORT_EDGE, aspect_size, resolution_to_short_edge
from lib.logging_utils import format_kwargs_for_log
from lib.providers import PROVIDER_NEWAPI
from lib.retry import (
    DEFAULT_BACKOFF_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DOWNLOAD_BACKOFF_SECONDS,
    DOWNLOAD_MAX_ATTEMPTS,
    with_retry_async,
)
from lib.video_backends.base import (
    ProviderJobIdPersistenceMixin,
    ResumeExpiredError,
    VideoCapabilities,
    VideoGenerationRequest,
    VideoGenerationResult,
    download_video,
    poll_with_retry,
    should_retry_poll,
    should_retry_submit,
    submit_post,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "kling-v1"

_POLL_INTERVAL_SECONDS = 5.0
_MIN_POLL_TIMEOUT_SECONDS = 600
_POLL_TIMEOUT_PER_SECOND = 30

# 超过此阈值的起始图会触发 warning，NewAPI 聚合后端常见 4MB 请求体上限
_LARGE_IMAGE_WARN_BYTES = 4 * 1024 * 1024

# 视频标准尺寸对齐 8 的倍数（1920x1080 / 1080x1920 等；1080 非 16 的倍数），主流视频模型通用。
_VIDEO_ROUND_TO = 8

# Seedance 2.0 多模态参考图上限（上游 1~9 张）。
_MAX_REFERENCE_IMAGES = 9


def _is_seedance(model: str) -> bool:
    return "seedance" in model


def _is_seedance_2(model: str) -> bool:
    return "seedance-2" in model or "seedance2" in model or "seedance-2-0" in model


def _resolve_size(resolution: str | None, aspect_ratio: str) -> tuple[int, int]:
    """比例优先、清晰度其次：短边来自 resolution（档位 / 自定义 / None 兜底 720P），
    比例精确来自 aspect_ratio、对齐 8 的倍数。修复旧表 1080 不被整除 + 仅 9:16/16:9 两档。
    """
    short = resolution_to_short_edge(resolution, tier_map=VIDEO_TIER_SHORT_EDGE)
    return aspect_size(aspect_ratio, short, round_to=_VIDEO_ROUND_TO)


class NewAPIVideoBackend(ProviderJobIdPersistenceMixin):
    """NewAPI 统一视频生成端点后端。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str | None = None,
        http_timeout: float = 60.0,
    ) -> None:
        if not api_key:
            raise ValueError("NewAPIVideoBackend 需要 api_key")
        if not base_url:
            raise ValueError("NewAPIVideoBackend 需要 base_url")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model or DEFAULT_MODEL
        self._http_timeout = http_timeout

    @property
    def name(self) -> str:
        return PROVIDER_NEWAPI

    @property
    def model(self) -> str:
        return self._model

    @staticmethod
    def video_capabilities_for_model(model: str) -> VideoCapabilities:
        """按 model_id 纯计算 caps —— 不构造 SDK client（无需 api_key）。

        中转端点本身只是转发，能力取决于网关背后挂的模型。Seedance 家族支持首尾帧，
        2.0 另外支持多模态参考图（1~9 张，与首帧模式互斥）；其余模型保持保守默认，
        避免请求发出去才被上游拒。instance property 委托至此，保持 backend 为单一真相源。
        """
        name = (model or "").lower()
        if not _is_seedance(name):
            return VideoCapabilities(max_reference_images=0)
        if _is_seedance_2(name):
            return VideoCapabilities(last_frame=True, max_reference_images=_MAX_REFERENCE_IMAGES)
        return VideoCapabilities(last_frame=True, max_reference_images=0)

    @property
    def video_capabilities(self) -> VideoCapabilities:
        return self.video_capabilities_for_model(self._model)

    async def generate(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        width, height = _resolve_size(request.resolution, request.aspect_ratio)
        # width/height 是老中转实现的键;上游 new-api 的统一任务契约只读 size(WxH)与
        # metadata(引擎原生参数从这里透传),两套都发,谁认哪个各取所需,互不干扰。
        payload: dict = {
            "model": self._model,
            "prompt": request.prompt,
            "width": width,
            "height": height,
            "size": f"{width}x{height}",
            "duration": request.duration_seconds,
            "n": 1,
        }
        metadata: dict = {}
        if request.seed is not None:
            payload["seed"] = request.seed
            metadata["seed"] = request.seed

        images, image_role = self._collect_images(request)
        if images:
            # 图片入参在 NewAPI 生态里有两套并存的写法，都发，谁认哪套各取所需：
            #   1. 顶层 images[] + metadata.image_role —— 上游 new-api 的统一任务契约
            #      （TaskSubmitReq.Images，role 按位置推断，image_role 显式钉住语义）；
            #   2. image + metadata.image_tail / metadata.image_urls —— 中转站事实标准，
            #      入参只有 image 与 metadata 黑盒，尾帧沿用可灵的 image_tail、参考数组沿用
            #      即梦的 image_urls（见 docs/research/arcreel-video-api-protocol-research.md
            #      §2.2 与参数对齐表 NewAPI 列）。
            # 只发第 1 套的话，中转站部署会看不到尾帧/参考图 —— 能力宣称支持、生成却不受约束。
            payload["images"] = images
            if image_role is None:
                payload["image"] = images[0]
                if len(images) > 1:
                    metadata["image_tail"] = images[1]
            else:
                metadata["image_role"] = image_role
                metadata["image_urls"] = images
        if metadata:
            payload["metadata"] = metadata

        logger.info("NewAPI 视频生成开始: model=%s, duration=%s", self._model, request.duration_seconds)
        logger.info("调用 %s 视频 SDK payload=%s", self.name, format_kwargs_for_log(payload))

        async with httpx.AsyncClient(timeout=self._http_timeout) as client:
            provider_task_id = await self._create_task(client, payload)
            logger.info("NewAPI 任务创建: task_id=%s", provider_task_id)
            await self._persist_provider_job_id(request, provider_task_id, provider=PROVIDER_NEWAPI)
            return await self._poll_and_build(client, provider_task_id, request, is_resume=False)

    def _collect_images(self, request: VideoGenerationRequest) -> tuple[list[str], str | None]:
        """把图片输入编码成 data-uri 列表，并给出显式 role（None = 按首帧/首尾帧语义）。

        只做上游协议的**结构**约束：三种图片模式互斥（单图首帧 / 首尾帧 / 多张参考图），
        尾帧必须与首帧成对。同时给首帧和参考图时以首帧模式为准并告警——两者一起发上游
        会整单拒绝。

        **能力门控不在这里做**：生效能力是「系统按模型判定 ⊕ 用户覆盖」的合成结果
        （见 lib/custom_provider/capabilities.py），由 executor 在调用前裁剪。backend 若
        再按模型名二次否决，用户在自定义供应商上显式开启的尾帧 / 参考图就会被静默丢弃，
        界面宣称支持而实际生成无该约束。其余 backend（v2 / ark 等）同样不做这层否决。
        """
        references = list(request.reference_images or [])

        if references and not request.start_image:
            return [uri for path in references if (uri := self._encode_image(path, "reference_image"))], (
                "reference_image"
            )

        if references and request.start_image:
            logger.warning("首帧与参考图模式互斥，已忽略 %d 张参考图", len(references))

        frames: list[str] = []
        if request.start_image and (uri := self._encode_image(request.start_image, "start_image")):
            frames.append(uri)
            # 尾帧必须与首帧成对出现，单独给尾帧上游无法解释。
            if request.end_image and (uri := self._encode_image(request.end_image, "end_image")):
                frames.append(uri)
        elif request.end_image:
            logger.warning("提供了尾帧但缺少首帧，已忽略: %s", request.end_image)
        return frames, None

    @staticmethod
    def _encode_image(path: Path | str, label: str) -> str | None:
        """读盘编码成 data-uri；文件不存在返回 None（与原行为一致：告警后跳过）。"""
        image_path = Path(path)
        if not image_path.exists():
            logger.warning("%s 文件不存在，已忽略: %s", label, image_path)
            return None
        size_bytes = image_path.stat().st_size
        if size_bytes > _LARGE_IMAGE_WARN_BYTES:
            logger.warning(
                "NewAPI %s 较大 (%.1fMB)，Base64 编码后可能触发服务端请求体限制",
                label,
                size_bytes / 1024 / 1024,
            )
        # 延迟导入避免 image_backends ↔ video_backends 循环依赖
        from lib.image_backends.base import image_to_base64_data_uri

        return image_to_base64_data_uri(image_path)

    async def resume_video(self, job_id: str, request: VideoGenerationRequest) -> VideoGenerationResult:
        """接续已 submit 的 NewAPI task：仅 poll + 下载。"""
        async with httpx.AsyncClient(timeout=self._http_timeout) as client:
            return await self._poll_and_build(client, job_id, request, is_resume=True)

    async def _poll_and_build(
        self,
        client: httpx.AsyncClient,
        task_id: str,
        request: VideoGenerationRequest,
        *,
        is_resume: bool,
    ) -> VideoGenerationResult:
        # _is_done 纯谓词：completed / failed / expired 均视为终态；caller 按 is_resume
        # flag 决定 expired 抛 RuntimeError（generate）还是 ResumeExpiredError（resume）。
        # resume 路径下 404 由 _gated_poll 直接抛 ResumeExpiredError：should_retry_poll 把
        # 轮询 404 当作"短暂未就绪"重试，对已过期的 resume 任务会一直重到 max_wait 超时、
        # 永不落 [resume_expired]，对应 pending ApiCall 也不走 failed/cost=0 路径，故在此一击
        # 转终态异常。非 resume 的 4xx 重新抛出，交 should_retry_poll 按 status_code 分流。
        async def _gated_poll() -> dict:
            try:
                return await self._poll_once(client, task_id)
            except httpx.HTTPStatusError as exc:
                if is_resume and exc.response.status_code == 404:
                    raise ResumeExpiredError(job_id=task_id, provider=PROVIDER_NEWAPI) from exc
                raise

        final = await poll_with_retry(
            poll_fn=_gated_poll,
            is_done=lambda state: state.get("status") in ("completed", "failed", "expired"),
            is_failed=_extract_failure,
            poll_interval=_POLL_INTERVAL_SECONDS,
            max_wait=self._max_wait(request.duration_seconds),
            retry_if=should_retry_poll,
            label="NewAPI",
        )

        if final.get("status") == "expired":
            if is_resume:
                raise ResumeExpiredError(
                    job_id=task_id,
                    provider=PROVIDER_NEWAPI,
                    message=f"NewAPI task expired: {task_id}",
                )
            raise RuntimeError(f"NewAPI task expired during generate: {task_id}")

        video_url = final.get("url")
        if not video_url:
            raise RuntimeError(f"NewAPI 任务完成但缺少 url 字段: {final}")

        # 流式下载，不携带 Authorization 头（视频 URL 常为 CDN/OSS，避免 API Key 泄露）
        await self._download_with_retry(video_url, request.output_path)

        meta = final.get("metadata") or {}
        raw_duration = meta.get("duration")
        duration_seconds = int(float(raw_duration)) if raw_duration is not None else request.duration_seconds
        return VideoGenerationResult(
            video_path=request.output_path,
            provider=PROVIDER_NEWAPI,
            model=self._model,
            duration_seconds=duration_seconds,
            task_id=task_id,
            seed=meta.get("seed"),
        )

    @with_retry_async(
        max_attempts=DEFAULT_MAX_ATTEMPTS,
        backoff_seconds=DEFAULT_BACKOFF_SECONDS,
        retry_if=should_retry_submit,
    )
    async def _create_task(self, client: httpx.AsyncClient, payload: dict) -> str:
        resp = await submit_post(
            lambda: client.post(
                f"{self._base_url}/video/generations",
                json=payload,
                headers=self._headers(),
            ),
            provider=PROVIDER_NEWAPI,
        )
        body = resp.json()
        task_id = body.get("task_id")
        if not task_id:
            raise RuntimeError(f"NewAPI 创建任务返回体缺少 task_id: {body}")
        return task_id

    async def _poll_once(self, client: httpx.AsyncClient, task_id: str) -> dict:
        resp = await client.get(
            f"{self._base_url}/video/generations/{task_id}",
            headers=self._headers(),
        )
        resp.raise_for_status()
        return _normalize_task_state(resp.json())

    @staticmethod
    @with_retry_async(
        max_attempts=DOWNLOAD_MAX_ATTEMPTS,
        backoff_seconds=DOWNLOAD_BACKOFF_SECONDS,
        retry_if=should_retry_poll,
    )
    async def _download_with_retry(video_url: str, output_path: Path) -> None:
        """对齐 OpenAI/Ark 的下载重试策略（5 次、5/10/20/40 秒），与生成阶段独立。"""
        await download_video(video_url, output_path)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    @staticmethod
    def _max_wait(duration_seconds: int) -> float:
        return max(_MIN_POLL_TIMEOUT_SECONDS, duration_seconds * _POLL_TIMEOUT_PER_SECOND)


def _extract_failure(state: dict) -> str | None:
    if state.get("status") != "failed":
        return None
    err = (state.get("error") or {}).get("message") or "unknown"
    return f"NewAPI 视频生成失败: {err}"


# 查询回包的状态串 → canonical(小写后查表)。NewAPI 系有两套词表:
#   - 文档化的视频任务响应:queued / in_progress / completed / failed;
#   - 通用任务模型透出的内部态:NOT_START / SUBMITTED / QUEUED / IN_PROGRESS / SUCCESS / FAILURE。
# 未收录的串一律当"仍在跑"继续轮询,不误判成终态。
_STATUS_ALIASES: dict[str, str] = {
    "completed": "completed",
    "succeeded": "completed",
    "success": "completed",
    "failed": "failed",
    "failure": "failed",
    "error": "failed",
    "expired": "expired",
}


def _normalize_task_state(payload: object) -> dict:
    """把查询回包统一成 {status, url, metadata, error} —— 上层只认这一种形状。

    NewAPI 的 ``GET /v1/video/generations/{id}`` 有两种回包:

    1. 扁平的视频任务响应(文档形态、部分中转实现)::

           {"task_id": ..., "status": "completed", "url": ..., "metadata": {...}}

    2. 通用任务信封(上游 new-api 现行实现,relay/relay_task.go)::

           {"code": "success", "data": {"task_id": ..., "status": "SUCCESS",
                                        "result_url": ..., "fail_reason": ...}}

    只认第 1 种会永远等不到终态——信封里没有顶层 status,轮询会一直跑到超时。
    """
    if not isinstance(payload, dict):
        return {"status": "", "url": "", "metadata": {}, "error": None}

    inner = payload
    if "data" in payload and isinstance(payload.get("data"), dict):
        inner = payload["data"]

    raw_status = str(inner.get("status") or "").strip().lower()
    status = _STATUS_ALIASES.get(raw_status, raw_status)

    metadata = inner.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    url = ""
    for candidate in (inner.get("url"), metadata.get("url"), inner.get("result_url")):
        if isinstance(candidate, str) and candidate.strip():
            url = candidate.strip()
            break

    # error 归一成 {"message": ...},_extract_failure 只按这一种形状取。
    error: dict | None = None
    raw_error = inner.get("error")
    if isinstance(raw_error, dict):
        error = raw_error
    elif isinstance(raw_error, str) and raw_error.strip():
        error = {"message": raw_error.strip()}
    elif isinstance(inner.get("fail_reason"), str) and inner["fail_reason"].strip():
        error = {"message": inner["fail_reason"].strip()}

    return {"status": status, "url": url, "metadata": metadata, "error": error}
