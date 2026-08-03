"""NewAPIMusicBackend —— 经 NewAPI 中转的音乐生成（ACE-Step 系）。

音乐与视频**共用同一个异步任务端点** ``/v1/video/generations``：提交拿 task_id、
轮询到终态、下载产物。区别只在 ``model`` 与 ``metadata.task_type``（t2m / cover /
repaint）。故本 backend 复用 video_backends.base 的提交/轮询/重试三件套，不另起一套
HTTP 栈——两处各写一份，重试策略与状态词表迟早漂移。

产物是音频（.wav），不是视频；``download_video`` 只是按 URL 落盘的通用下载，名字带
video 是历史，行为与媒体类型无关。

回包归一同样复用视频侧的 ``normalize_newapi_task_state``——同一个端点、同样两种回包形态
（扁平响应 / ``{"code","data"}`` 任务信封）。各写一份的代价是实测过的：音乐侧曾只认扁平
形态，信封部署下任务永远等不到终态、一路轮询到超时。
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from lib.audio_backends.base import (
    AudioCapability,
    MusicGenerationRequest,
    MusicGenerationResult,
    SingingSynthesisRequest,
    SingingSynthesisResult,
)
from lib.logging_utils import format_kwargs_for_log
from lib.resource_paths import AUDIO_EXTENSIONS, audio_data_uri
from lib.retry import (
    DEFAULT_BACKOFF_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DOWNLOAD_BACKOFF_SECONDS,
    DOWNLOAD_MAX_ATTEMPTS,
    with_retry_async,
)
from lib.video_backends.base import (
    download_video,
    poll_with_retry,
    should_retry_poll,
    should_retry_submit,
    submit_post,
)
from lib.video_backends.newapi import normalize_newapi_task_state

logger = logging.getLogger(__name__)

PROVIDER_NEWAPI_MUSIC = "newapi"

DEFAULT_MODEL = "acestep-v15-xl-turbo"

_POLL_INTERVAL_SECONDS = 5.0
_POLL_TIMEOUT_SECONDS = 900.0

#: 音乐任务的 task_type。t2m 纯文本作曲；cover 按参考音频翻唱、repaint 按源音频重绘，
#: 两者都需要 ``reference_audio``（门面键名不同，见 _resolve_task_type）。
_TASK_TYPE_T2M = "t2m"
_TASK_TYPE_COVER = "cover"
#: 歌声合成（SoulX-Singer）。门面对 svs 的 prompt 仅作占位，真正的输入是两段音频。
_TASK_TYPE_SVS = "svs"
#: svs 的占位 prompt。门面在 prompt 为空时会兜底同一个标签；显式带上让日志里能一眼
#: 看出这是歌声合成而非空请求。
_SVS_PROMPT_PLACEHOLDER = "soulx-singer"


class NewAPIMusicBackend:
    """经 NewAPI 中转调用自建 ACE-Step 引擎作曲。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str | None = None,
        http_timeout: int = 300,
        provider_name: str | None = None,
    ):
        if not api_key:
            raise ValueError("NewAPIMusicBackend 需要 api_key")
        if not base_url:
            raise ValueError("NewAPIMusicBackend 需要 base_url")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model or DEFAULT_MODEL
        self._http_timeout = http_timeout
        # 自定义供应商包装时传真实 provider_id，让记账与日志归因到用户配的那个 provider，
        # 而非内置 newapi（与 OpenAIAudioBackend 的 provider_name 同一理由）。
        self._provider_name = provider_name or PROVIDER_NEWAPI_MUSIC

    @property
    def name(self) -> str:
        return self._provider_name

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> set[AudioCapability]:
        return {AudioCapability.TEXT_TO_MUSIC, AudioCapability.SINGING_SYNTHESIS}

    async def generate_music(self, request: MusicGenerationRequest) -> MusicGenerationResult:
        payload = self._build_payload(request)
        logger.info("调用 %s 音乐生成 payload=%s", self.name, format_kwargs_for_log(payload))

        async with httpx.AsyncClient(timeout=self._http_timeout) as client:
            task_id = await self._create_task(client, payload)
            logger.info("NewAPI 音乐任务创建: task_id=%s", task_id)
            state = await poll_with_retry(
                poll_fn=lambda: self._poll_once(client, task_id),
                is_done=lambda s: s.get("status") in ("completed", "failed", "expired"),
                is_failed=_extract_failure,
                poll_interval=_POLL_INTERVAL_SECONDS,
                max_wait=_POLL_TIMEOUT_SECONDS,
                retry_if=should_retry_poll,
                label="NewAPI music",
            )

        status = state.get("status")
        if status != "completed":
            err = (state.get("error") or {}).get("message") or status or "unknown"
            raise RuntimeError(f"NewAPI 音乐生成失败: {err}")

        url = state.get("url")
        if not url:
            raise RuntimeError(f"NewAPI 音乐生成返回体缺少产物 URL: {state}")

        output_path = _output_with_real_ext(request.output_path, url)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        await self._download_with_retry(url, output_path)
        _drop_stale_siblings(output_path)

        metadata = state.get("metadata") or {}
        duration = metadata.get("duration")
        return MusicGenerationResult(
            provider=self.name,
            model=self._model,
            output_path=output_path,
            duration_seconds=float(duration) if isinstance(duration, (int, float)) else None,
        )

    async def synthesize_singing(self, request: SingingSynthesisRequest) -> SingingSynthesisResult:
        """歌声合成：音色参考 + 目标曲 → 歌声音频。

        与作曲共用提交/轮询/下载链，只是 task_type 与输入不同。两段音频都必填，
        缺任一方在编码阶段就 fail-loud——引擎侧缺输入会产出一段无关音频并照常计费。
        """
        payload = {
            "model": self._model,
            "prompt": _SVS_PROMPT_PLACEHOLDER,
            "metadata": {
                "task_type": _TASK_TYPE_SVS,
                "prompt_audio": _encode_reference_audio(request.voice_reference),
                "target_audio": _encode_reference_audio(request.target_song),
            },
        }
        logger.info("调用 %s 歌声合成 payload=%s", self.name, format_kwargs_for_log(payload))

        async with httpx.AsyncClient(timeout=self._http_timeout) as client:
            task_id = await self._create_task(client, payload)
            logger.info("NewAPI 歌声合成任务创建: task_id=%s", task_id)
            state = await poll_with_retry(
                poll_fn=lambda: self._poll_once(client, task_id),
                is_done=lambda s: s.get("status") in ("completed", "failed", "expired"),
                is_failed=_extract_failure,
                poll_interval=_POLL_INTERVAL_SECONDS,
                max_wait=_POLL_TIMEOUT_SECONDS,
                retry_if=should_retry_poll,
                label="NewAPI svs",
            )

        if state.get("status") != "completed":
            err = (state.get("error") or {}).get("message") or state.get("status") or "unknown"
            raise RuntimeError(f"NewAPI 歌声合成失败: {err}")
        url = state.get("url")
        if not url:
            raise RuntimeError(f"NewAPI 歌声合成返回体缺少产物 URL: {state}")

        output_path = _output_with_real_ext(request.output_path, url)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        await self._download_with_retry(url, output_path)
        _drop_stale_siblings(output_path)

        metadata = state.get("metadata") or {}
        duration = metadata.get("duration")
        return SingingSynthesisResult(
            provider=self.name,
            model=self._model,
            output_path=output_path,
            duration_seconds=float(duration) if isinstance(duration, (int, float)) else None,
        )

    def _build_payload(self, request: MusicGenerationRequest) -> dict:
        """装配 ACE-Step 请求。

        引擎读的是 ``metadata`` 下的一组参数，**不是**顶层 duration——顶层那个是视频
        任务的受控字段，音乐时长走 ``metadata.audio_duration``，发错位置等于没填。

        歌词决定两种作曲模式：
        - **给了歌词**：描述作 caption、歌词直接透传，引擎按词唱；
        - **没给歌词**（仅 t2m）：开 ``sample_mode``，引擎按描述用 LM 自动生成
          caption + 歌词。``prompt`` 仍保持描述文本——门面要求 prompt 必填，且不认
          sample_mode 的路径可靠它兜底。
        """
        task_type = _resolve_task_type(request)
        metadata: dict = {"task_type": task_type}
        payload: dict = {
            "model": self._model,
            "prompt": request.prompt,
        }
        if request.duration_seconds is not None:
            metadata["audio_duration"] = request.duration_seconds

        lyrics = (request.lyrics or "").strip()
        if lyrics:
            metadata["lyrics"] = lyrics
        elif task_type == _TASK_TYPE_T2M:
            # 无歌词的纯 t2m：让引擎自己作词，否则出来是纯器乐。
            metadata["sample_mode"] = True
            metadata["sample_query"] = request.prompt

        if request.bpm is not None:
            metadata["bpm"] = request.bpm
        if request.vocal_language:
            metadata["vocal_language"] = request.vocal_language

        if request.reference_audio is not None:
            # 输入统一走 input_refs 物化：裸键 reference_audio / src_audio 混进请求体
            # 会被门面当作「原始输入字段」整单 400，故编码成 data-uri 挂在 metadata 下
            # 由门面接管（与视频侧图片入参同一机制）。
            metadata["reference_audio"] = _encode_reference_audio(request.reference_audio)
        payload["metadata"] = metadata
        return payload

    @with_retry_async(
        max_attempts=DEFAULT_MAX_ATTEMPTS,
        backoff_seconds=DEFAULT_BACKOFF_SECONDS,
        retry_if=should_retry_submit,
    )
    async def _create_task(self, client: httpx.AsyncClient, payload: dict) -> str:
        # 装饰器与 submit_post 是配套的两半（见 submit_post docstring）：包装只负责把歧义态
        # （请求可能已送达）转成不可重试的终态异常，真正的重试由装饰器做。只用包装不加装饰器
        # 的话，连接建立失败这类「请求确定未送达」的瞬态错误会直接判整次生成失败——同一个
        # 端点上视频侧会重试三次、音乐侧一次不试，可靠性凭空低一档。
        resp = await submit_post(
            lambda: client.post(
                f"{self._base_url}/video/generations",
                json=payload,
                headers=self._headers(),
            ),
            provider=self.name,
        )
        body = resp.json()
        task_id = body.get("task_id")
        if not task_id:
            raise RuntimeError(f"NewAPI 创建音乐任务返回体缺少 task_id: {body}")
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
    async def _download_with_retry(url: str, output_path: Path) -> None:
        await download_video(url, output_path)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}


def _output_with_real_ext(output_path: Path, url: str) -> Path:
    """按产物 URL 的真实扩展名调整落盘路径。

    同一个 backend 下不同模型的产物格式不同：ACE-Step 作曲出 ``.mp3``、SoulX-Singer 歌声
    合成出 ``.wav``。调用方按资源类型给的是默认扩展名，这里以实际产物为准改写——内容与
    后缀不符会在两处出问题：后续按后缀推导 MIME 给门面贴错标签，以及播放器/剪辑器按后缀
    选解码器。URL 无法识别扩展名时保留调用方给的默认值。
    """
    stem = url.split("?", 1)[0].rsplit("/", 1)[-1]
    ext = Path(stem).suffix.lower()
    return output_path.with_suffix(ext) if ext in AUDIO_EXTENSIONS else output_path


def _drop_stale_siblings(kept: Path) -> None:
    """删掉同名不同后缀的旧产物（``music/main.wav`` vs ``music/main.mp3``）。

    音乐 / 歌声是**项目级单件产物**（一支片子一首曲、一条主唱轨），重复生成即覆盖。但换了
    产出格式不同的模型后，新文件是写在旧文件**旁边**而非覆盖它——而读取侧按固定优先级遍历
    候选扩展名（``resource_candidate_paths``），于是导出与口型驱动会一直选中那个陈旧的、
    上一个模型留下的音轨，任务却报告了新路径，两边说法不一。

    在**下载成功之后**才清理：先删后下的话，下载失败会让用户既没有新的也没有旧的。
    删除失败只告警不抛——产物已经落好，为清理旧文件失败而让整个任务失败不划算。
    """
    for ext in AUDIO_EXTENSIONS:
        stale = kept.with_suffix(ext)
        if stale == kept or not stale.exists():
            continue
        try:
            stale.unlink()
            logger.info("清理同名旧格式音频: %s（当前产物 %s）", stale.name, kept.name)
        except OSError:
            logger.warning("清理同名旧格式音频失败，读取侧可能选中陈旧文件: %s", stale, exc_info=True)


def _resolve_task_type(request: MusicGenerationRequest) -> str:
    """带参考音频即翻唱（cover），否则纯文本作曲（t2m）。

    repaint（按源音频重绘）与 cover 输入形态相同、门面键名不同，靠请求本身分不出来，
    需要调用方显式指定——当前没有调用方需要它，不臆造入口。
    """
    return _TASK_TYPE_COVER if request.reference_audio is not None else _TASK_TYPE_T2M


def _encode_reference_audio(path: Path) -> str:
    """音色参考音频编码成 data-uri。合法扩展名与上传路由 ``character_audio_ref`` 对齐。"""
    return audio_data_uri(path, label="参考音频")


def _extract_failure(state: dict) -> str | None:
    """轮询终态为 failed 时给出错误消息，供 poll_with_retry 提前中止。"""
    if state.get("status") != "failed":
        return None
    err = (state.get("error") or {}).get("message") or "unknown"
    return f"NewAPI 音乐生成失败: {err}"
