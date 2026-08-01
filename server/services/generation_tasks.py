"""
Task execution service for queued generation jobs.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from lib.asset_types import ASSET_SPECS
from lib.audio_backends import MusicGenerationRequest, SingingSynthesisRequest
from lib.audio_utils import slice_audio_window
from lib.config.registry import PROVIDER_REGISTRY
from lib.config.resolver import constrain_durations
from lib.db.base import DEFAULT_USER_ID
from lib.ledger import Ledger
from lib.lip_sync import item_is_lip_sync
from lib.path_safety import PathTraversalError, safe_exists, safe_join, try_safe_join
from lib.project_change_hints import emit_project_change_batch, project_change_source
from lib.project_manager import get_project_manager
from lib.prompt_builders import (
    append_product_fidelity_tail,
    build_character_prompt,
    build_product_prompt,
    build_prop_prompt,
    build_scene_prompt,
)
from lib.prompt_utils import (
    image_prompt_to_yaml,
    is_structured_image_prompt,
    is_structured_video_prompt,
    utterances_to_dialogue,
    video_prompt_to_yaml,
)
from lib.providers import CALL_TYPE_MUSIC
from lib.resource_paths import END_FRAME_RESOURCE_TYPE, resource_relative_path
from lib.script_models import get_generated_assets, voiceover_text_field
from lib.script_skeleton import SKELETON_ENTITY_TYPES, SKELETON_ITEM_NOUNS, resolve_script_kind
from lib.storyboard_sequence import (
    build_previous_storyboard_reference,
    find_storyboard_item,
    get_storyboard_items,
    group_scenes_by_segment_break,
    resolve_previous_storyboard_path,
    resolve_storyboard_image_ref,
)
from lib.thumbnail import extract_video_thumbnail
from lib.video_backends.base import VideoCapabilityError
from server.services.generation_context import (
    AudioLaneRequest,
    ImageLaneRequest,
    MusicLaneRequest,
    VideoLaneRequest,
    resolve_generation_context,
)

logger = logging.getLogger(__name__)


def get_aspect_ratio(project: dict, resource_type: str) -> str:
    if resource_type == "characters":
        # 角色采用四视图横版
        return "16:9"
    if resource_type in ("scenes", "props", "products"):
        # 多视图横排版式（product sheet 同为多角度横版）
        return "16:9"
    # 优先读顶层字段；缺失时按 content_mode 推导（向后兼容）
    val = project.get("aspect_ratio")
    if isinstance(val, str):
        return val
    if isinstance(val, dict) and resource_type in val:
        return val[resource_type]
    # narration/ad 默认竖屏，drama（含未知值的历史兜底）默认横屏
    return "9:16" if project.get("content_mode", "narration") in {"narration", "ad"} else "16:9"


def _normalize_storyboard_prompt(prompt: str | dict, style: str) -> str:
    """归一化分镜图 prompt 并在末尾追加统一文本化的反向提示词。"""
    from lib.prompt_builders import append_image_negative_tail

    if isinstance(prompt, str):
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        return append_image_negative_tail(prompt)

    if not isinstance(prompt, dict):
        raise ValueError("prompt must be a string or object")

    if not is_structured_image_prompt(prompt):
        raise ValueError("prompt must be a string or include scene/composition")

    scene_text = str(prompt.get("scene", "")).strip()
    if not scene_text:
        raise ValueError("prompt.scene must not be empty")

    composition_raw = prompt.get("composition")
    composition: dict = composition_raw if isinstance(composition_raw, dict) else {}
    normalized_prompt = {
        "scene": scene_text,
        "composition": {
            "shot_type": str(composition.get("shot_type") or "Medium Shot"),
            "lighting": str(composition.get("lighting", "") or ""),
            "ambiance": str(composition.get("ambiance", "") or ""),
        },
    }
    return append_image_negative_tail(image_prompt_to_yaml(normalized_prompt, style))


def _normalize_video_prompt(prompt: str | dict) -> str:
    """归一化视频 prompt 并在末尾追加统一文本化的反向提示词。"""
    from lib.prompt_builders import append_video_negative_tail

    if isinstance(prompt, str):
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        return append_video_negative_tail(prompt)

    if not isinstance(prompt, dict):
        raise ValueError("prompt must be a string or object")

    if not is_structured_video_prompt(prompt):
        raise ValueError("prompt must be a string or include action/camera_motion")

    action_text = str(prompt.get("action", "")).strip()
    if not action_text:
        raise ValueError("prompt.action must not be empty")

    dialogue = prompt.get("dialogue", [])
    if dialogue is None:
        dialogue = []
    if not isinstance(dialogue, list):
        raise ValueError("prompt.dialogue must be an array")

    normalized_dialogue = []
    for item in dialogue:
        if not isinstance(item, dict):
            continue
        speaker = str(item.get("speaker", "") or "").strip()
        line = str(item.get("line", "") or "").strip()
        if speaker or line:
            normalized_dialogue.append({"speaker": speaker, "line": line})

    normalized_prompt: dict[str, Any] = {
        "action": action_text,
        "camera_motion": str(prompt.get("camera_motion", "") or "") or "Static",
        "ambiance_audio": str(prompt.get("ambiance_audio", "") or ""),
        "dialogue": normalized_dialogue,
    }
    return append_video_negative_tail(video_prompt_to_yaml(normalized_prompt))


def _get_model_default_duration(provider_name: str, model_name: str | None) -> int:
    """从 PROVIDER_REGISTRY 查找模型的 supported_durations[0]，找不到则 fallback 4。"""
    provider_meta = PROVIDER_REGISTRY.get(provider_name)
    if provider_meta and model_name:
        model_info = provider_meta.models.get(model_name)
        if model_info and model_info.supported_durations:
            return model_info.supported_durations[0]
    # 自定义供应商或 registry 中无此模型时 fallback
    return 4


def assert_duration_supported(duration: int | float | str, supported_durations: list[int]) -> None:
    """执行层能力守卫：duration 必须落在已解析 model 的 supported_durations 内。

    这是 `duration ↔ supported_durations` 唯一的权威校验家——provider 在执行时才解析
    （见 ADR-0001），故能力校验只能坐在 provider 解析之后。``supported_durations`` 为空时
    放行（能力不可解析，不更坏：保持既有行为不被本次改动弄坏）。

    duration 可能来自外部配置（payload / project.json），故安全解析字符串 / 浮点：
    可解析为整数秒（如 ``"6"`` / ``6.0``）的归一化后比较；非整数秒（如 ``4.5``）一律
    视为非法而**拒绝**，不做截断式归一化（截断会把本应拒绝的非法值静默修正）。

    校验失败抛 :class:`VideoCapabilityError`（带稳定 code），与 ImageCapabilityError 对称——
    Worker 按 code + params 落 task.error_message，文案由读侧 Translator 渲染。
    """
    if not supported_durations:
        return
    try:
        numeric = float(duration)
    except (TypeError, ValueError):
        raise VideoCapabilityError("video_duration_invalid", duration=duration)
    if not numeric.is_integer():
        raise VideoCapabilityError("video_duration_invalid", duration=duration)
    seconds = int(numeric)
    if seconds not in supported_durations:
        raise VideoCapabilityError(
            "video_duration_not_supported",
            duration=seconds,
            supported=", ".join(str(d) for d in supported_durations),
        )


def assert_reference_images_survived_clamp(
    *,
    original_count: int,
    remaining_count: int,
    provider: str,
    model: str | None,
) -> None:
    """执行层能力守卫：参考直出路径下参考图不得被能力裁剪清空。

    与 :func:`assert_duration_supported` 对称，坐在同一处（provider 解析之后、下发之前）。
    参考直出（``generation_mode=reference_video``）的本体就是资产参考图——模型据此锁定
    角色/产品外观。模型声明 ``max_reference_images=0`` 时裁剪会把参考清空，此时继续下发
    有两种坏结局，且后者更坏：

    - i2v 模型收到无图请求，被远端以 400 拒绝（至少还会报错）；
    - t2v 模型照单全收，能出片，但画面与登记资产毫无关系——参考直出静默降级成文生视频，
      要到看成片才发现产品长得不对，且看不出为什么。

    故在此本地拦下。原本就没有参考图（``original_count == 0``）不属于本守卫范围：那是
    调用方自己的选择，不是能力裁剪造成的，放行以免误伤无参考的合法调用。
    """
    if original_count <= 0 or remaining_count > 0:
        return
    raise VideoCapabilityError("video_reference_mode_unsupported", model=model or provider)


def _collect_sheet_references(
    project: dict,
    project_path: Path,
    items: list[dict],
    *,
    char_field: str | None,
    scene_field: str,
    prop_field: str,
    max_count: int = 0,
) -> tuple[list[dict], set[str]]:
    """Collect character_sheet, scene_sheet and prop_sheet references from scene/segment items.

    Returns (list of ``{"image": Path, "label": 资产名}`` dicts, set of relative
    sheet strings for dedup). If *max_count* > 0 collection stops after that many images.

    label 取 project.json 中的资产名，与 prompt 里的专名严格一致——供支持内联标签的
    后端（如 Gemini）把参考图与 prompt 专名显式绑定，不再依赖文件名推断。

    ``char_field`` 为 ``None`` 表示该骨架无逐条角色名单字段（video_units：角色以
    references 条目形态存在），``item.get(None) or []`` 天然跳过角色 sheet 收集。
    """
    seen: set[str] = set()
    refs: list[dict] = []

    characters = project.get("characters")
    characters = characters if isinstance(characters, dict) else {}
    project_scenes = project.get("scenes")
    project_scenes = project_scenes if isinstance(project_scenes, dict) else {}
    project_props = project.get("props")
    project_props = project_props if isinstance(project_props, dict) else {}

    for item in items:
        for char_name in item.get(char_field) or []:
            if not isinstance(char_name, str):
                continue
            char_data = characters.get(char_name)
            sheet = char_data.get("character_sheet") if isinstance(char_data, dict) else None
            if isinstance(sheet, str) and sheet and sheet not in seen:
                path = project_path / sheet
                if path.exists():
                    refs.append({"image": path, "label": char_name})
                    seen.add(sheet)
        for scene_name in item.get(scene_field) or []:
            if not isinstance(scene_name, str):
                continue
            scene_data = project_scenes.get(scene_name)
            sheet = scene_data.get("scene_sheet") if isinstance(scene_data, dict) else None
            if isinstance(sheet, str) and sheet and sheet not in seen:
                path = project_path / sheet
                if path.exists():
                    refs.append({"image": path, "label": scene_name})
                    seen.add(sheet)
        for prop_name in item.get(prop_field) or []:
            if not isinstance(prop_name, str):
                continue
            prop_data = project_props.get(prop_name)
            sheet = prop_data.get("prop_sheet") if isinstance(prop_data, dict) else None
            if isinstance(sheet, str) and sheet and sheet not in seen:
                path = project_path / sheet
                if path.exists():
                    refs.append({"image": path, "label": prop_name})
                    seen.add(sheet)
        if max_count and len(refs) >= max_count:
            break

    return (refs[:max_count] if max_count else refs), seen


def _collect_reference_images(
    project: dict,
    project_path: Path,
    target_item: dict,
    *,
    char_field: str | None,
    scene_field: str,
    prop_field: str,
    extra_reference_images: list[str] | None = None,
    previous_storyboard_path: Path | None = None,
) -> list[object] | None:
    sheet_refs, _ = _collect_sheet_references(
        project, project_path, [target_item], char_field=char_field, scene_field=scene_field, prop_field=prop_field
    )
    reference_images: list[object] = list(sheet_refs)

    for extra in extra_reference_images or []:
        extra_path = Path(extra)
        if not extra_path.is_absolute():
            extra_path = project_path / extra_path
        if extra_path.exists():
            reference_images.append(extra_path)

    if previous_storyboard_path and previous_storyboard_path.exists():
        reference_images.append(build_previous_storyboard_reference(previous_storyboard_path))

    return reference_images or None


def _collect_shot_product_references(project: dict, project_path: Path, item: dict) -> list[dict]:
    """产品镜头（``products_in_shot`` 非空）的产品参考集，用于分镜图生成。

    每个产品：有 product sheet 时注入集为「sheet 多角度 + 原图压阵」（sheet 在前、
    原图收尾），无 sheet 时原图直注。返回 ``{"image": Path, "label": str, "name": str,
    "kind": "sheet"|"original"}`` 列表——label 供支持内联标签的后端绑定图与产品名，
    name 供高保真指令点名（指令只点名实际注入了参考的产品），kind 供截断时让 sheet
    优先存活；调用方负责把该列表排在其它参考之前（排序绝对优先）。氛围镜头
    （列表为空）返回空列表，零产品图。脏数据（products_in_shot 非列表、products
    非 dict、产品名非字符串、引用不存在的产品）按既有装配口径跳过不抛。
    """
    raw_products_in_shot = item.get("products_in_shot")
    if not isinstance(raw_products_in_shot, (list, tuple)):
        if raw_products_in_shot:
            logger.warning(
                "products_in_shot 类型异常（%s），产品参考注入跳过",
                type(raw_products_in_shot).__name__,
            )
        return []
    return collect_product_references_for_names(project, project_path, raw_products_in_shot)


def collect_product_references_for_names(
    project: dict,
    project_path: Path,
    names: Sequence[str],
) -> list[dict]:
    """按产品名列表收集产品参考集（注入二元规则的装配核心，条目语义见
    ``_collect_shot_product_references``）。分镜图按镜头注入与 ad 参考直出
    按 unit 注入共用此函数，保证两条路径的「sheet 在前、原图压阵」口径一致。
    """
    spec = ASSET_SPECS["product"]
    products = project.get(spec.bucket_key)
    if not isinstance(products, dict):
        products = {}
    references: list[dict] = []
    for name in names:
        if not isinstance(name, str):
            logger.warning("products_in_shot 含非字符串条目 %r，产品参考跳过", name)
            continue
        entry = products.get(name)
        if not isinstance(entry, dict):
            logger.warning("镜头引用的产品 '%s' 不在 project.json products 中，产品参考跳过", name)
            continue
        before = len(references)
        sheet = entry.get(spec.sheet_field)
        if sheet and safe_exists(project_path, sheet):
            references.append(
                {
                    "image": project_path / sheet,
                    "label": f"产品「{name}」标准多角度参考图",
                    "name": name,
                    "kind": "sheet",
                }
            )
        for original in _collect_product_reference_images(project, project_path, name) or []:
            references.append(
                {"image": original, "label": f"产品「{name}」实拍原图（保真锚点）", "name": name, "kind": "original"}
            )
        if len(references) == before:
            logger.warning("产品镜头引用的产品 '%s' 无任何可用参考图（sheet 与原图均缺失），保真注入退化为纯文本", name)
    return references


def _product_names_in_references(product_references: list[dict]) -> list[str]:
    """从产品参考集提取去重保序的产品名——高保真指令只点名实际注入了参考的产品。"""
    return list(dict.fromkeys(ref["name"] for ref in product_references))


def _episode_from_script(script: dict[str, Any] | None) -> int | None:
    if not isinstance(script, dict):
        return None
    episode = script.get("episode")
    if isinstance(episode, int):
        return episode
    return None


def compute_affected_fingerprints(project_name: str, task_type: str, resource_id: str) -> dict[str, int]:
    """计算受影响文件的 mtime 指纹"""
    try:
        project_path = get_project_manager().get_project_path(project_name)
    except Exception:
        return {}

    paths: list[tuple[str, Path]] = []

    if task_type == "storyboard":
        paths.append(
            (
                f"storyboards/scene_{resource_id}.png",
                project_path / "storyboards" / f"scene_{resource_id}.png",
            )
        )
    elif task_type == "video":
        paths.append(
            (
                f"videos/scene_{resource_id}.mp4",
                project_path / "videos" / f"scene_{resource_id}.mp4",
            )
        )
        paths.append(
            (
                f"thumbnails/scene_{resource_id}.jpg",
                project_path / "thumbnails" / f"scene_{resource_id}.jpg",
            )
        )
    elif task_type == "character":
        paths.append(
            (
                f"characters/{resource_id}.png",
                project_path / "characters" / f"{resource_id}.png",
            )
        )
    elif task_type == "scene":
        paths.append(
            (
                f"scenes/{resource_id}.png",
                project_path / "scenes" / f"{resource_id}.png",
            )
        )
    elif task_type == "prop":
        paths.append(
            (
                f"props/{resource_id}.png",
                project_path / "props" / f"{resource_id}.png",
            )
        )
    elif task_type == "product":
        paths.append(
            (
                f"products/{resource_id}.png",
                project_path / "products" / f"{resource_id}.png",
            )
        )
    elif task_type == "grid":
        paths.append(
            (
                f"grids/{resource_id}.png",
                project_path / "grids" / f"{resource_id}.png",
            )
        )
        # 宫格切割还会覆写多个 canonical 分镜图，实际写入的 cell 路径持久化在
        # grid 记录的 frame_chain 中，一并纳入指纹让前端对这些文件 cache-bust；
        # 记录缺失/损坏时降级为只报宫格主图。
        try:
            from lib.grid_manager import GridManager

            grid = GridManager(project_path).get(resource_id)
        except Exception:
            grid = None
        if grid is not None:
            # 记录是磁盘上的 JSON，image_path 不可直接信任：绝对路径会覆盖左操作数、
            # ../ 会越出项目目录，把任意服务器文件的存在性/mtime 暴露给前端
            project_root = project_path.resolve()
            for frame in grid.frame_chain:
                if not frame.image_path:
                    continue
                candidate = try_safe_join(project_root, frame.image_path)
                if candidate is None:
                    logger.warning("跳过越出项目目录的宫格 cell 路径: %s", frame.image_path)
                    continue
                # 指纹 key 用归一化后的项目相对路径：原始字符串若是项目内的
                # 绝对路径，会把服务器路径泄漏给前端且匹配不上前端的资源 key
                rel = candidate.relative_to(project_root).as_posix()
                paths.append((rel, candidate))
    elif task_type == "reference_video":
        paths.append(
            (
                f"reference_videos/{resource_id}.mp4",
                project_path / "reference_videos" / f"{resource_id}.mp4",
            )
        )
        paths.append(
            (
                f"reference_videos/thumbnails/{resource_id}.jpg",
                project_path / "reference_videos" / "thumbnails" / f"{resource_id}.jpg",
            )
        )
    elif task_type == "tts":
        audio_rel = resource_relative_path("audio", resource_id)
        paths.append((audio_rel, project_path / audio_rel))
    elif task_type in ("music", "singing"):
        # 重复生成覆写同一路径（一支 MV 一首曲子 / 一条主唱轨）：不进指纹表的话，
        # 前端播放器会一直放缓存里的旧音频，用户听不出「重生成到底有没有生效」。
        media_rel = resource_relative_path(task_type, resource_id)
        paths.append((media_rel, project_path / media_rel))

    result: dict[str, int] = {}
    for rel, abs_path in paths:
        if abs_path.exists():
            result[rel] = abs_path.stat().st_mtime_ns

    return result


# (entity_type, action, label_tpl, include_script_episode)
# 三类项目级资产（character / scene / prop）的 spec 由 lib.asset_types.ASSET_SPECS 派生。
# storyboard / video / reference_video 不在此表——三者按剧本骨架种类（segments/scenes/shots/
# video_units）动态派生 entity_type 与条目名词，见 _SKELETON_DRIVEN_TASK_ACTIONS，避免恒发
# ``segment``/「分镜」而与分镜级事件（project_events.py）名词不一致。
_TASK_CHANGE_SPECS: dict[str, tuple] = {
    "tts": ("segment", "tts_ready", "旁白「{}」", True),
    "grid": ("grid", "grid_ready", "宫格「{}」", True),
    # 音乐/歌声是项目级单件产物，不挂在某一集剧本上（include_script_episode=False）。
    "music": ("music", "music_ready", "曲子「{}」", False),
    "singing": ("singing", "singing_ready", "人声轨「{}」", False),
    **{atype: (atype, "updated", f"{spec.label_zh}「{{}}」设计图", False) for atype, spec in ASSET_SPECS.items()},
}

# 骨架驱动的任务类型 → 完成事件 action。entity_type/条目名词按项目剧本当前骨架种类
# （resolve_script_kind，与分镜级事件同一判定）动态解析，不按 task_type 恒定硬编码。
_SKELETON_DRIVEN_TASK_ACTIONS: dict[str, str] = {
    "storyboard": "storyboard_ready",
    "video": "video_ready",
    "reference_video": "reference_video_ready",
}

# reference_video 的条目标签沿用「参考视频」措辞（区别于分镜级事件的骨架名词「视频单元」，
# 两者服务不同场景：此为任务完成通知的条目文案，不随骨架名词收敛）；storyboard/video 未列出，
# 回退到骨架名词本身（分镜/场景/镜头），与同项目分镜级事件同口径。
_SKELETON_TASK_LABEL_NOUNS: dict[str, str] = {
    "reference_video": "参考视频",
}


def _load_event_script(project_name: str, script_file: str | None) -> dict[str, Any] | None:
    """加载完成事件所属剧本一次，供骨架种类与 episode 共用；缺失/损坏时返回 None。

    调用方对 None 各自兜底（骨架种类回退 ``"segments"``、episode 回退 ``None``），
    不让剧本加载失败导致通知发送中断。
    """
    if not script_file:
        return None
    try:
        return get_project_manager().load_script(project_name, script_file)
    except Exception:
        return None


def emit_generation_success_batch(
    *,
    task_type: str,
    project_name: str,
    resource_id: str,
    payload: dict[str, Any],
) -> dict[str, int]:
    """发送生成/上传完成的项目变更事件，返回受影响文件的指纹（调用方可直接复用，免二次计算）。

    事件 source 由 project_change_source contextvar 决定（worker / webui 调用方各自包裹）。
    """
    if task_type == "image_edit":
        # 编辑完成事件与「同一资源的生成完成事件」同形状：按 payload.resource_type 派发到
        # 既有 spec 表（storyboard 走骨架驱动、四类资产走 ASSET_SPECS 派生表），entity/action/
        # 指纹与生成路径一致，前端既有的 SSE fingerprint 刷新零改动即可覆盖编辑完成。
        task_type = str(payload.get("resource_type") or "")

    script_file = str(payload.get("script_file") or "") or None
    # 单次加载剧本，骨架种类与 episode 共用，避免同一 script_file 双解析。
    script = _load_event_script(project_name, script_file)

    action = _SKELETON_DRIVEN_TASK_ACTIONS.get(task_type)
    if action is not None:
        if task_type == "reference_video":
            # ad 剧本骨架恒为 shots[]（reference_video 路径只是把镜头派生分组为
            # video_unit 索引，二者持久于同一份剧本 JSON），resolve_script_kind
            # 的数据形状优先判别会因 shots 键仍在而退回 content_mode==ad→shots，
            # 与该任务实际对应 video_unit 资源不符——直接固定 kind，不经骨架判别。
            kind = "video_units"
        else:
            kind = resolve_script_kind(script) if isinstance(script, dict) else "segments"
        entity_type = SKELETON_ENTITY_TYPES.get(kind, "segment")
        noun = _SKELETON_TASK_LABEL_NOUNS.get(task_type) or SKELETON_ITEM_NOUNS.get(kind, "分镜")
        label_tpl = f"{noun}「{{}}」"
        include_script_episode = True
    else:
        spec = _TASK_CHANGE_SPECS.get(task_type)
        if spec is None:
            return {}
        entity_type, action, label_tpl, include_script_episode = spec

    asset_fingerprints = compute_affected_fingerprints(project_name, task_type, resource_id)

    change: dict[str, Any] = {
        "entity_type": entity_type,
        "action": action,
        "entity_id": resource_id,
        "label": label_tpl.format(resource_id),
        "focus": None,
        "important": True,
        "asset_fingerprints": asset_fingerprints,
    }
    if include_script_episode:
        change["script_file"] = script_file
        change["episode"] = _episode_from_script(script)

    try:
        emit_project_change_batch(project_name, [change])
    except Exception:
        logger.exception(
            "发送生成完成项目事件失败 project=%s task_type=%s resource_id=%s",
            project_name,
            task_type,
            resource_id,
        )
    return asset_fingerprints


async def execute_storyboard_task(
    project_name: str,
    resource_id: str,
    payload: dict[str, Any],
    *,
    user_id: str = DEFAULT_USER_ID,
    task_id: str | None = None,
) -> dict[str, Any]:
    script_file = payload.get("script_file")
    if not script_file:
        raise ValueError("script_file is required for storyboard task")

    prompt = payload.get("prompt")
    if prompt is None:
        raise ValueError("prompt is required for storyboard task")

    def _prepare():
        _project = get_project_manager().load_project(project_name)
        _project_path = get_project_manager().get_project_path(project_name)
        _script = get_project_manager().load_script(project_name, script_file)
        _items, _id_field, _char_field, _scene_field, _prop_field = get_storyboard_items(_script)

        _resolved = find_storyboard_item(_items, _id_field, resource_id)
        if _resolved is None:
            raise ValueError(f"scene/segment not found: {resource_id}")
        _target_item, _ = _resolved

        _prev_path = resolve_previous_storyboard_path(_project_path, _items, _id_field, resource_id)
        _prompt_text = _normalize_storyboard_prompt(prompt, _project.get("style", ""))
        _ref_images = _collect_reference_images(
            _project,
            _project_path,
            _target_item,
            char_field=_char_field,
            scene_field=_scene_field,
            prop_field=_prop_field,
            extra_reference_images=payload.get("extra_reference_images") or [],
            previous_storyboard_path=_prev_path,
        )
        # 产品镜头：产品参考全量注入且排序绝对优先（先于角色/场景/道具 sheet），
        # 并附高保真还原指令；氛围镜头零产品图，既有装配不变。
        _product_refs = _collect_shot_product_references(_project, _project_path, _target_item)
        if _product_refs:
            _ref_images = _product_refs + (_ref_images or [])
            _prompt_text = append_product_fidelity_tail(_prompt_text, _product_names_in_references(_product_refs))
        return _project, _project_path, _prompt_text, _ref_images

    project, project_path, prompt_text, reference_images = await asyncio.to_thread(_prepare)
    _needs_i2i = bool(reference_images)

    ctx = await resolve_generation_context(
        project_name,
        payload,
        project=project,
        user_id=user_id,
        image=ImageLaneRequest(capability="i2i" if _needs_i2i else "t2i"),
    )
    generator = ctx.generator
    aspect_ratio = get_aspect_ratio(project, "storyboards")
    image_size = ctx.image.resolution

    _, version = await generator.generate_image_async(
        prompt=prompt_text,
        resource_type="storyboards",
        resource_id=resource_id,
        reference_images=reference_images,
        aspect_ratio=aspect_ratio,
        image_size=image_size,
    )

    def _finalize():
        get_project_manager().update_scene_asset(
            project_name=project_name,
            script_filename=script_file,
            scene_id=resource_id,
            asset_type="storyboard_image",
            asset_path=f"storyboards/scene_{resource_id}.png",
        )
        return generator.versions.get_versions("storyboards", resource_id)["versions"][-1]["created_at"]

    created_at = await asyncio.to_thread(_finalize)

    return {
        "version": version,
        "file_path": f"storyboards/scene_{resource_id}.png",
        "created_at": created_at,
        "resource_type": "storyboards",
        "resource_id": resource_id,
    }


async def execute_tts_task(
    project_name: str,
    resource_id: str,
    payload: dict[str, Any],
    *,
    user_id: str = DEFAULT_USER_ID,
    task_id: str | None = None,
) -> dict[str, Any]:
    """为单个 segment / 镜头合成旁白音频（同步 TTS，无续传）。

    文本来源：payload.text 显式优先；否则按剧本的 content_mode 从对应字段读取
    （narration 取 novel_text、ad 取 voiceover_text，见 ``VOICEOVER_TEXT_FIELDS``）。
    字段名不写死在这里——字幕导出读的是同一份文案，两处各写一份会漂移成「字幕有词、
    配音没声」。文本为空 / segment 找不到 / 模式不支持整段朗读一律显式 raise，绝不把
    空串送给 backend 合成。

    该表同时是 TTS 的**准入判定**：查不到字段名即拒绝。mv 有意不在表内——歌词要唱不要念，
    人声走 ``generate_singing``；把它登记进来等于给 MV 开了配音的门，产物是被念出来的歌词。
    """
    script_file = payload.get("script_file")

    def _prepare() -> tuple[dict, str]:
        _project = get_project_manager().load_project(project_name)
        _text = payload.get("text") or payload.get("prompt")
        _field = "text"
        if not _text:
            if not script_file:
                raise ValueError("tts task 需要 payload.text 或 payload.script_file 之一")
            _script = get_project_manager().load_script(project_name, script_file)
            _mode = _script.get("content_mode") or _project.get("content_mode")
            _field = voiceover_text_field(_mode) or ""
            if not _field:
                raise ValueError(f"content_mode={_mode!r} 没有可直接朗读的整段口播文案，不支持 TTS")
            _items, _id_field, *_ = get_storyboard_items(_script)
            _resolved = find_storyboard_item(_items, _id_field, resource_id)
            if _resolved is None:
                raise ValueError(f"segment not found: {resource_id}")
            _segment, _ = _resolved
            _text = _segment.get(_field)
        if not isinstance(_text, str) or not _text.strip():
            raise ValueError(f"segment {resource_id} 无可合成的旁白文本（{_field} 为空）")
        return _project, _text.strip()

    project, text = await asyncio.to_thread(_prepare)

    ctx = await resolve_generation_context(
        project_name,
        payload,
        project=project,
        user_id=user_id,
        audio=AudioLaneRequest(),
    )
    generator = ctx.generator
    voice = ctx.audio.narration_voice
    speed = ctx.audio.narration_speed

    _, version = await generator.generate_audio_async(
        text=text,
        resource_id=resource_id,
        voice=voice,
        speed=speed,
    )

    audio_rel = resource_relative_path("audio", resource_id)

    def _finalize():
        if script_file:
            get_project_manager().update_scene_asset(
                project_name=project_name,
                script_filename=script_file,
                scene_id=resource_id,
                asset_type="narration_audio",
                asset_path=audio_rel,
            )
        return generator.versions.get_versions("audio", resource_id)["versions"][-1]["created_at"]

    created_at = await asyncio.to_thread(_finalize)

    return {
        "version": version,
        "file_path": audio_rel,
        "created_at": created_at,
        "resource_type": "audio",
        "resource_id": resource_id,
    }


def _ledger() -> Ledger:
    """音乐 / 歌声执行器共用的记账入口。

    这两条路径不经 MediaGenerator（音乐是项目级单件产物，版本管理与批量语义对它没有价值，
    见 MusicLaneResult），但记账不能因此缺席——生成路径少一条记账就是费用页少一块，且
    「用量对不上」这种症状最难定位到具体是哪条路径漏的。
    """
    return Ledger()


def _require_audio_capability(backend: object, method: str, model: str, *, task_label: str, hint: str) -> None:
    """断言解析出的音频后端确实具备该任务需要的能力。

    ``media_type="audio"`` 底下装着三种互不兼容的协议（TTS 只有 ``synthesize``、作曲只有
    ``generate_music``、歌声只有 ``synthesize_singing``）。设置页的选项已按 endpoint 声明的能力
    分列，但项目级覆盖、历史配置、直接改库都能绕过它——落到这里只剩一个没有目标方法的对象，
    不拦就是 ``AttributeError``，错误信息里完全看不出「配错了模型」这回事。
    """
    if not hasattr(backend, method):
        raise ValueError(f"模型 {model} 不具备{task_label}能力：{hint}")


async def execute_music_task(
    project_name: str,
    resource_id: str,
    payload: dict[str, Any],
    *,
    user_id: str = DEFAULT_USER_ID,
    task_id: str | None = None,
) -> dict[str, Any]:
    """生成一首曲子并落到项目的 ``music/`` 目录（同步等待，无续传）。

    与 TTS 的两点不同：

    - **产物是项目级单件**（一支片子一首曲），不挂在某个分镜下，故不写 scene asset、
      也不走 VersionManager 的分镜版本链；写回由调用方（MV 剧本的 song 字段）负责。
    - **文本是风格描述而非台词**：``payload.prompt`` 必填，空 prompt 会让引擎自由发挥，
      产出与项目无关的曲子且照常计费，故显式拒绝。
    """
    prompt = payload.get("prompt") or payload.get("text")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("music task 需要 payload.prompt（曲风描述）")

    project = await asyncio.to_thread(get_project_manager().load_project, project_name)
    ctx = await resolve_generation_context(
        project_name,
        payload,
        project=project,
        user_id=user_id,
        music=MusicLaneRequest(),
    )

    music_rel = resource_relative_path("music", resource_id)
    project_path = await asyncio.to_thread(get_project_manager().get_project_path, project_name)
    output_path = project_path / music_rel

    duration = payload.get("duration_seconds")
    lyrics = payload.get("lyrics")
    bpm = payload.get("bpm")
    language = payload.get("vocal_language")
    request = MusicGenerationRequest(
        prompt=prompt.strip(),
        output_path=output_path,
        duration_seconds=int(duration) if isinstance(duration, (int, float)) and duration > 0 else None,
        lyrics=lyrics if isinstance(lyrics, str) and lyrics.strip() else None,
        bpm=int(bpm) if isinstance(bpm, (int, float)) and bpm > 0 else None,
        vocal_language=language if isinstance(language, str) and language.strip() else None,
    )

    backend = ctx.music.backend
    _require_audio_capability(
        backend,
        "generate_music",
        ctx.music.backend_model,
        task_label="作曲",
        hint="请在设置页把作曲模型配成 ACE-Step 一类的 t2m 模型（旁白 TTS 模型不会作曲）",
    )
    # 记账括号：与图片 / 视频 / TTS 同一套（进入落 pending、成功递交 backend 结果对象、
    # 异常自动翻 failed）。不经 MediaGenerator 但同样要记——漏了的表现是曲子生成成功、
    # 费用页一行都没有，用量对不上却查不到是哪条路径漏的。
    async with _ledger().record(
        project_name=project_name,
        call_type=CALL_TYPE_MUSIC,
        model=ctx.music.backend_model,
        prompt=request.prompt,
        provider=ctx.music.provider_model.provider_id,
        user_id=user_id,
        output_path=str(output_path),
    ) as call:
        result = await backend.generate_music(request)  # type: ignore[attr-defined]
        call.success(result)

    return {
        "file_path": music_rel,
        "resource_type": "music",
        "resource_id": resource_id,
        "duration_seconds": result.duration_seconds,
        "provider": result.provider,
        "model": result.model,
    }


#: MV 主唱人声轨的 resource_id，与 sdk_tools/enqueue_singing.py 同源。
_MV_MAIN_VOCAL_ID = "main"


async def execute_singing_task(
    project_name: str,
    resource_id: str,
    payload: dict[str, Any],
    *,
    user_id: str = DEFAULT_USER_ID,
    task_id: str | None = None,
) -> dict[str, Any]:
    """歌声合成：音色参考 + 目标曲 → 人声轨（同步等待，无续传）。

    两个音频输入都是项目内相对路径，由调用方给出：
    - ``voice_reference``：角色的 reference_audio（谁来唱）
    - ``target_song``：作曲产物 music/main.wav（唱什么旋律）

    缺任一方直接拒绝——引擎收到不全的输入会产出一段无关音频并照常计费。
    """
    voice_ref = payload.get("voice_reference")
    target = payload.get("target_song")
    if not isinstance(voice_ref, str) or not voice_ref.strip():
        raise ValueError("singing task 需要 payload.voice_reference（音色参考音频的项目内相对路径）")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("singing task 需要 payload.target_song（目标曲/伴奏的项目内相对路径）")

    project = await asyncio.to_thread(get_project_manager().load_project, project_name)
    project_path = await asyncio.to_thread(get_project_manager().get_project_path, project_name)

    voice_abs = safe_join(project_path, voice_ref, require_file=True)
    target_abs = safe_join(project_path, target, require_file=True)

    ctx = await resolve_generation_context(
        project_name,
        payload,
        project=project,
        user_id=user_id,
        # singing lane 解析的是歌声模型（default_singing_backend），不是作曲模型——
        # 两者是不同模型，混用会把 svs 请求发给只会作曲的 ACE-Step。
        music=MusicLaneRequest(task_type="singing"),
    )
    backend = ctx.music.backend
    _require_audio_capability(
        backend,
        "synthesize_singing",
        ctx.music.backend_model,
        task_label="歌声合成",
        hint="请在设置页把歌声模型配成 SoulX-Singer 一类的 svs 模型（作曲模型只会作曲、不会唱）",
    )

    singing_rel = resource_relative_path("singing", resource_id)
    singing_path = project_path / singing_rel
    # 与作曲同一记账通道：都按产出时长计价，只是 task_type 不同。
    async with _ledger().record(
        project_name=project_name,
        call_type=CALL_TYPE_MUSIC,
        model=ctx.music.backend_model,
        # svs 没有文本 prompt（唱什么由目标曲决定），记账 prompt 落音色 + 目标曲的引用，
        # 让费用页能看出这一次唱的是哪首。
        prompt=f"svs voice={voice_ref} target={target}",
        provider=ctx.music.provider_model.provider_id,
        user_id=user_id,
        output_path=str(singing_path),
    ) as call:
        result = await backend.synthesize_singing(  # type: ignore[attr-defined]
            SingingSynthesisRequest(
                voice_reference=voice_abs,
                target_song=target_abs,
                output_path=singing_path,
            )
        )
        call.success(result)

    return {
        "file_path": singing_rel,
        "resource_type": "singing",
        "resource_id": resource_id,
        "duration_seconds": result.duration_seconds,
        "provider": result.provider,
        "model": result.model,
    }


def _resolve_lip_sync_source(project: dict, project_path: Path, item: object) -> Path | None:
    """MV 演唱镜头的整支歌声轨；其余情况返回 None（走常规图生视频）。

    只有 ``content_mode=mv`` 且镜头 ``is_performance=true`` 才需要——歌声轨驱动人物口型。
    非演唱镜头（氛围镜、空镜）传了驱动音频反而会让画面主体被强行对口型。

    歌声轨缺失时**显式抛错而非静默降级**：降级的后果是演唱镜头照常出片、口型却对不上，
    要到看成片才发现，且看不出原因。缺就报，让用户先跑 generate_singing。

    返回的是整轨，尚不能直接送去驱动口型——按镜头时间窗切分见 ``_slice_lip_sync_window``。
    """
    if not item_is_lip_sync(project, item):
        return None

    vocal_rel = resource_relative_path("singing", _MV_MAIN_VOCAL_ID)
    try:
        return safe_join(project_path, vocal_rel, require_file=True)
    except (PathTraversalError, FileNotFoundError) as exc:
        raise ValueError(
            f"演唱镜头需要歌声轨（{vocal_rel}）作口型驱动，但它不可用：请先用 generate_singing 合成歌声"
        ) from exc


async def _slice_lip_sync_window(source: Path, item: object, output: Path) -> Path:
    """把整支歌声轨切到当前镜头对应的时间窗，作为该镜的驱动音频。

    MV 镜头钉在歌曲的**绝对时间轴**上（``start_seconds``），而 s2v 是从音频第 0 秒开始驱动
    口型的。整轨直接送进去，等于让第 40 秒那一镜的演员去对唱歌曲开头的词——除了 ``start_seconds``
    恰为 0 的第一镜，其余演唱镜全部口型错位，且成片能听能看、只是对不上，最难排查。

    时间窗读自剧本而非 payload：payload 的 ``duration_seconds`` 会被供应商档位收窄
    （``assert_duration_supported`` 之前还可能回落到项目默认），而口型要对齐的是剧本排好的
    那一段歌词。窗口比实际出片时长略长无害（多余部分不会被用到），错位才致命。
    """
    if not isinstance(item, dict):
        raise ValueError("演唱镜头缺少剧本条目，无法确定驱动音频的时间窗")
    start = item.get("start_seconds")
    duration = item.get("duration_seconds")
    if not isinstance(start, (int, float)) or isinstance(start, bool):
        raise ValueError(f"演唱镜头的 start_seconds 无效: {start!r}；它是口型对齐的入点，必须是数字")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0:
        raise ValueError(f"演唱镜头的 duration_seconds 无效: {duration!r}；必须是正数")
    return await slice_audio_window(source, output, start_seconds=float(start), duration_seconds=float(duration))


async def execute_video_task(
    project_name: str,
    resource_id: str,
    payload: dict[str, Any],
    *,
    user_id: str = DEFAULT_USER_ID,
    task_id: str | None = None,
) -> dict[str, Any]:
    script_file = payload.get("script_file")
    if not script_file:
        raise ValueError("script_file is required for video task")

    prompt = payload.get("prompt")
    if prompt is None:
        raise ValueError("prompt is required for video task")

    def _load():
        _pm = get_project_manager()
        _project = _pm.load_project(project_name)
        _project_path = _pm.get_project_path(project_name)
        _script = _pm.load_script(project_name, script_file)
        _items, _id_field, _, _, _ = get_storyboard_items(_script)
        _resolved = find_storyboard_item(_items, _id_field, resource_id)
        _item = _resolved[0] if _resolved else {}
        return _project, _project_path, _item

    project, project_path, item = await asyncio.to_thread(_load)
    # MV 演唱镜头：以歌声轨作驱动音频走口型驱动（s2v）。非演唱镜头与其余内容模式不受影响。
    # 这里只定位整轨（并对缺失 fail-loud），按镜头时间窗的切分推迟到真正要发请求时做。
    lip_sync_source = _resolve_lip_sync_source(project, project_path, item)
    ctx = await resolve_generation_context(
        project_name,
        payload,
        project=project,
        user_id=user_id,
        # 演唱镜头解析口型驱动模型，其余镜头走项目配置的常规视频模型——同一支 MV 里
        # 两类镜头用不同模型，这是 MV 与其余内容模式的结构性差异。
        video=VideoLaneRequest(lip_sync=lip_sync_source is not None),
    )
    generator = ctx.generator

    # 优先读取 generated_assets.storyboard_image，回退默认路径。校验口径见
    # resolve_storyboard_image_ref：与路由入队预检、SDK 工具入队预检共用同一份。
    storyboard_rel = get_generated_assets(item).get("storyboard_image")
    storyboard_file = resolve_storyboard_image_ref(project_path, storyboard_rel)
    if storyboard_file is None:
        storyboard_file = project_path / "storyboards" / f"scene_{resource_id}.png"
    # is_file 而非 exists：字段被外部编辑指向目录（如 "storyboards" 本身）时 exists() 仍为
    # True，目录会被当作 start_image 传给视频后端，在编码阶段才失败且原因不可读。
    if not storyboard_file.is_file():
        raise ValueError(f"storyboard not found: {storyboard_file.name}")

    # drama 口型台词单一真相源在场景级有序 utterances：从 dialogue-kind 条目取台词注入 video YAML
    # 的 dialogue 出口（覆盖 payload 里 drama 已不再携带的 video_prompt.dialogue）。narration / ad
    # 的 item 无 utterances 字段，payload.dialogue 原样透传；SDK 路径 prompt 已是渲染好的字符串、跳过。
    if isinstance(item, dict) and "utterances" in item and isinstance(prompt, dict):
        prompt = {**prompt, "dialogue": utterances_to_dialogue(item.get("utterances"))}

    prompt_text = _normalize_video_prompt(prompt)
    aspect_ratio = get_aspect_ratio(project, "videos")
    seed = payload.get("seed")
    service_tier = payload.get("video_provider_settings", {}).get("service_tier", "default")

    # provider / model / 能力 / 分辨率均取自单次解析的 video lane：能力按 backend 实际身份
    # （registry provider_id + backend.model）查询，与实际要调用的 model 对齐——历史任务 payload
    # 携带 provider 覆盖、或自定义供应商目标 model 被禁用回退时，二者一致避免 duration 守卫误判
    # （用「项目默认 model 的能力」误判「实际调用的 model」）。能力不可解析时 supported_durations
    # 留空，守卫遇空列表放行（不更坏，见 ADR-0002）。解析/构造失败已在 resolve_generation_context
    # 内原样上抛整次任务失败，不再有硬编码 provider/model 静默兜底。
    registry_provider_id = ctx.video.provider_model.provider_id
    model_name = ctx.video.backend_model
    supported_durations: list[int] = list(ctx.video.supported_durations)
    resolution = ctx.video.resolution

    # duration 解析收口于执行层：payload > project.default_duration > caps 默认。
    # 用 ``is not None`` 而非 ``or`` 取 payload 值，避免显式 falsy 值被当作未设置。
    duration_seconds = payload.get("duration_seconds")
    if duration_seconds is None:
        duration_seconds = project.get("default_duration")
    if not duration_seconds:
        # 取首项前先按当前分辨率的联动约束收窄：否则 Veo + 1080p/4k 的默认（Auto）设置会取到
        # 4 秒，被 backend 的「该分辨率必须 8 秒」拒绝——UI 已按同一份声明门控，此处不收窄
        # 就等于默认配置必然失败。显式指定的时长不经此收窄，其合法性由 assert_duration_supported
        # 与 backend 的执行期校验把关。
        candidates = constrain_durations(registry_provider_id, model_name, supported_durations, resolution=resolution)
        duration_seconds = (
            candidates[0] if candidates else _get_model_default_duration(registry_provider_id, model_name)
        )
    # 能力守卫：provider 解析之后的唯一权威家（见 ADR-0001）。安全解析交给守卫，
    # 此处不预先 int() 截断，避免把非整数秒静默修正成「碰巧合法」的值。
    assert_duration_supported(duration_seconds, supported_durations)

    # end_frame_image 是镜头持久属性（见 server/services/end_frame.py），剧本每次加载都带出，
    # 重新生成无需额外操作即可沿用。能力是否支持尾帧由 generate_video_async 内的 plan_frame_slots
    # 按已解析 backend 统一 gating（不支持即 VideoCapabilityError），此处不重复一份判断。
    #
    # 剧本是磁盘上的 JSON，字段值不可直接信任（归档导入、外部编辑、脏数据都能落值）：绝对路径会
    # 覆盖 `/` 的左操作数、`..` 会越出项目目录，把任意服务器文件送进视频请求上传给供应商。只接受
    # 「当前镜头自己的」end_frames/ 快照——不是随便一个存在的 end_frames/ 内文件：字段被外部编辑
    # 指向别的镜头（如 E1S01 引用 E1S02 的快照）会静默生成/扣费错镜头的尾帧，仅凭目录归属挡不住，
    # 须与 resource_relative_path 算出的当前镜头 canonical 路径逐一比对。裸文件名（无路径分隔符）
    # 按校验侧 data_validator._resolve_existing_path 的 default_dir 回退口径补 end_frames/ 前缀
    # 重试，否则通过导入校验的值会在生成期无理由硬失败。
    end_frame_rel = item.get("end_frame_image") if isinstance(item, dict) else None
    end_image: Path | None = None
    # 只把 None / "" 视为「未设置」（与 data_validator 的 _resolve_existing_path 同口径）；
    # 0 / False / [] / {} 等其余 falsy 脏数据必须继续走下面的硬失败，不能被 Python 的真值判断
    # 静默吞成「未设置」进而无声跳过尾帧、照常生成扣费。
    if end_frame_rel not in (None, ""):
        if not isinstance(end_frame_rel, str):
            raise ValueError(f"invalid end frame snapshot path: {end_frame_rel!r}")
        normalized = end_frame_rel.strip().replace("\\", "/")
        candidate = normalized if "/" in normalized else f"{END_FRAME_RESOURCE_TYPE}/{normalized}"
        expected_rel = resource_relative_path(END_FRAME_RESOURCE_TYPE, resource_id)
        end_frame_file = try_safe_join(project_path, candidate)
        expected_file = safe_join(project_path, expected_rel)
        # try_safe_join / safe_join 都走 realpath，会展开符号链接：若字段值恰是当前镜头的
        # canonical 相对路径，但磁盘上那个位置（含 end_frames/ 目录本身等中间组件）被替换成
        # 指向别处（如另一镜头快照、分镜图）的符号链接，两次解析会算出同一个被展开的真实目标，
        # 让下面的相等比较失去意义。这里逐段检查 canonical 路径每个组件——文件名与父目录——
        # 挡住"路径字符串正确但磁盘对象被调包"，不止查最终文件名那一段。Windows 原生环境下
        # 目录联接（junction）是独立于符号链接的 reparse point 类型，`is_symlink()` 识别不到，
        # 须用 `is_junction()`（3.12+，POSIX 上恒为 False）单独检测。
        canonical_path_tampered = False
        current = project_path
        for component in Path(expected_rel).parts:
            current = current / component
            if current.is_symlink() or current.is_junction():
                canonical_path_tampered = True
                break
        if end_frame_file is None or end_frame_file != expected_file or canonical_path_tampered:
            raise ValueError(f"invalid end frame snapshot path: {end_frame_rel!r}")
        if not end_frame_file.is_file():
            raise ValueError(f"end frame snapshot not found: {end_frame_file.name}")
        end_image = end_frame_file

    # 切片是本次请求的一次性输入而非项目产物：落临时目录、随请求结束即弃。写进 music/ 会在
    # 镜头改时长/改入点后留下一堆与剧本不再对应的残片，且被归档一并带走。
    with tempfile.TemporaryDirectory(dir=tempfile.gettempdir(), prefix="arcreel-lipsync-") as tmpdir:
        driving_audio = (
            await _slice_lip_sync_window(lip_sync_source, item, Path(tmpdir) / f"driving_{resource_id}.wav")
            if lip_sync_source is not None
            else None
        )
        _, version, _, video_uri = await generator.generate_video_async(
            prompt=prompt_text,
            resource_type="videos",
            resource_id=resource_id,
            start_image=storyboard_file,
            end_image=end_image,
            driving_audio=driving_audio,
            aspect_ratio=aspect_ratio,
            duration_seconds=duration_seconds,
            resolution=resolution,
            task_id=task_id,
            seed=seed,
            service_tier=service_tier,
        )

    return await _finalize_video_task(
        project_name=project_name,
        script_file=script_file,
        project_path=project_path,
        resource_id=resource_id,
        version=version,
        video_uri=video_uri,
        generator=generator,
    )


async def _finalize_video_task(
    *,
    project_name: str,
    script_file: str,
    project_path: Path,
    resource_id: str,
    version: int,
    video_uri: str | None,
    generator: Any,
) -> dict[str, Any]:
    """Normal + resume 共用的 finalize 逻辑：写 scene asset + 抽缩略图 + 返回 result dict。"""

    def _update_video_metadata():
        get_project_manager().update_scene_asset(
            project_name=project_name,
            script_filename=script_file,
            scene_id=resource_id,
            asset_type="video_clip",
            asset_path=f"videos/scene_{resource_id}.mp4",
        )
        if video_uri:
            get_project_manager().update_scene_asset(
                project_name=project_name,
                script_filename=script_file,
                scene_id=resource_id,
                asset_type="video_uri",
                asset_path=video_uri,
            )

    await asyncio.to_thread(_update_video_metadata)

    video_file = project_path / f"videos/scene_{resource_id}.mp4"
    thumbnail_file = project_path / f"thumbnails/scene_{resource_id}.jpg"
    if await extract_video_thumbnail(video_file, thumbnail_file):
        await asyncio.to_thread(
            get_project_manager().update_scene_asset,
            project_name=project_name,
            script_filename=script_file,
            scene_id=resource_id,
            asset_type="video_thumbnail",
            asset_path=f"thumbnails/scene_{resource_id}.jpg",
        )
    else:
        thumbnail_file.unlink(missing_ok=True)

    created_at = await asyncio.to_thread(
        lambda: generator.versions.get_versions("videos", resource_id)["versions"][-1]["created_at"]
    )

    return {
        "version": version,
        "file_path": f"videos/scene_{resource_id}.mp4",
        "created_at": created_at,
        "resource_type": "videos",
        "resource_id": resource_id,
        "video_uri": video_uri,
    }


async def execute_character_task(
    project_name: str,
    resource_id: str,
    payload: dict[str, Any],
    *,
    user_id: str = DEFAULT_USER_ID,
    task_id: str | None = None,
) -> dict[str, Any]:
    prompt = str(payload.get("prompt", "") or "").strip()
    if not prompt:
        raise ValueError("prompt is required for character task")

    def _prepare_char():
        _project = get_project_manager().load_project(project_name)
        _project_path = get_project_manager().get_project_path(project_name)
        if resource_id not in _project.get("characters", {}):
            raise ValueError(f"character not found: {resource_id}")
        _char_data = _project["characters"][resource_id]
        _style = _project.get("style", "")
        _style_desc = _project.get("style_description", "")
        _full_prompt = build_character_prompt(resource_id, prompt, _style, _style_desc)
        _ref_images = None
        _ref_path = _char_data.get("reference_image")
        if _ref_path:
            _full_ref = _project_path / _ref_path
            if _full_ref.exists():
                _ref_images = [_full_ref]
        return _project, _full_prompt, _ref_images

    project, full_prompt, reference_images = await asyncio.to_thread(_prepare_char)
    _needs_i2i = bool(reference_images)

    ctx = await resolve_generation_context(
        project_name,
        payload,
        project=project,
        user_id=user_id,
        image=ImageLaneRequest(capability="i2i" if _needs_i2i else "t2i"),
    )
    generator = ctx.generator
    aspect_ratio = get_aspect_ratio(project, "characters")
    image_size = ctx.image.resolution

    _, version = await generator.generate_image_async(
        prompt=full_prompt,
        resource_type="characters",
        resource_id=resource_id,
        reference_images=reference_images,
        aspect_ratio=aspect_ratio,
        image_size=image_size,
    )

    sheet_path = f"characters/{resource_id}.png"

    def _finalize_char():
        def _set_character_sheet(p: dict) -> None:
            p["characters"][resource_id]["character_sheet"] = sheet_path

        get_project_manager().update_project(project_name, _set_character_sheet)
        return generator.versions.get_versions("characters", resource_id)["versions"][-1]["created_at"]

    created_at = await asyncio.to_thread(_finalize_char)

    return {
        "version": version,
        "file_path": f"characters/{resource_id}.png",
        "created_at": created_at,
        "resource_type": "characters",
        "resource_id": resource_id,
    }


# 仅保留 design 任务的「prompt 构造器」差异；bucket_key 与 sheet 写入由 ASSET_SPECS 与
# ProjectManager._update_asset_sheet 统一派发。
_DESIGN_PROMPT_BUILDERS: dict[str, Any] = {
    "scene": build_scene_prompt,
    "prop": build_prop_prompt,
    "product": build_product_prompt,
}


def _collect_product_reference_images(project: dict, project_path: Path, resource_id: str) -> list[Path] | None:
    """产品原图（保真验收锚点）作为 sheet 标准化整理的参考输入；缺失文件跳过。"""
    entry = (project.get("products") or {}).get(resource_id) or {}
    refs = entry.get("reference_images")
    if not isinstance(refs, list):
        return None
    # safe_exists 同时兜住脏数据（非字符串）、越出项目目录的绝对路径 / `..` 穿越与文件缺失
    existing = [project_path / ref for ref in refs if safe_exists(project_path, ref)]
    if refs and not existing:
        # 声明了原图却全部缺失：下游（sheet 生成 / 镜头保真注入）静默退化会丢失保真锚定，
        # 留观测痕迹便于诊断（不阻塞——文件缺失可能是归档迁移等正常历史原因）。
        # 文案保持场景中立：本函数同时服务 sheet 生成与产品镜头参考收集两个调用方。
        logger.warning("产品 '%s' 声明了 %d 张原图但磁盘均缺失", resource_id, len(refs))
    return existing or None


# design 任务的参考图收集器差异：product 的 sheet 是「原图 → 标准多角度图」的整理，
# 原图全量注入；scene / prop 维持纯文生图。
_DESIGN_REFERENCE_COLLECTORS: dict[str, Any] = {
    "product": _collect_product_reference_images,
}


async def execute_design_task(
    kind: str,
    project_name: str,
    resource_id: str,
    payload: dict[str, Any],
    *,
    user_id: str = DEFAULT_USER_ID,
) -> dict[str, Any]:
    """合并 execute_scene_task / execute_prop_task / execute_product_task：按 kind 查表派发。"""
    spec = ASSET_SPECS[kind]
    bucket_key = spec.bucket_key
    prompt_builder = _DESIGN_PROMPT_BUILDERS[kind]
    reference_collector = _DESIGN_REFERENCE_COLLECTORS.get(kind)

    prompt = str(payload.get("prompt", "") or "").strip()
    if not prompt:
        raise ValueError(f"prompt is required for {kind} task")

    def _prepare():
        project = get_project_manager().load_project(project_name)
        project_path = get_project_manager().get_project_path(project_name)
        if resource_id not in project.get(bucket_key, {}):
            raise ValueError(f"{kind} not found: {resource_id}")
        style = project.get("style", "")
        style_desc = project.get("style_description", "")
        full_prompt = prompt_builder(resource_id, prompt, style, style_desc)
        refs = reference_collector(project, project_path, resource_id) if reference_collector else None
        return project, full_prompt, refs

    project, full_prompt, reference_images = await asyncio.to_thread(_prepare)
    needs_i2i = bool(reference_images)

    ctx = await resolve_generation_context(
        project_name,
        payload,
        project=project,
        user_id=user_id,
        image=ImageLaneRequest(capability="i2i" if needs_i2i else "t2i"),
    )
    generator = ctx.generator
    aspect_ratio = get_aspect_ratio(project, bucket_key)
    image_size = ctx.image.resolution

    _, version = await generator.generate_image_async(
        prompt=full_prompt,
        resource_type=bucket_key,
        resource_id=resource_id,
        reference_images=reference_images,
        aspect_ratio=aspect_ratio,
        image_size=image_size,
    )

    sheet_path = f"{bucket_key}/{resource_id}.png"

    def _finalize():
        get_project_manager()._update_asset_sheet(kind, project_name, resource_id, sheet_path)
        return generator.versions.get_versions(bucket_key, resource_id)["versions"][-1]["created_at"]

    created_at = await asyncio.to_thread(_finalize)

    return {
        "version": version,
        "file_path": sheet_path,
        "created_at": created_at,
        "resource_type": bucket_key,
        "resource_id": resource_id,
    }


async def execute_scene_task(
    project_name: str,
    resource_id: str,
    payload: dict[str, Any],
    *,
    user_id: str = DEFAULT_USER_ID,
    task_id: str | None = None,
) -> dict[str, Any]:
    return await execute_design_task("scene", project_name, resource_id, payload, user_id=user_id)


async def execute_prop_task(
    project_name: str,
    resource_id: str,
    payload: dict[str, Any],
    *,
    user_id: str = DEFAULT_USER_ID,
    task_id: str | None = None,
) -> dict[str, Any]:
    return await execute_design_task("prop", project_name, resource_id, payload, user_id=user_id)


async def execute_product_task(
    project_name: str,
    resource_id: str,
    payload: dict[str, Any],
    *,
    user_id: str = DEFAULT_USER_ID,
    task_id: str | None = None,
) -> dict[str, Any]:
    return await execute_design_task("product", project_name, resource_id, payload, user_id=user_id)


def _group_scenes_by_segment_break(items: list[dict], id_field: str) -> list[list[dict]]:
    """Groups consecutive scene dicts, breaking at segment_break=True.

    Delegates to :func:`lib.storyboard_sequence.group_scenes_by_segment_break`.
    """
    return group_scenes_by_segment_break(items, id_field)


def _collect_grid_reference_images(
    project_path: Path,
    payload: dict[str, Any],
    scene_ids: list[str],
) -> tuple[list[object] | None, list[dict]]:
    """Collect character/scene/prop sheet images referenced by grid scenes.

    Returns a tuple of ``(image_paths, metadata)``:
    - *image_paths*: up to 6 :class:`~pathlib.Path` objects for the generation API.
    - *metadata*: list of dicts ``{path, name, ref_type}`` for persisting in
      :class:`~lib.grid.models.GridGeneration`.
    """
    project_json = project_path / "project.json"
    if not project_json.exists():
        return None, []

    import json

    project = json.loads(project_json.read_text(encoding="utf-8"))

    script_file = payload.get("script_file")
    if not script_file:
        return None, []

    script_path = project_path / "scripts" / script_file
    if not script_path.exists():
        return None, []

    script = json.loads(script_path.read_text(encoding="utf-8"))

    items, id_field, char_field, scene_field, prop_field = get_storyboard_items(script)

    scene_id_set = set(scene_ids)
    matched_items = [item for item in items if str(item.get(id_field, "")) in scene_id_set]

    characters = project.get("characters")
    characters = characters if isinstance(characters, dict) else {}
    project_scenes = project.get("scenes")
    project_scenes = project_scenes if isinstance(project_scenes, dict) else {}
    project_props = project.get("props")
    project_props = project_props if isinstance(project_props, dict) else {}

    seen: set[str] = set()
    paths: list[Path] = []
    metadata: list[dict] = []
    max_count = 6

    for item in matched_items:
        for char_name in item.get(char_field) or []:
            if not isinstance(char_name, str):
                continue
            char_data = characters.get(char_name)
            sheet = char_data.get("character_sheet") if isinstance(char_data, dict) else None
            if isinstance(sheet, str) and sheet and sheet not in seen:
                p = project_path / sheet
                if p.exists():
                    paths.append(p)
                    seen.add(sheet)
                    metadata.append({"path": sheet, "name": char_name, "ref_type": "character"})
        for scene_name in item.get(scene_field) or []:
            if not isinstance(scene_name, str):
                continue
            scene_data = project_scenes.get(scene_name)
            sheet = scene_data.get("scene_sheet") if isinstance(scene_data, dict) else None
            if isinstance(sheet, str) and sheet and sheet not in seen:
                p = project_path / sheet
                if p.exists():
                    paths.append(p)
                    seen.add(sheet)
                    metadata.append({"path": sheet, "name": scene_name, "ref_type": "scene"})
        for prop_name in item.get(prop_field) or []:
            if not isinstance(prop_name, str):
                continue
            prop_data = project_props.get(prop_name)
            sheet = prop_data.get("prop_sheet") if isinstance(prop_data, dict) else None
            if isinstance(sheet, str) and sheet and sheet not in seen:
                p = project_path / sheet
                if p.exists():
                    paths.append(p)
                    seen.add(sheet)
                    metadata.append({"path": sheet, "name": prop_name, "ref_type": "prop"})
        if len(paths) >= max_count:
            break

    return list(paths[:max_count]) or None, metadata[:max_count]


async def execute_grid_task(
    project_name: str,
    resource_id: str,
    payload: dict[str, Any],
    *,
    user_id: str = DEFAULT_USER_ID,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Execute a grid image generation task.

    resource_id is the grid_id. Steps:
    1. Load GridGeneration, set status to generating
    2. Generate image via MediaGenerator
    3. Split grid image into cells
    4. Assign cell images to scenes in the script
    5. Mark completed
    """
    from PIL import Image

    from lib.grid.splitter import split_grid_image
    from lib.grid_manager import GridManager

    project_path = await asyncio.to_thread(get_project_manager().get_project_path, project_name)
    grid_manager = GridManager(project_path)

    # a) Load grid
    grid = grid_manager.get(resource_id)
    if grid is None:
        raise ValueError(f"grid not found: {resource_id}")

    script_file = grid.script_file

    try:
        # b) Set status to generating
        grid.status = "generating"
        grid.error_message = None
        grid_manager.save(grid)

        # c) Build reference images + metadata
        from lib.grid.models import ReferenceImage

        reference_images, ref_metadata = await asyncio.to_thread(
            _collect_grid_reference_images, project_path, payload, grid.scene_ids
        )
        grid.reference_images = [ReferenceImage.from_dict(m) for m in ref_metadata] if ref_metadata else []
        grid_manager.save(grid)

        # d) Generate grid image
        prompt_text = payload.get("prompt") or grid.prompt
        if not prompt_text:
            raise ValueError("prompt is required for grid task")

        _needs_i2i = bool(reference_images)
        project = await asyncio.to_thread(get_project_manager().load_project, project_name)
        ctx = await resolve_generation_context(
            project_name,
            payload,
            project=project,
            user_id=user_id,
            image=ImageLaneRequest(capability="i2i" if _needs_i2i else "t2i"),
        )
        generator = ctx.generator
        aspect_ratio = payload.get("grid_aspect_ratio") or get_aspect_ratio(project, "storyboards")

        # 回填 grid metadata：route 层创建/重建时无法预知 needs_i2i，由此处补齐。
        # provider 记 registry 身份（供后续重解析定位供应商），model 记 backend 实际身份
        # （自定义供应商目标 model 被禁用回退时，实际调用的 model 与解析出的 model_id 不同）。
        grid.provider = ctx.image.provider_model.provider_id
        grid.model = ctx.image.backend_model
        grid_manager.save(grid)
        image_size = ctx.image.resolution or "2K"  # 宫格图保底高分辨率

        image_path, version = await generator.generate_image_async(
            prompt=prompt_text,
            resource_type="grids",
            resource_id=resource_id,
            reference_images=reference_images,
            aspect_ratio=aspect_ratio,
            image_size=image_size,
        )

        # e) Set grid_image_path, status to splitting
        grid.grid_image_path = f"grids/{resource_id}.png"
        grid.status = "splitting"
        grid_manager.save(grid)

        # f) Split the grid image
        grid_image = Image.open(image_path)
        video_aspect_ratio = get_aspect_ratio(project, "videos")
        cells = split_grid_image(grid_image, grid.rows, grid.cols, video_aspect_ratio)

        # g) Assign cells to scenes
        storyboards_dir = project_path / "storyboards"
        storyboards_dir.mkdir(parents=True, exist_ok=True)

        def _assign_cells():
            from lib.script_editor import resolve_items

            # batch_update_scene_assets 在任一 scene_id 未命中时整批 fail-loud 回滚——避免
            # cell.save() 已写 PNG 落盘后又因 KeyError 整批回滚留下 orphan PNG,这里先 load
            # 当前剧本拿 valid id 集合,frame_chain 中已不存在的分镜(grid plan 生成后 agent
            # split/remove 改动了剧本)跳过 cell PNG 保存 + 收集到 missing 列表 + warning。
            pm = get_project_manager()
            script = pm.load_script(project_name, script_file)
            items, id_field, _kind = resolve_items(script)
            valid_ids = {str(item.get(id_field)) for item in items if isinstance(item, dict)}

            asset_updates: list[tuple[str, str, Any]] = []
            missing_ids: list[str] = []

            # 宫格已统一走普通图生视频（不再使用 first_last 模式），cell 仅作为
            # next_scene_id 的起始分镜图，文件名与普通分镜对齐为 scene_{id}.png。
            for cell, frame in zip(cells, grid.frame_chain):
                if frame.frame_type == "placeholder":
                    continue
                if frame.frame_type not in ("first", "transition"):
                    continue
                if not frame.next_scene_id:
                    continue

                if str(frame.next_scene_id) not in valid_ids:
                    missing_ids.append(str(frame.next_scene_id))
                    continue

                cell_rel = f"storyboards/scene_{frame.next_scene_id}.png"
                cell_path = storyboards_dir / f"scene_{frame.next_scene_id}.png"
                # 与 MediaGenerator 版本顺序一致：旧文件先补登再覆写、覆写后登记新版本。
                # 否则宫格重切的单元格不进版本史，版本面板的「当前版本」与磁盘内容脱节，
                # 且下一次还原/上传会让未登记的格子字节永久丢失。
                generator.versions.ensure_current_tracked("storyboards", str(frame.next_scene_id), cell_path, "")
                cell.save(cell_path, format="PNG")
                generator.versions.add_version(
                    resource_type="storyboards",
                    resource_id=str(frame.next_scene_id),
                    prompt="",
                    source_file=cell_path,
                    source="grid_split",
                    grid_id=resource_id,
                )
                frame.image_path = cell_rel
                asset_updates.append((frame.next_scene_id, "storyboard_image", cell_rel))
                asset_updates.append((frame.next_scene_id, "grid_id", resource_id))
                asset_updates.append((frame.next_scene_id, "grid_cell_index", frame.index))

            if missing_ids:
                logger.warning(
                    "grid %s: frame_chain 中以下分镜在剧本 %s 已不存在,跳过 cell 保存: %s",
                    resource_id,
                    script_file,
                    sorted(set(missing_ids)),
                )

            # Batch-write all asset updates in one script read+write pass
            if asset_updates:
                pm.batch_update_scene_assets(
                    project_name=project_name,
                    script_filename=script_file,
                    updates=asset_updates,
                )

        await asyncio.to_thread(_assign_cells)

        # h) Set status to completed
        grid.status = "completed"
        grid_manager.save(grid)

    except Exception:
        grid.status = "failed"
        import traceback

        grid.error_message = traceback.format_exc()
        grid_manager.save(grid)
        raise

    created_at = grid.created_at

    return {
        "version": version,
        "file_path": f"grids/{resource_id}.png",
        "created_at": created_at,
        "resource_type": "grids",
        "resource_id": resource_id,
    }


async def _execute_reference_video_task_proxy(
    project_name: str,
    resource_id: str,
    payload: dict[str, Any],
    *,
    user_id: str,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Lazy proxy to avoid circular import: reference_video_tasks imports from this module."""
    from server.services.reference_video_tasks import execute_reference_video_task

    return await execute_reference_video_task(project_name, resource_id, payload, user_id=user_id, task_id=task_id)


async def _execute_image_edit_task_proxy(
    project_name: str,
    resource_id: str,
    payload: dict[str, Any],
    *,
    user_id: str,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Lazy proxy to avoid circular import: image_edit_tasks imports from this module."""
    from server.services.image_edit_tasks import execute_image_edit_task

    return await execute_image_edit_task(project_name, resource_id, payload, user_id=user_id, task_id=task_id)


_TASK_EXECUTORS = {
    "storyboard": execute_storyboard_task,
    "video": execute_video_task,
    "tts": execute_tts_task,
    "music": execute_music_task,
    "singing": execute_singing_task,
    "character": execute_character_task,
    "scene": execute_scene_task,
    "prop": execute_prop_task,
    "product": execute_product_task,
    "grid": execute_grid_task,
    "reference_video": _execute_reference_video_task_proxy,
    "image_edit": _execute_image_edit_task_proxy,
}


async def execute_generation_task(task: dict[str, Any]) -> dict[str, Any]:
    task_type = task.get("task_type")
    project_name = task.get("project_name")
    resource_id = str(task.get("resource_id"))
    payload = task.get("payload") or {}
    user_id = task.get("user_id", DEFAULT_USER_ID)
    queue_task_id = task.get("task_id")

    if not project_name:
        raise ValueError("task.project_name is required")
    if not task_type:
        raise ValueError("task.task_type is required")

    executor = _TASK_EXECUTORS.get(task_type)
    if executor is None:
        raise ValueError(f"unsupported task_type: {task_type}")

    with project_change_source("worker"):
        # 能力类异常（Image/VideoCapabilityError、ReferencePayloadFloorError）原样上抛：
        # worker 的 _encode_task_failure_message 按 code + params 落库，渲染留到读侧
        # Translator，同一失败任务按 Accept-Language 显示 zh/en/vi。
        result = await executor(project_name, resource_id, payload, user_id=user_id, task_id=queue_task_id)
        emit_generation_success_batch(
            task_type=task_type,
            project_name=project_name,
            resource_id=resource_id,
            payload=payload,
        )
        return result
