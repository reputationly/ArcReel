"""资源路径解析器 — 「资源类型 → 项目内相对路径」的唯一真相源。

纯函数，不读盘、不持有项目状态。独家拥有各资源类型的子目录、文件名模板、
扩展名，以及 storyboards/end_frames/videos（``scene_``）、audio（``segment_``）的文件名前缀。

写侧（MediaGenerator）、版本回溯（versions 路由）、导入修复（project_archive）、
版本管理（VersionManager）都从这里取形状，避免副本各自漂移。越界校验不在此处，
由调用方拼绝对路径时自行负责。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ResourcePattern:
    """单一资源类型的路径形状。"""

    subdir: str
    extension: str
    prefix: str = ""  # 文件名前缀：storyboards/videos 用 "scene_"，audio 用 "segment_"，其余空


# 尾帧快照的资源类型名。独立导出到这个无反向依赖的纯函数模块，供
# server/services/end_frame.py（写侧）与 generation_tasks.py（读侧）共用，避免二者互相
# import 对方所在的 server.services 包造成循环依赖；同时作为 `_PATTERNS` 对应 key 的唯一
# 来源，防止两处字面量各自维护后读写侧路径口径分叉。
END_FRAME_RESOURCE_TYPE = "end_frames"

_PATTERNS: dict[str, ResourcePattern] = {
    "storyboards": ResourcePattern("storyboards", ".png", prefix="scene_"),
    # 尾帧快照与分镜图、镜头视频同按镜头 id 命名，故共用 scene_ 前缀。
    END_FRAME_RESOURCE_TYPE: ResourcePattern(END_FRAME_RESOURCE_TYPE, ".png", prefix="scene_"),
    "videos": ResourcePattern("videos", ".mp4", prefix="scene_"),
    "characters": ResourcePattern("characters", ".png"),
    "scenes": ResourcePattern("scenes", ".png"),
    "props": ResourcePattern("props", ".png"),
    "products": ResourcePattern("products", ".png"),
    "grids": ResourcePattern("grids", ".png"),
    "reference_videos": ResourcePattern("reference_videos", ".mp4"),
    "audio": ResourcePattern("audio", ".wav", prefix="segment_"),
    # 音乐是项目级单件产物（一支 MV 一首曲子），不像 audio 那样按分镜逐条产出，
    # 故无 segment_ 前缀——resource_id 直接是曲目标识（如 "main"）。
    "music": ResourcePattern("music", ".wav"),
    # 歌声合成产物。与 music 同目录不同前缀——一支 MV 可能有多条人声轨（主唱/和声），
    # 而伴奏只有一条；分开命名让二者不互相覆盖。
    "singing": ResourcePattern("music", ".wav", prefix="vocal_"),
}

RESOURCE_TYPES: tuple[str, ...] = tuple(_PATTERNS)


def _pattern(resource_type: str) -> ResourcePattern:
    pattern = _PATTERNS.get(resource_type)
    if pattern is None:
        raise ValueError(f"不支持的资源类型: {resource_type}")
    return pattern


#: 音乐 / 歌声类产物可能出现的扩展名（含点，按优先级排列）。
#:
#: 同一个 resource_type 下不同模型的产物格式**不同**：ACE-Step 作曲出 ``.mp3``、
#: SoulX-Singer 歌声合成出 ``.wav``。落盘必须按实际产物的扩展名，读取侧则按 stem 逐个候选找
#: （``resource_candidate_paths``）。写死一种的后果是内容与后缀不符——把 MP3 存成 ``.wav``，
#: 之后按后缀推导 MIME 就会给门面贴错标签，而错误发生在引擎侧、指不回标签这一层。
AUDIO_EXTENSIONS: tuple[str, ...] = (".wav", ".mp3")

#: 扩展名 → data-uri MIME。键集与 ``AUDIO_EXTENSIONS`` 必须一致，故与它同处一个模块——
#: 分开放会漂移：曾经作曲侧按扩展名取 MIME、口型侧仍写死 WAV，同一条规则两份实现只改了一份。
AUDIO_MIME_BY_SUFFIX: dict[str, str] = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
}


def is_outdated_by(derived: Path, source: Path) -> bool:
    """派生产物是否已被它的输入超越（输入更新于产物）。

    MV 里人声轨（``music/vocal_main.*``）派生自作曲产物（``music/main.*``）——
    ``synthesize_singing`` 的 target_song 就是后者。用户重新作曲却没重跑歌声合成时，盘上两个
    文件都在，读取侧按「人声轨优先」拿到的是**上一版曲子**唱出来的人声：导出的成片配的是旧曲，
    演唱镜的口型也对着旧旋律，而 ArcReel 内部完全看不出来（分镜、视频、字幕都对）。

    用 mtime 而非内容指纹：判定跑在导出与视频生成的热路径上，读全文件算哈希不划算；两者都是
    本地落盘的生成产物，mtime 就是生成时刻。相等判为不过期——同一次流水线里先后落盘的两个
    文件可能落在同一秒。取不到 mtime 时判为不过期：判定本身失败不该让导出或生成停摆。
    """
    try:
        return source.stat().st_mtime > derived.stat().st_mtime
    except OSError:
        return False


def audio_data_uri(path: Path, *, label: str) -> str:
    """音频编码成 data-uri，供门面物化到 input_refs。

    ``label`` 只用于报错文案（「参考音频」/「驱动音频」），让异常直接指出是哪一路入参。

    文件不存在直接抛：静默跳过会退化成一段没有音频驱动的产物（翻唱变随机作曲、演唱镜口型
    乱动），错误显现在成片里而非调用处。MIME 按扩展名取、未知扩展名 fail-loud——MP3 字节
    顶着 audio/wav 标签送进门面，物化或解码会在引擎侧失败，指不回「标签贴错了」。
    """
    if not path.exists():
        raise FileNotFoundError(f"{label}不存在: {path}")
    mime = AUDIO_MIME_BY_SUFFIX.get(path.suffix.lower())
    if mime is None:
        raise ValueError(f"不支持的{label}格式: {path.name}（支持 {sorted(AUDIO_MIME_BY_SUFFIX)}）")
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{payload}"


def resource_relative_path(resource_type: str, resource_id: str, *, ext: str | None = None) -> str:
    """返回资源在项目内的相对路径（posix，正斜杠）。

    storyboards/end_frames/videos 形如 ``storyboards/scene_{id}.png``、audio 形如 ``audio/segment_{id}.wav``；
    其余 ``{subdir}/{id}{ext}``。未知类型抛 ``ValueError``。

    ``ext`` 覆盖默认扩展名（含点，大小写不敏感），供产物格式随模型而变的类型使用——写侧拿到
    实际产物后按真实扩展名落盘，避免内容与后缀不符。
    """
    pattern = _pattern(resource_type)
    filename = f"{pattern.prefix}{resource_id}"
    suffix = ext.lower() if ext else pattern.extension
    return f"{pattern.subdir}/{filename}{suffix}"


def resource_candidate_paths(resource_type: str, resource_id: str) -> tuple[str, ...]:
    """该资源所有可能的相对路径（按优先级）。读取侧遍历它来定位实际落盘的那个。

    音频类产物的扩展名随模型而变（见 ``AUDIO_EXTENSIONS``），读取侧按固定扩展名找会漏掉
    另一种格式——表现为「文件明明生成了，导出/播放却说没有」。非音频类只有一种形状，
    返回单元素元组，调用方无需分支。
    """
    pattern = _pattern(resource_type)
    if pattern.extension not in AUDIO_EXTENSIONS:
        return (resource_relative_path(resource_type, resource_id),)
    ordered = (pattern.extension, *(e for e in AUDIO_EXTENSIONS if e != pattern.extension))
    return tuple(resource_relative_path(resource_type, resource_id, ext=e) for e in ordered)


def resource_extension(resource_type: str) -> str:
    """返回资源类型的文件扩展名（含点，如 ``.png``）。未知类型抛 ``ValueError``。"""
    return _pattern(resource_type).extension
