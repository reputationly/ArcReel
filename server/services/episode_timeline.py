"""单集时间线的收集，与导出目标格式无关。

从剧本里把「哪些镜头已出片、各自多长、配什么字幕、之间怎么转场」抽成中立的片段列表，
供各导出格式各自转写：剪映草稿（``jianying_draft_service``）与 ChatCut 交接包
（``chatcut_handoff_service``）读同一份收集结果。

拆出来的理由是这部分逻辑与目标格式无关却容易被复制：按内容骨架取分镜数组、按模式取字幕
文案源、drama 从 utterances 派生 span、ad 参考路径按 unit 收集——每条都是分派表驱动的，
复制一份就意味着新增内容模式时要改两处，而漏改的那一处不会报错，只会导出一份内容不全的
草稿。
"""

import logging
from pathlib import Path
from typing import Any

from lib.path_safety import safe_resolve
from lib.reference_video.ad_units import ad_shots_by_id
from lib.resource_paths import is_outdated_by, resource_candidate_paths
from lib.script_models import SUBTITLE_TEXT_FIELDS, ad_shot_duration_seconds, get_generated_assets
from lib.script_skeleton import SKELETONS, resolve_declared_kind
from lib.speech_rate import estimate_spoken_seconds

logger = logging.getLogger(__name__)

# content_mode → 整段单字幕文案源字段，收敛到 lib.script_models 单点声明。
# 该表是 TTS 口播表的超集：narration / ad 两条链读同一份文案（各写一份会漂移成
# 「字幕有词、配音没声」），mv 只在字幕侧登记——歌词是字幕但不能拿去念。
# drama 不在表内：口播是场景级有序 utterances，按 span 逐条派生
# （见 SPAN_SUBTITLE_MODES / utterance_subtitle_spans）。
_SUBTITLE_TEXT_FIELDS = SUBTITLE_TEXT_FIELDS

# 字幕由有序 span 派生（而非单字段）的内容模式。drama 从 utterances 派生 subtitle_spans；
# ad + reference_video 路径虽也产 span，但 content_mode 仍是 ad（已在 SUBTITLE_TEXT_FIELDS），
# 故此处只列 drama。未注册且不在此集合的模式（未知脏值）不挂字幕轨。
SPAN_SUBTITLE_MODES: frozenset[str] = frozenset({"drama"})

#: 项目级单曲的固定 resource_id，与 sdk_tools/enqueue_music.py 同源。
MAIN_MUSIC_TRACK_ID = "main"
#: 主唱人声轨的固定 resource_id，与 sdk_tools/enqueue_singing.py 同源。
MAIN_VOCAL_TRACK_ID = "main"


class NoCompletedSegmentsError(ValueError):
    """本集没有已完成视频片段，与暂存/写入阶段的路径越界守卫错误区分——后者属于安全告警，
    不应被路由层误报成「请先生成视频」的常规空态。"""


def first_existing_audio(project_dir: Path, resource_type: str, resource_id: str) -> Path | None:
    """按候选扩展名找音频产物。

    产物格式随模型而变（ACE-Step 出 .mp3、SoulX-Singer 出 .wav），认死一种会漏掉另一格式的
    文件——表现为「曲子明明生成了，导出的草稿却没有音乐轨」。
    """
    for rel in resource_candidate_paths(resource_type, resource_id):
        found = safe_resolve(project_dir, rel)
        if found is not None:
            return found
    return None


def resolve_music_track(project_dir: Path) -> Path | None:
    """成片音轨：做过歌声合成就用人声轨，否则用作曲产物。

    二者是**替代关系而非叠加**：``generate_singing`` 的输入 ``target_audio`` 就是作曲产物
    ``music/main.wav``，产出是「换了指定歌手音色重唱的同一首完整歌曲」。同时挂两条会变成
    两个人在唱同一首。

    人声轨优先的理由是它更贴近用户意图：用户专门确认过歌手的音色参考、专门跑了一次歌声
    合成，成片里却听到作曲引擎自带的嗓子——而且这个错误在 ArcReel 内部完全看不出来（分镜、
    视频、字幕都对，只有音轨是另一个人），要到导入剪辑器试听才发现。

    演唱镜的口型也是按人声轨驱动的（见 ``generation_tasks._resolve_lip_sync_source``），
    用作曲产物当音轨会让画面在对口型、声音却是另一个人。

    但人声轨比作曲产物旧时反过来退回作曲产物：那说明用户重新作曲后没重跑歌声合成，人声轨
    唱的是上一版曲子。此时两个选择都不完美，取作曲产物是因为它与当前歌词、字幕、分镜一致，
    且「嗓子不是我选的那个」用户一听就能发现，从而回去补跑歌声合成；继续用旧人声轨则是把
    一首完全过时的歌配进成片，听起来一切正常。
    """
    music = first_existing_audio(project_dir, "music", MAIN_MUSIC_TRACK_ID)
    vocal = first_existing_audio(project_dir, "singing", MAIN_VOCAL_TRACK_ID)
    if vocal is None:
        return music
    if music is not None and is_outdated_by(vocal, music):
        logger.warning(
            "人声轨 %s 比作曲产物 %s 旧，成片改用作曲产物；要用歌手音色请重跑 generate_singing",
            vocal.name,
            music.name,
        )
        return music
    return vocal


def has_subtitle_track(content_mode: str) -> bool:
    """该内容模式是否注册为字幕模式（生成字幕轨）。

    单字段模式（narration / ad）与 span 派生模式（drama）都为真；未注册的未知脏值为假。
    """
    return content_mode in _SUBTITLE_TEXT_FIELDS or content_mode in SPAN_SUBTITLE_MODES


def utterance_subtitle_spans(utterances: object, language: str | None) -> list[dict[str, Any]]:
    """从 drama 场景的有序 utterances 派生 subtitle_spans。

    台词（dialogue）与画外音（voiceover）一并成字幕、按 utterances 真实先后排列；每条时长
    按语速估算（``estimate_spoken_seconds``，单一真相源），顺次摆放、offset 累加。空 / 纯空白
    text、非 dict 条目、估时长为 0 的条目跳过且不占 offset——既不产退化字幕，也不留空位。
    不依场景时长拉伸：估算总时长可短于场景，余下自然留白、不撑满场景。
    """
    spans: list[dict[str, Any]] = []
    offset = 0.0
    for utterance in utterances if isinstance(utterances, list) else []:
        if not isinstance(utterance, dict):
            continue
        text = utterance.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        duration = estimate_spoken_seconds(text, language)
        if duration <= 0:
            continue
        spans.append({"offset_seconds": offset, "duration_seconds": duration, "text": text})
        offset += duration
    return spans


def script_content_mode(script: dict) -> str:
    """读取剧本 content_mode 供内容-行为分派（字幕轨 / 草稿命名）；非字符串脏值归一为空串。

    归一后基于成员判定的字幕轨分派（``_SUBTITLE_TEXT_FIELDS`` / ``SPAN_SUBTITLE_MODES``）
    不会因不可哈希的脏值抛 TypeError；脏值不挂字幕轨，与历史一致。骨架分派另走
    ``resolve_declared_kind``（读剧本原值、缺失/未知 fail-loud），不经此归一。
    """
    value = script.get("content_mode", "narration")
    return value if isinstance(value, str) else ""


def collect_video_clips(
    script: dict,
    project_dir: Path,
    *,
    generation_mode: str | None = None,
    language: str | None = None,
) -> list[dict[str, Any]]:
    """从剧本中提取已完成视频的片段列表

    分镜列表按 ``resolve_declared_kind`` 定内容骨架（narration→segments、drama→scenes、
    ad/mv→shots、narration/drama + reference_video→video_units；缺失/未知 content_mode
    fail-loud，不静默兜底）；字幕文案按 ``_SUBTITLE_TEXT_FIELDS`` 取各模式的文案源字段，
    归一到 ``subtitle_text``。drama 改走 span 派生：从场景级有序 ``utterances`` 按语速估算出
    ``subtitle_spans``（``language`` 决定语速，由调用方按项目 ``source_language`` 传入），
    整段 ``subtitle_text`` 留空。

    ``generation_mode`` 须由调用方按 project.json 解析传入（``effective_mode``）：两条参考直出
    路径都靠它定位成片。ad 剧本不打 generation_mode 戳，narration/drama 虽有戳但切回 storyboard
    后戳与残留索引都不该抢走收集，故一律以项目配置为准。

    参考直出成片是 unit 级视频，两条路径的 unit 形状不同，故分两处收集：ad 的 unit 只存
    ``shot_ids`` 索引、内容仍在 shots 里，字幕要按成员镜头水合（``collect_ad_reference_unit_clips``）；
    narration/drama 的 ``video_units`` 自带内容，与分镜条目同形，直接走下面的通用循环。
    后者字幕恒为空——``ReferenceVideoUnit`` 全程没有口播文本字段（``Shot.text`` 是给生成模型的
    画面描述，不是台词），拿它当字幕会把画面描述打到成片上。

    片段字典与导出格式无关：``video_clip`` 是项目内相对路径（引用式导出用它拼 URL），
    ``abs_path`` 是绝对路径（打包式导出用它取字节），两者都给，各取所需。
    """
    content_mode = script_content_mode(script)
    if content_mode == "ad" and generation_mode == "reference_video":
        return collect_ad_reference_unit_clips(script, project_dir)
    # 内容骨架经规范解析定分镜数组：content_mode 取剧本原值（缺失/未知即 fail-loud，不静默
    # 兜底到 drama），generation_mode 一并传入——narration/drama 走参考直出时成片挂在
    # video_units 下，漏传就会回落到 segments/scenes 取到空列表，表现是「视频明明生成好了，
    # 导出却报请先生成视频」。
    kind = resolve_declared_kind(script.get("content_mode"), generation_mode)
    items = script.get(kind, [])
    id_field = SKELETONS[kind].id_field
    subtitle_field = _SUBTITLE_TEXT_FIELDS.get(content_mode)
    is_drama = content_mode == "drama"

    clips = []
    for item in items:
        assets = get_generated_assets(item)
        video_clip = assets.get("video_clip")
        if not video_clip:
            continue

        abs_path = safe_resolve(project_dir, video_clip)
        if abs_path is None:
            logger.warning("video_clip 不可用（越界或文件不存在），已跳过: %s", video_clip)
            continue

        # 字幕文案只接受字符串：手编剧本写入数字/列表等脏值时按缺失处理，
        # 不让单镜头脏数据把整次导出带崩（TextSegment 对非 str 序列化即抛错）
        subtitle_value = item.get(subtitle_field) if subtitle_field else None
        start_value = item.get("start_seconds")

        clip: dict[str, Any] = {
            "id": item.get(id_field, ""),
            "duration_seconds": item.get("duration_seconds", 8),
            # 绝对入点：MV 的镜头钉在歌曲时间轴上（MVShot.start_seconds），不能顺次累加——
            # 生成时长按供应商档位取整，偏离规划值是常态，累加排布会让后面整条错位，
            # 而演唱镜的口型是按绝对歌曲位置切的驱动音频生成的，一漂移就对不上音乐。
            # 其余骨架不声明该字段，取 None 表示「按累加排」。
            "start_seconds": start_value if isinstance(start_value, (int, float)) else None,
            "video_clip": video_clip,
            "abs_path": abs_path,
            "subtitle_text": subtitle_value if isinstance(subtitle_value, str) else "",
            "transition_to_next": item.get("transition_to_next", "cut"),
            "narration_audio_abs": safe_resolve(project_dir, assets.get("narration_audio")),
        }
        # drama：从场景 utterances 派生有序字幕 span（台词 + 画外音按真实先后，按语速估时长）
        if is_drama:
            clip["subtitle_spans"] = utterance_subtitle_spans(item.get("utterances"), language)
        clips.append(clip)

    return clips


def collect_ad_reference_unit_clips(script: dict, project_dir: Path) -> list[dict[str, Any]]:
    """ad 参考直出的 unit 级片段收集：字幕按成员镜头口播在 unit 内逐镜头对齐。

    成员镜头从 shots（内容唯一真相）按 shot_ids 水合：字幕 span 的偏移/时长取
    规划时长（与生成请求一致）；unit 间转场取末位成员镜头的 ``transition_to_next``。
    悬空 shot_id（索引过期）按缺失成员跳过其字幕，不阻断导出。
    """
    shots_by_id = ad_shots_by_id(script)

    clips: list[dict[str, Any]] = []
    units = script.get("reference_units")
    for unit in units if isinstance(units, list) else []:
        if not isinstance(unit, dict):
            continue
        video_clip = get_generated_assets(unit).get("video_clip")
        if not video_clip:
            continue
        abs_path = safe_resolve(project_dir, video_clip)
        if abs_path is None:
            logger.warning("video_clip 不可用（越界或文件不存在），已跳过: %s", video_clip)
            continue

        spans: list[dict[str, Any]] = []
        offset = 0
        transition = "cut"
        member_shots = [shots_by_id.get(sid) for sid in unit.get("shot_ids") or []]
        for shot in member_shots:
            if shot is None:
                continue
            duration = ad_shot_duration_seconds(shot)
            text = shot.get("voiceover_text")
            if isinstance(text, str) and text and duration > 0:
                spans.append({"offset_seconds": offset, "duration_seconds": duration, "text": text})
            offset += max(duration, 0)
            transition = shot.get("transition_to_next", "cut")

        clips.append(
            {
                "id": unit.get("unit_id", ""),
                "duration_seconds": offset,
                # 参考直出的 unit 不钉绝对位置，顺次排布
                "start_seconds": None,
                "video_clip": video_clip,
                "abs_path": abs_path,
                "subtitle_text": "",
                "subtitle_spans": spans,
                "transition_to_next": transition,
                "narration_audio_abs": None,
            }
        )
    return clips


def resolve_canvas_size(project: dict, first_video_path: Path | None = None) -> tuple[int, int]:
    """根据项目 aspect_ratio 确定画布尺寸，缺失时从首个视频自动检测。

    自动检测走 pyJianYingDraft 的 VideoMaterial（探测容器元数据），故在函数内 import——
    引用式导出（ChatCut 交接包）在 aspect_ratio 齐全时用不到它，不该为此拖上剪映依赖。
    """
    ar = project.get("aspect_ratio")
    aspect = ar if isinstance(ar, str) else (ar.get("video") if isinstance(ar, dict) else None)
    if aspect is None and first_video_path is not None:
        from pyJianYingDraft import VideoMaterial

        mat = VideoMaterial(str(first_video_path))
        aspect = "9:16" if mat.height > mat.width else "16:9"
    if aspect == "9:16":
        return 1080, 1920
    return 1920, 1080
