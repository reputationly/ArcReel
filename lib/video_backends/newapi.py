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
from lib.resource_paths import audio_data_uri
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

# 插帧（RIFE 帧率翻倍）：随 metadata.target_fps 透传给 NewAPI，网关不校验该键、原样下发给
# 自建引擎（gpustack 门面对非控制字段直通，LightX2V 收到后折进 video_frame_interpolation）。
# 生成默认 16fps、RIFE v1 只做 16→32 的 2 倍插帧，故目标帧率恒为 32。
_INTERPOLATION_TARGET_FPS = 32

# 支持插帧的模型白名单——必须精确匹配，不能用子串。自建 gpustack 渠道的 wan2.2-t2v /
# wan2.2-i2v 才走 RIFE；阿里云渠道的 wan2.2-i2v-plus / wan2.2-i2v-flash / wan2.2-t2v-plus
# 是同名不同货的第三方模型，子串匹配会把 target_fps 发给厂商（无插帧效果，且平白多一个
# 上游可能拒收的未知参数）。新增自建插帧模型时在此登记。
_INTERPOLATION_MODELS = frozenset({"wan2.2-t2v", "wan2.2-i2v"})


def supports_frame_interpolation(model: str) -> bool:
    """model 是否在插帧白名单内（大小写与首尾空白无关）。"""
    return (model or "").strip().lower() in _INTERPOLATION_MODELS


# ---------------------------------------------------------------------------
# gpustackplus 自建渠道方言
#
# NewAPI 生态里图片入参有两套并存的写法（见 generate() 内注释），本 backend 对通用中转
# 两套都发。但自建 gpustackplus 渠道有第三套、且互斥：它按 ``metadata.task_type`` 分派
# 输入契约，键名逐类型不同，且门面维护一张「原始输入字段」剥离表——把 image / last_frame /
# src_ref_images 这些裸键塞进请求体会被**整单 400**（见 new-api 仓
# relay/channel/task/gpustackplus/adaptor.go 的 legacyInputKeys）。
#
# 故命中自建模型时改走本方言：只发 images[] + metadata.task_type，由门面按 task_type
# 物化成引擎入参。判定按模型名白名单而非 base_url——同一个网关可同时挂自建与第三方模型。
# ---------------------------------------------------------------------------

#: 自建 gpustackplus 引擎的模型名（精确匹配）。子串匹配会误伤同名不同货的第三方模型
#: （如阿里云的 wan2.2-i2v-plus），与 _INTERPOLATION_MODELS 同一理由。
_GPUSTACK_MODELS = frozenset(
    {
        "wan2.2-t2v",
        "wan2.2-i2v",
        "wan2.2-flf2v",
        "bernini",
        "infinitetalk-480p",
        "infinitetalk-720p",
        "seedvr2",
    }
)

#: 支持首尾帧的自建模型：task_type=flf2v，images=[首帧, 尾帧]（门面强制两张）。
#:
#: 首尾帧是**独立部署的模型**（wan2.2-flf2v），不是 wan2.2-i2v 的一种任务类型——门面按模型名
#: 推断 task_type（``strings.Contains(m, "flf2v")``），给 i2v 模型显式下发 flf2v 只会让门面按
#: 首尾帧物化两张图、再发给一个不认这个任务的引擎。能力声明必须跟着实际部署的模型名走。
_GPUSTACK_FLF2V_MODELS = frozenset({"wan2.2-flf2v"})

#: 支持纯参考图生视频的自建模型：task_type=r2v，参考图走 metadata.src_ref_images。
#: Bernini 另有 v2v / rv2v（需源视频），本 backend 暂不涉及——ArcReel 没有「源视频」这一输入。
_GPUSTACK_R2V_MODELS = frozenset({"bernini"})

#: 支持口型驱动（数字人）的自建模型：task_type=s2v，人物图走 images[0]、驱动音频走
#: metadata.audio。InfiniteTalk 按分辨率分两个模型，能力相同。
_GPUSTACK_S2V_MODELS = frozenset({"infinitetalk-480p", "infinitetalk-720p"})


def supports_lip_sync(model: str) -> bool:
    """model 是否支持口型驱动（s2v）。MV 的演唱镜头据此选模型。"""
    return (model or "").strip().lower() in _GPUSTACK_S2V_MODELS


#: Bernini 参考图张数上限。门面未声明硬上限，取与 Seedance 2.0 同档的保守值；
#: 用户可在自定义供应商的「参考图上限」栏覆盖（见 CAPABILITY_OVERRIDE_ALLOWLIST）。
_BERNINI_MAX_REFERENCE_IMAGES = 4


def is_gpustack_model(model: str) -> bool:
    """model 是否为自建 gpustackplus 引擎（决定走 task_type 方言还是通用中转写法）。"""
    return (model or "").strip().lower() in _GPUSTACK_MODELS


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

        中转端点本身只是转发，能力取决于网关背后挂的模型。三类分别声明：

        - 自建 gpustackplus 引擎：能力按门面的 task_type 契约展开（wan2.2-i2v 支持
          首尾帧走 flf2v、bernini 支持纯参考图走 r2v）；
        - Seedance 家族：支持首尾帧，2.0 另外支持多模态参考图（1~9 张，与首帧模式互斥）；
        - 其余（未知第三方中转）：保守默认，避免请求发出去才被上游拒。

        instance property 委托至此，保持 backend 为单一真相源。
        """
        name = (model or "").lower()
        if is_gpustack_model(name):
            return VideoCapabilities(
                last_frame=name in _GPUSTACK_FLF2V_MODELS,
                max_reference_images=(_BERNINI_MAX_REFERENCE_IMAGES if name in _GPUSTACK_R2V_MODELS else 0),
            )
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

        # 插帧只对白名单里的自建模型下发。这是尽力而为的增强：引擎侧没开 VFI 时只会 warning
        # 后按原帧率出片（不报错），所以调用方不能假定产物一定是 32fps —— 实际帧率以回包
        # metadata.fps 为准。顶层 fps 字段对该网关无效（不被转发），只有 metadata.target_fps 生效。
        if request.frame_interpolation and supports_frame_interpolation(self._model):
            metadata["target_fps"] = _INTERPOLATION_TARGET_FPS

        # 口型驱动：驱动音频非空即走 s2v。必须在图片装配之前判定——s2v 的人物图走
        # images[0]、与 i2v 的首帧同槽，但 task_type 不同，装配后再改会与下面的分派打架。
        if request.driving_audio is not None:
            self._apply_lip_sync(metadata, request.driving_audio)

        images, image_role = self._collect_images(request)
        if images:
            if is_gpustack_model(self._model):
                self._apply_gpustack_images(payload, metadata, images, image_role)
            else:
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

    def _apply_lip_sync(self, metadata: dict, driving_audio: Path) -> None:
        """装配口型驱动入参：task_type=s2v + metadata.audio。

        音频编码成 data-uri 由门面物化到 input_refs——裸键 ``audio`` 混进请求体会被
        门面当作「原始输入字段」整单 400（与图片入参同一机制）。

        模型不支持 s2v 时只告警不改写：能力门控在上层（executor 按模型能力选路），
        backend 二次否决会让用户显式配置的模型被静默降级成无声视频——口型对不上的
        演唱镜头比直接报错更难发现。
        """
        if not supports_lip_sync(self._model):
            logger.warning("模型 %s 未登记 s2v 能力，驱动音频可能不被门面接受", self._model)
        metadata["task_type"] = "s2v"
        metadata["audio"] = audio_data_uri(driving_audio, label="驱动音频")

    def _apply_gpustack_images(
        self,
        payload: dict,
        metadata: dict,
        images: list[str],
        image_role: str | None,
    ) -> None:
        """按自建门面的 task_type 契约装配图片入参。

        与通用中转的「两套都发」相反，这里**只能发一套**：门面对 image / last_frame /
        src_ref_images 等裸键有剥离表，混发会整单 400（adaptor.go 的 legacyInputKeys）。
        输入统一走顶层 ``images[]``，由 ``metadata.task_type`` 决定门面如何物化：

        - 首帧（1 张）→ i2v，由模型名推断，无需显式 task_type；
        - 首尾帧（2 张）→ **flf2v 必须显式指定**（模型名只能推断出 i2v，那条分支不读
          images[1]，尾帧会被静默丢弃——声明支持尾帧却不受约束是最坏的一种失败）；
        - 纯参考图 → **r2v 必须显式指定**，且参考图走 metadata.src_ref_images
          （bernini 由模型名推断出的是 v2v，那要源视频，我们没有）。
        """
        payload["images"] = images
        model = (self._model or "").strip().lower()

        if image_role is not None:
            # 参考直出：仅 r2v 能力的模型走得通。门面对 r2v 从 metadata.src_ref_images 取图，
            # 顶层 images 同时保留——HasImage() 据它判定「有输入」，缺了会被输入防呆拒。
            metadata["task_type"] = "r2v"
            metadata["src_ref_images"] = images
            if model not in _GPUSTACK_R2V_MODELS:
                # 能力守卫在 executor 侧（参考图被裁空即中止），走到这里说明能力声明与
                # 模型不符；不静默改写语义，留一条 warning 供排查。
                logger.warning("模型 %s 未登记 r2v 能力，参考图可能不被门面接受", self._model)
            return

        if len(images) > 1:
            if model in _GPUSTACK_FLF2V_MODELS:
                metadata["task_type"] = "flf2v"
            else:
                # 门面 i2v 分支只读 images[0]，多传的尾帧会被静默丢弃。宁可显式告警，
                # 也不让「界面声明支持尾帧、成片却没有尾帧约束」这种无声降级发生。
                logger.warning("模型 %s 未登记 flf2v 能力，尾帧将被门面忽略", self._model)

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
        return normalize_newapi_task_state(resp.json())

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


def normalize_newapi_task_state(payload: object) -> dict:
    """把查询回包统一成 {status, url, metadata, error} —— 上层只认这一种形状。

    **音乐/歌声后端复用本函数**（``lib.audio_backends.newapi_music``）：它们打的是同一个
    ``/v1/video/generations`` 任务端点，回包形状自然相同。各写一份必然漂移——信封分支就是
    第一次漂移的地方（音乐侧曾只认扁平形态，导致任务永远等不到终态、轮询到超时）。

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
