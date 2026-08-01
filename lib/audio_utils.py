"""音频工具：上传校验用的时长探测，与按时间窗切片。"""

from __future__ import annotations

import asyncio
import functools
import logging
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_FFPROBE_TIMEOUT_SECONDS = 10.0
_FFMPEG_TIMEOUT_SECONDS = 60.0

# ffprobe 的 format_name 是逗号分隔的候选容器列表（如 m4a 探测出
# "mov,mp4,m4a,3gp,3g2,mj2"），按扩展名要求其中必须含指定 token，
# 防止「有音轨但容器不是 wav/mp3」的文件（如把 m4a 改名为 .wav）蒙混过关。
_CONTAINER_FORMAT_TOKENS = {
    ".wav": {"wav"},
    ".mp3": {"mp3"},
}


@functools.cache
def _ffprobe_available() -> bool:
    """ffprobe 可执行文件是否在 PATH 中（结果缓存，避免每次调用重复 shutil.which）。"""
    return shutil.which("ffprobe") is not None


@functools.cache
def _ffmpeg_available() -> bool:
    """ffmpeg 可执行文件是否在 PATH 中（独立检查：精简容器可能只装了 ffprobe）。"""
    return shutil.which("ffmpeg") is not None


def _reset_for_tests() -> None:
    """test helper —— 清缓存让 monkeypatch shutil.which 立刻生效。"""
    _ffprobe_available.cache_clear()
    _ffmpeg_available.cache_clear()


async def _run_ffprobe(extra_args: list[str]) -> bytes:
    """执行一次 ffprobe 子进程，返回 stdout；超时/非零退出统一按不可解析处理。

    `-protocol_whitelist file` 限制 ffprobe 只读本地文件：上传字节可能嵌套
    HLS/RTMP 等播放列表引用，ffprobe 默认会跟随其中的协议自动发起网络请求
    （对内网地址同样生效），不加白名单会把这个探测调用变成 SSRF 跳板。
    超时同样按 ValueError 处理，避免损坏文件让 ffprobe 挂起占用请求。
    """
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-protocol_whitelist",
        "file",
        *extra_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_FFPROBE_TIMEOUT_SECONDS)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise ValueError("音频文件无法解析") from None

    if proc.returncode != 0:
        raise ValueError("音频文件无法解析")
    return stdout


async def probe_audio_duration_seconds(content: bytes, suffix: str) -> float | None:
    """探测音频字节的时长（秒），并确认其中确有可解码的音频流。

    ffprobe 不可用时返回 None（调用方按仓库惯例降级：跳过时长校验，不阻断上传），
    与 lib/thumbnail.py 的 ffmpeg/ffprobe 降级模式一致。

    Raises:
        ValueError: ffprobe 可用但无法解出时长、超时、容器内没有音频流
            （如把视频文件改名为 .wav/.mp3 上传），或探测出的容器格式与
            扩展名不符（如把 m4a/aac 改名为 .wav 上传）。
    """
    if not _ffprobe_available():
        logger.info("ffprobe 不可用，跳过音频时长探测")
        return None

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=tempfile.gettempdir(), suffix=suffix, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(content)
    except OSError:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise

    try:
        stream_types = await _run_ffprobe(
            ["-select_streams", "a", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(tmp_path)]
        )
        if b"audio" not in stream_types:
            raise ValueError("音频文件无法解析")

        expected_tokens = _CONTAINER_FORMAT_TOKENS.get(suffix.lower())
        if expected_tokens is not None:
            format_name_out = await _run_ffprobe(
                ["-show_entries", "format=format_name", "-of", "csv=p=0", str(tmp_path)]
            )
            detected_tokens = {token.strip() for token in format_name_out.decode().strip().split(",")}
            if not detected_tokens & expected_tokens:
                raise ValueError("音频文件无法解析")

        duration_out = await _run_ffprobe(["-show_entries", "format=duration", "-of", "csv=p=0", str(tmp_path)])
    except (FileNotFoundError, OSError):
        logger.info("ffprobe 调用失败，跳过音频时长探测")
        return None
    finally:
        tmp_path.unlink(missing_ok=True)

    try:
        return float(duration_out.decode().strip())
    except ValueError:
        raise ValueError("音频文件无法解析") from None


async def slice_audio_window(
    source: Path,
    output: Path,
    *,
    start_seconds: float,
    duration_seconds: float,
) -> Path:
    """把 ``source`` 的 ``[start, start+duration)`` 窗口切成独立文件写到 ``output``。

    与本模块另一半（上传探测）的降级策略相反：**ffmpeg 缺失或切片失败一律抛错，不回退到
    整轨**。调用方（MV 演唱镜头的口型驱动）拿整轨的后果是画面照常产出、口型对着歌曲开头的
    歌词，要到看成片才发现且看不出原因；探测类降级只是少一道校验，两者不可同日而语。

    ``-ss`` 放在 ``-i`` 之后走精确解码定位而非关键帧粗定位：口型对齐容不下半秒级偏移，
    而这里切的是几秒长的窗口，精确定位的额外开销可以忽略。输出统一重编码为 16bit PCM，
    不用 ``-c copy``——按帧边界对齐的流拷贝会把入点吸附到最近的包边界上。
    """
    if start_seconds < 0:
        raise ValueError(f"切片入点不能为负: {start_seconds}")
    if duration_seconds <= 0:
        raise ValueError(f"切片时长必须为正: {duration_seconds}")
    if not _ffmpeg_available():
        raise ValueError("需要 ffmpeg 按镜头时间窗切分驱动音频，但它不在 PATH 中：请先安装 ffmpeg")

    output.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-i",
        str(source),
        "-ss",
        f"{start_seconds:.3f}",
        "-t",
        f"{duration_seconds:.3f}",
        "-c:a",
        "pcm_s16le",
        str(output),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_FFMPEG_TIMEOUT_SECONDS)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        output.unlink(missing_ok=True)
        raise ValueError("切分驱动音频超时") from None

    if proc.returncode != 0 or not output.is_file():
        output.unlink(missing_ok=True)
        detail = stderr.decode(errors="replace").strip().splitlines()
        raise ValueError(f"切分驱动音频失败: {detail[-1] if detail else f'ffmpeg 退出码 {proc.returncode}'}")
    return output
