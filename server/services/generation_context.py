"""GenerationContext —— 生成任务 provider 解析产物的单次收口入口（见 ``docs/adr/0049``）。

``resolve_generation_context`` 在单个 ConfigResolver session 内完成全部声明 lane 的解析与
backend 构造，返回不可变的 :class:`GenerationContext`（MediaGenerator + 各 lane 结果值对象）。
每条 lane 固定求解顺序：解析 ProviderModel → 经 ``assemble_backend``（``docs/adr/0039``）构造
backend → 按实际身份查 resolution 与能力。

查询身份 =（规范 registry provider_id, backend 实际 model）：provider 在构造缝中不可能漂移，
而族别名 provider（如 ark-agent-plan 复用 Ark backend）的 ``backend.name`` 是族名、非 registry
key，不能用作查询键；model 是唯一真实漂移轴（自定义供应商目标 model 被禁用时 loader 静默回退），
故取 backend 实际 ``.model``。lane 结果同时暴露 ``provider_model``（规范 registry 身份）与
``backend_name`` / ``backend_model``（backend 报告的实际身份）两组字段。

backend 实例缓存随本模块承载：缓存是 server 执行层关切（``docs/adr/0039``「缓存留在调用方」），
供应商配置变更路由经 ``invalidate_backend_cache()`` 统一失效。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from lib.backend_assembly import assemble_backend
from lib.config.resolver import ConfigResolver, get_provider_fallback
from lib.db.base import DEFAULT_USER_ID
from lib.gemini_shared import get_shared_rate_limiter
from lib.media_generator import MediaGenerator
from lib.project_manager import get_project_manager

if TYPE_CHECKING:
    from lib.config.resolver import ProviderModel

logger = logging.getLogger(__name__)

rate_limiter = get_shared_rate_limiter()

_CacheKey = tuple[str, str, str | None]


class _BackendCache:
    """Backend 实例缓存：按 (media_type, provider_name, model) 复用实例，避免每次任务重建 API 客户端。

    缓存查询/构造/写回/失效在此单点实现，两条并发纪律藏在实现内、不扩大接口：

    - **代际 invariant**：``invalidate()`` 时代数 +1；代数须在等锁前（而非取得锁后）捕获，
      构造完成后代数未变才写回。代数已变——无论是本请求持锁构造期间发生失效，还是本请求在
      失效边界前排队等锁、失效后才拿到锁——该实例用完即弃，不写回缓存遮蔽新配置；该笔任务
      仍按入队时配置快照跑完。
    - **per-key single-flight**：同 key 并发 miss 经 per-key 锁串行化，只构造一次、各调用方
      拿到同一实例，避免并发构造出无人持有的多余 SDK client（全库无 backend 关闭协议）。
    """

    def __init__(self) -> None:
        self._entries: dict[_CacheKey, Any] = {}
        self._locks: dict[_CacheKey, asyncio.Lock] = {}
        self._generation = 0

    async def get_or_create(self, key: _CacheKey, factory: Callable[[], Awaitable[Any]]) -> Any:
        if key in self._entries:
            return self._entries[key]
        # 代数须在等锁前捕获：若在失效边界前排队等锁，即使失效后才拿到锁，也要按排队时的
        # 旧代数与失效后的当前代数不符处理，避免用旧 resolver 构造的实例污染新代际缓存。
        generation = self._generation
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self._entries:
                return self._entries[key]
            backend = await factory()
            if self._generation == generation:
                self._entries[key] = backend
            return backend

    def invalidate(self) -> None:
        self._generation += 1
        self._entries.clear()


_backend_cache = _BackendCache()


def invalidate_backend_cache() -> None:
    """清空 Backend 实例缓存。在供应商配置变更后调用。"""
    _backend_cache.invalidate()


async def _get_or_create_backend(
    media_type: str,
    provider_name: str,
    provider_settings: dict,
    resolver: ConfigResolver,
    default_model: str | None,
) -> Any:
    """组 key + 提供 factory closure，缓存纪律统一委托 :class:`_BackendCache`。"""
    effective_model = provider_settings.get("model") or default_model or None

    async def _factory() -> Any:
        return await assemble_backend(
            provider_id=provider_name,
            media_type=media_type,
            model_id=effective_model,
            resolver=resolver,
            rate_limiter=rate_limiter,
        )

    return await _backend_cache.get_or_create((media_type, provider_name, effective_model), _factory)


async def _get_or_create_video_backend(
    provider_name: str,
    provider_settings: dict,
    resolver: ConfigResolver,
    *,
    default_video_model: str | None = None,
):
    """获取或创建 VideoBackend 实例（带缓存）。

    provider_name 可以是旧格式（gemini/seedance/grok）或新格式（gemini-aistudio/gemini-vertex）。
    通过 resolver 按需加载供应商配置。
    default_video_model: 全局默认视频模型，当 provider_settings 中无 model 时作为 fallback。
    """
    return await _get_or_create_backend("video", provider_name, provider_settings, resolver, default_video_model)


async def _get_or_create_image_backend(
    provider_name: str,
    provider_settings: dict,
    resolver: ConfigResolver,
    *,
    default_image_model: str | None = None,
):
    """获取或创建 ImageBackend 实例（带缓存）。"""
    return await _get_or_create_backend("image", provider_name, provider_settings, resolver, default_image_model)


async def _get_or_create_audio_backend(
    provider_name: str,
    provider_settings: dict,
    resolver: ConfigResolver,
    *,
    default_audio_model: str | None = None,
):
    """获取或创建 AudioBackend 实例（带缓存）。audio 无媒体特例：自定义 + 简单族统一经构造缝。"""
    return await _get_or_create_backend("audio", provider_name, provider_settings, resolver, default_audio_model)


async def _get_or_create_music_backend(
    provider_name: str,
    resolver: ConfigResolver,
    *,
    default_music_model: str | None = None,
):
    """获取或创建 MusicBackend（带缓存）。

    走与 audio 相同的 ``media_type="audio"`` 构造缝：音乐没有独立 media_type（复用同一条
    并发通道），实际落到哪个 backend 由用户给该 model 选的 endpoint 决定——``newapi-music``
    产出 MusicBackend，``openai-tts`` 产出 AudioBackend。选错 endpoint 的后果由执行器的
    能力检查兜住（缺 generate_music 即 fail-loud），不在这里按模型名猜。
    """
    return await _get_or_create_backend("audio", provider_name, {}, resolver, default_music_model)


@dataclass(frozen=True)
class ImageLaneRequest:
    """声明本次任务需要 image lane。capability 决定 t2i / i2i 默认槽（``docs/adr/0001``）。"""

    capability: Literal["t2i", "i2i"] = "t2i"


@dataclass(frozen=True)
class VideoLaneRequest:
    """声明本次任务需要 video lane。

    ``lip_sync`` 为真时解析的是**口型驱动模型**（InfiniteTalk 一类的 s2v 模型）而非项目
    配置的常规视频模型：普通图生视频模型没有 s2v 能力，拿它跑演唱镜头要么被门面拒，
    要么更糟——照常出片但口型对不上，看成片才发现。仅 MV 的演唱镜头需要。
    """

    lip_sync: bool = False


@dataclass(frozen=True)
class AudioLaneRequest:
    """声明本次任务需要 audio lane（旁白 TTS）。"""


@dataclass(frozen=True)
class MusicLaneRequest:
    """声明本次任务需要 music lane。

    与 audio lane 分列：TTS 与作曲是不同模型、常在不同 provider，共用一条 lane 会让
    「配了 TTS 就以为能作曲」。两者仍共用 worker 的 audio 并发通道（同一批 GPU）。

    ``task_type`` 再分作曲（music）与歌声合成（singing）：ACE-Step 只会作曲、
    SoulX-Singer 只会唱，共用一个模型配置会把请求发给不会那件事的模型——而调用侧
    拦不住，同一个 backend 类承载两种能力，方法恒在，差异在模型上。
    """

    task_type: Literal["music", "singing"] = "music"


@dataclass(frozen=True)
class ImageLaneResult:
    """image lane 解析产物。

    ``provider_model`` 是规范 registry 身份；``backend_name`` / ``backend_model`` 是构造后
    backend 报告的实际身份——自定义供应商目标 model 被禁用回退时 ``backend_model`` 可能与
    ``provider_model.model_id`` 不同。``resolution`` 为 None 表示调用时不传 SDK 参数
    （``docs/adr/0019``）。
    """

    provider_model: ProviderModel
    backend_name: str
    backend_model: str
    resolution: str | None


@dataclass(frozen=True)
class VideoLaneResult:
    """video lane 解析产物。

    能力字段（``supported_durations`` / ``max_duration`` / ``max_reference_images``）在能力
    查询失败时降级为空值（空元组 / None）放行：能力是已选定 provider/model 的元数据，缺失
    不代表不可调用，守卫遇空值不施加限制、把决策推给 backend。``resolution_or_fallback``
    供需要非空档位的调用方（参考视频路径），其余语义同 :class:`ImageLaneResult`。
    """

    provider_model: ProviderModel
    backend_name: str
    backend_model: str
    resolution: str | None
    resolution_or_fallback: str
    supported_durations: tuple[int, ...]
    max_duration: int | None
    max_reference_images: int | None


@dataclass(frozen=True)
class AudioLaneResult:
    """audio lane 解析产物。narration voice/speed 与 backend 解析在同一 session 内交付。"""

    provider_model: ProviderModel
    backend_name: str
    backend_model: str
    narration_voice: str
    narration_speed: float | None


@dataclass(frozen=True)
class MusicLaneResult:
    """music lane 解析产物。

    只交付 backend 本身，不进 MediaGenerator：音乐是项目级单件产物（一支片子一首曲），
    不像 audio 那样按分镜逐条产出，MediaGenerator 的版本管理与批量语义对它没有价值。
    记账由执行器直接经 ledger 完成。
    """

    provider_model: ProviderModel
    backend_name: str
    backend_model: str
    backend: object


def _lane_not_declared(lane: str, request_hint: str) -> RuntimeError:
    return RuntimeError(
        f"{lane} lane 未声明：调用 resolve_generation_context 时传入 {request_hint} 才能访问该 lane 的解析产物"
    )


@dataclass(frozen=True)
class GenerationContext:
    """单次解析交付的全部产物：MediaGenerator + 各声明 lane 的结果值对象。

    lane 字段为 None 表示该 lane 未声明；经同名 property 访问未声明 lane 直接抛
    RuntimeError（fail-loud，返回类型非 Optional）。测试可用本 dataclass 直接拼装假 context。
    """

    generator: MediaGenerator
    image_lane: ImageLaneResult | None = None
    video_lane: VideoLaneResult | None = None
    audio_lane: AudioLaneResult | None = None
    music_lane: MusicLaneResult | None = None

    @property
    def image(self) -> ImageLaneResult:
        if self.image_lane is None:
            raise _lane_not_declared("image", "image=ImageLaneRequest(...)")
        return self.image_lane

    @property
    def video(self) -> VideoLaneResult:
        if self.video_lane is None:
            raise _lane_not_declared("video", "video=VideoLaneRequest()")
        return self.video_lane

    @property
    def audio(self) -> AudioLaneResult:
        if self.audio_lane is None:
            raise _lane_not_declared("audio", "audio=AudioLaneRequest()")
        return self.audio_lane

    @property
    def music(self) -> MusicLaneResult:
        if self.music_lane is None:
            raise _lane_not_declared("music", "music=MusicLaneRequest()")
        return self.music_lane


async def resolve_generation_context(
    project_name: str,
    payload: dict | None,
    *,
    project: dict,
    user_id: str = DEFAULT_USER_ID,
    image: ImageLaneRequest | None = None,
    video: VideoLaneRequest | None = None,
    audio: AudioLaneRequest | None = None,
    music: MusicLaneRequest | None = None,
) -> GenerationContext:
    """在单个 ConfigResolver session 内解析全部声明 lane、构造 backend 并组装 MediaGenerator。

    lane 传即声明、None 跳过，任务只为用到的 lane 付出配置要求与构造成本。任一声明 lane
    的解析或构造失败即原样上抛、整次调用失败——无部分结果、无跨 provider 兜底；仅能力
    查询失败降级空值放行。``project`` 是调用方已加载的项目快照，本函数不读盘。
    """
    from lib.db import async_session_factory

    project_path = await asyncio.to_thread(get_project_manager().get_project_path, project_name)
    resolver = ConfigResolver(async_session_factory)

    image_result: ImageLaneResult | None = None
    video_result: VideoLaneResult | None = None
    audio_result: AudioLaneResult | None = None
    music_result: MusicLaneResult | None = None
    image_backend: Any = None
    video_backend: Any = None
    audio_backend: Any = None

    async with resolver.session() as r:
        if image is not None:
            resolved = await r.resolve_image_backend(project, payload, capability=image.capability)
            image_backend = await _get_or_create_image_backend(
                resolved.provider_id,
                {},
                r,
                default_image_model=resolved.model_id or None,
            )
            image_result = ImageLaneResult(
                provider_model=resolved,
                backend_name=image_backend.name,
                backend_model=image_backend.model,
                resolution=await r.resolve_resolution(project, resolved.provider_id, image_backend.model),
            )

        if video is not None:
            if video.lip_sync:
                resolved = await r.resolve_lip_sync_backend(project, payload)
                if not resolved.provider_id or not resolved.model_id:
                    raise RuntimeError(
                        "尚未配置口型驱动模型：请到设置页「模型选择 → 口型驱动模型」选择一个"
                        "支持数字人（s2v）的模型，如 infinitetalk-720p"
                    )
            else:
                resolved = await r.resolve_video_backend(project, payload)
            video_backend = await _get_or_create_video_backend(
                resolved.provider_id,
                {},
                r,
                default_video_model=resolved.model_id or None,
            )
            actual_model = video_backend.model
            resolution = await r.resolve_resolution(project, resolved.provider_id, actual_model)
            supported_durations: tuple[int, ...] = ()
            max_duration: int | None = None
            max_reference_images: int | None = None
            try:
                caps = await r.video_capabilities_for_model(resolved.provider_id, actual_model, project)
                supported_durations = tuple(int(d) for d in caps.get("supported_durations") or [])
                max_duration = caps.get("max_duration")
                max_reference_images = caps.get("max_reference_images")
            except Exception as exc:
                logger.info(
                    "无法解析 video capabilities（%s/%s），能力值降级为空：%s",
                    resolved.provider_id,
                    actual_model,
                    exc,
                )
            video_result = VideoLaneResult(
                provider_model=resolved,
                backend_name=video_backend.name,
                backend_model=actual_model,
                resolution=resolution,
                resolution_or_fallback=resolution or get_provider_fallback(resolved.provider_id),
                supported_durations=supported_durations,
                max_duration=max_duration,
                max_reference_images=max_reference_images,
            )

        if audio is not None:
            resolved = await r.resolve_audio_backend(project, payload)
            audio_backend = await _get_or_create_audio_backend(
                resolved.provider_id,
                {},
                r,
                default_audio_model=resolved.model_id or None,
            )
            audio_result = AudioLaneResult(
                provider_model=resolved,
                backend_name=audio_backend.name,
                backend_model=audio_backend.model,
                narration_voice=await r.resolve_narration_voice(project),
                narration_speed=await r.resolve_narration_speed(project),
            )

        if music is not None:
            resolved_music = await r.resolve_music_backend(project, payload, task_type=music.task_type)
            if not resolved_music.provider_id or not resolved_music.model_id:
                # 未配置：给出可操作的指引，而不是让 assemble_backend 抛
                # 「no builtin ProviderSpec」——那句话对用户没有任何意义。
                label = "音乐" if music.task_type == "music" else "歌声"
                raise RuntimeError(
                    f"尚未配置{label}模型：请到设置页「模型选择 → {label}模型」选择一个"
                    f"自定义供应商下的模型（{label}模型需经自定义供应商接入）"
                )
            music_backend = await _get_or_create_music_backend(
                resolved_music.provider_id,
                r,
                default_music_model=resolved_music.model_id or None,
            )
            if not hasattr(music_backend, "generate_music"):
                # 选到了 TTS endpoint 而非 newapi-music：能力不符即刻失败，不让请求带着
                # 一个不会作曲的 backend 走到执行层（那里只会抛 AttributeError，更难定位）。
                raise RuntimeError(
                    f"provider {resolved_music.provider_id} 的模型 {resolved_music.model_id} 不具备音乐生成能力："
                    f"请在自定义供应商里把该模型的 endpoint 选为 newapi-music"
                )
            music_result = MusicLaneResult(
                provider_model=resolved_music,
                backend_name=music_backend.name,
                backend_model=music_backend.model,
                backend=music_backend,
            )

    generator = MediaGenerator(
        project_path,
        rate_limiter=rate_limiter,
        image_backend=image_backend,
        video_backend=video_backend,
        audio_backend=audio_backend,
        config_resolver=resolver,
        user_id=user_id,
        image_provider_id=image_result.provider_model.provider_id if image_result else None,
        video_provider_id=video_result.provider_model.provider_id if video_result else None,
        audio_provider_id=audio_result.provider_model.provider_id if audio_result else None,
    )
    return GenerationContext(
        generator=generator,
        image_lane=image_result,
        video_lane=video_result,
        audio_lane=audio_result,
        music_lane=music_result,
    )
