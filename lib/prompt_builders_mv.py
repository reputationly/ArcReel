"""MV 模式（content_mode=mv）剧本生成 Prompt 构建器。

产出平铺 ``shots[]`` 的镜头表，但与其余模式有一条根本差异：**时间轴由歌曲决定**。
镜头时长不是创作选择，是音乐段落的产物；镜头必须钉在歌曲的绝对时间点上
（``start_seconds``），而不是顺次累加——累加式排布只要有一镜的实际产出偏离规划值，
后面全部错位，而视频时长本就按供应商档位取整，偏离是常态。

故本构建器要求歌曲**先于剧本存在**：段落表与实测时长是输入，不是让 LLM 编的。

设计原则与 narration/drama/ad 构建器一致：不重复 schema 已声明的枚举、字段说明给写作
指引而非「必须/禁止」清单。
"""

from lib.prompt_builders_script import (
    _ACTION_WRITING_GUIDE,
    _AMBIANCE_AUDIO_WRITING_GUIDE,
    _AMBIANCE_WRITING_GUIDE,
    _LIGHTING_WRITING_GUIDE,
    _SCENE_WRITING_GUIDE,
    _format_aspect_ratio_desc,
    _format_duration_constraint,
    _format_names,
)

#: 单镜时长的软下界（秒）。MV 卡点可以很快，但短于此值的镜头在视频模型上几乎都会被
#: 取整到更长，规划再细也没有意义。
_MIN_SHOT_SECONDS = 2

_MV_PACING_GUIDE = """\
镜头节奏按段落性质分配，不平均切：

- intro / outro：镜头可长（4-8 秒），留白多，建立与收束氛围
- verse（主歌）：中等（3-5 秒），叙事推进，画面跟着歌词走
- chorus（副歌）：快切（2-3 秒），情绪最高点，可用重复构图强化记忆
- bridge：与副歌形成反差，节奏或画面质感突变

镜头必须完整铺满歌曲：相邻镜头 start_seconds 首尾相接、不留空隙也不重叠，
最后一镜的结束时间等于歌曲总时长。"""

_MV_PERFORMANCE_GUIDE = """\
is_performance 标记该镜是否人物出镜演唱：

- true：画面主体是歌手在唱这句词，口型要对上。这类镜头会走口型驱动生成，
  画面描述应给出清晰的正面/侧面人物构图，避免主体过小或被遮挡
- false：氛围镜、空镜、意象镜、场景镜。歌词可以留空

副歌通常需要至少一个 is_performance 镜头——那是观众记住这首歌的地方。
纯器乐段（intro/outro/bridge 常见）的 lyrics_line 填空串。"""


def _format_song_sections(song: dict) -> str:
    """渲染歌曲段落表。这是排镜头的硬约束，不是参考信息。"""
    sections = song.get("sections") or []
    if not sections:
        duration = song.get("duration_seconds") or 0
        return f"歌曲总时长 {duration:g} 秒，未提供段落表——按上述节奏指引自行划分段落并标注 section。"

    lines = ["| 段落 | 起点(s) | 时长(s) |", "|---|---|---|"]
    for section in sections:
        if not isinstance(section, dict):
            continue
        lines.append(
            f"| {section.get('name', '')} | {section.get('start_seconds', 0):g} | "
            f"{section.get('duration_seconds', 0):g} |"
        )
    total = song.get("duration_seconds") or 0
    lines.append("")
    lines.append(f"歌曲总时长 {total:g} 秒。每个镜头的 section 必须取自上表的段落名。")
    return "\n".join(lines)


def _format_lyrics(lyrics: str) -> str:
    if not lyrics.strip():
        return "（本曲无歌词，纯器乐——所有镜头的 lyrics_line 填空串，is_performance 一律 false）"
    return lyrics.strip()


def build_mv_prompt(
    project_overview: dict,
    style: str,
    style_description: str,
    characters: dict,
    scenes: dict,
    props: dict,
    song: dict,
    lyrics: str,
    generation_mode: str,
    supported_durations: list[int] | None,
    episode: int = 1,
    aspect_ratio: str = "16:9",
    target_language: str = "中文",
) -> str:
    """构建 MV 模式的剧本生成 prompt。

    ``song`` 须已携带实测时长（作曲步骤写回）：拿申请值排镜头会让全片逐渐错位。
    时长为 0 时直接拒绝——那说明作曲还没跑或产物未回写，此时生成的镜头表没有意义。
    """
    duration = song.get("duration_seconds") or 0
    if not isinstance(duration, (int, float)) or duration <= 0:
        raise ValueError(f"MV 剧本生成需要歌曲实测时长（song.duration_seconds），当前为 {duration!r}；请先生成音乐")

    if generation_mode == "reference_video":
        raise ValueError("MV 模式暂不支持参考直出：口型驱动依赖分镜图作人物首帧，请使用图生视频模式")

    if not supported_durations:
        raise ValueError("MV 剧本生成需要视频模型的合法时长集合（supported_durations）")

    duration_constraint = _format_duration_constraint(supported_durations, None)

    return f"""为这支 MV 生成镜头脚本（第 {episode} 集）。

## 歌曲

{_format_song_sections(song)}

## 歌词

{_format_lyrics(lyrics)}

## 画面风格

{style}
{style_description}

画幅：{_format_aspect_ratio_desc(aspect_ratio)}

## 可用素材

角色：{_format_names(characters)}
场景：{_format_names(scenes)}
道具：{_format_names(props)}

镜头里出现的角色/场景/道具只能取自上述名单，不要新造名字。

## 镜头节奏

{_MV_PACING_GUIDE}

## 演唱镜头

{_MV_PERFORMANCE_GUIDE}

## 时长约束

{duration_constraint}
单镜不短于 {_MIN_SHOT_SECONDS} 秒——更短的镜头会被视频模型取整拉长，规划再细也不生效。

## 字段写作指引

- **start_seconds**：该镜在歌曲时间轴上的入点。相邻镜头首尾相接，全片铺满歌曲
- **section**：取自段落表的段落名
- **lyrics_line**：该镜对应的那一句歌词；器乐段填空串
- **image_prompt.scene**：{_SCENE_WRITING_GUIDE}
- **image_prompt.lighting**：{_LIGHTING_WRITING_GUIDE}
- **image_prompt.ambiance**：{_AMBIANCE_WRITING_GUIDE}
- **video_prompt.action**：{_ACTION_WRITING_GUIDE}
- **video_prompt.ambiance_audio**：{_AMBIANCE_AUDIO_WRITING_GUIDE}。注意 MV 已有配乐，
  此处只写画面内的环境音（脚步、风声等），不要描述音乐本身

目标语言：{target_language}
"""
