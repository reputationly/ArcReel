"""语音合成（TTS）服务层核心接口定义。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class AudioCapability(StrEnum):
    """音频后端支持的能力枚举。"""

    TEXT_TO_SPEECH = "text_to_speech"
    TEXT_TO_MUSIC = "text_to_music"
    SINGING_SYNTHESIS = "singing_synthesis"


@dataclass
class AudioSynthesisRequest:
    """通用语音合成请求。各 Backend 忽略不支持的字段。"""

    text: str
    output_path: Path
    voice: str
    language_type: str = "Chinese"
    # 语速预留：同步 qwen3-tts-flash 不支持（speech_rate 仅 realtime WebSocket 版可用），
    # 后端记 debug log 忽略。保留字段以便将来接入实时/可调速后端。
    speed: float | None = None


@dataclass
class AudioSynthesisResult:
    """通用语音合成结果。``characters`` 驱动按字符计费。"""

    provider: str
    model: str
    characters: int
    output_path: Path


class AudioBackend(Protocol):
    """语音合成后端协议。"""

    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def capabilities(self) -> set[AudioCapability]: ...

    async def synthesize(self, request: AudioSynthesisRequest) -> AudioSynthesisResult: ...


@dataclass
class MusicGenerationRequest:
    """通用音乐生成请求。

    与 :class:`AudioSynthesisRequest` 分开而不是加字段：TTS 是「把这段文字念出来」，
    音乐是「按这个描述作一首曲子」——前者的 text 是逐字产出的内容，后者的 prompt 是
    风格指令，两者对空值、长度、计费的语义都不同（TTS 按字符计费，音乐按秒/次）。
    合成一个 dataclass 会让两边都带着对方用不上的字段。

    ``duration_seconds`` 为 None 时由引擎自行决定时长（ACE-Step 有默认值）。
    ``reference_audio`` 服务 cover（翻唱）/ repaint（重绘）两种任务，t2m 不用。
    """

    prompt: str
    output_path: Path
    duration_seconds: int | None = None
    reference_audio: Path | None = None
    #: 歌词。ACE-Step 是 caption + lyrics 双输入：给了歌词就按词唱，留空则引擎按描述
    #: 自动作词（sample 模式）。MV 的歌词由用户定稿，必须走这里传进去——否则引擎会
    #: 自己编一版词，与剧本里排好的 lyrics_line 对不上。
    lyrics: str | None = None
    #: 速度（BPM）与演唱语言，引擎可选参数；留空由引擎决定。
    bpm: int | None = None
    vocal_language: str | None = None


@dataclass
class MusicGenerationResult:
    """音乐生成结果。``duration_seconds`` 取回包实测值，是 MV 排布镜头的时间轴依据。"""

    provider: str
    model: str
    output_path: Path
    duration_seconds: float | None = None


@dataclass
class SingingSynthesisRequest:
    """歌声合成请求（SoulX-Singer svs）。

    两个音频输入语义不同、都必填：``voice_reference`` 是**音色**样本（谁来唱），
    ``target_song`` 是**目标曲/伴奏**（唱什么旋律）。缺任一方引擎都无法产出——
    没有音色就不知道用谁的嗓子，没有目标曲就不知道唱什么调。

    刻意**没有 text 字段**：引擎按音频生成歌声，歌词不作为结构化输入
    （门面对 svs 的 prompt 仅作占位）。
    """

    voice_reference: Path
    target_song: Path
    output_path: Path


@dataclass
class SingingSynthesisResult:
    provider: str
    model: str
    output_path: Path
    duration_seconds: float | None = None


class MusicBackend(Protocol):
    """音乐生成后端协议。

    与 ``AudioBackend`` 并列而非继承：同一个 provider 可能只做其中一件事
    （qwen3-tts 只有 TTS、ACE-Step 只有音乐），用继承会逼着实现方写空方法。
    两者共用 worker 的 audio 并发通道——受同一批 GPU 约束，分成两条通道会让
    总并发翻倍打满显存。
    """

    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def capabilities(self) -> set[AudioCapability]: ...

    async def generate_music(self, request: MusicGenerationRequest) -> MusicGenerationResult: ...
