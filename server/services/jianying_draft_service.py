"""剪映草稿导出服务

将 ArcReel 单集已生成的视频片段导出为剪映草稿 ZIP。
使用 pyJianYingDraft 库生成 draft_content.json，
后处理路径替换使草稿指向用户本地剪映目录。
"""

import json
import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import pyJianYingDraft as draft
from pyJianYingDraft import (
    AudioMaterial,
    AudioSegment,
    ClipSettings,
    TextBorder,
    TextSegment,
    TextShadow,
    TextStyle,
    TrackType,
    TransitionType,
    VideoMaterial,
    VideoSegment,
    trange,
)

# transition_to_next schema 值 → 剪映 TransitionType。"cut" 不挂转场。
_TRANSITION_MAP: dict[str, TransitionType] = {
    "fade": TransitionType.闪黑,
    "dissolve": TransitionType.叠化,
}

from lib.path_safety import PathTraversalError, safe_join
from lib.project_manager import ProjectManager, effective_mode

# 片段收集、字幕 span 派生、音轨选择、画布尺寸都与目标格式无关，与 ChatCut 交接包共用同一份
# （见 episode_timeline 模块 docstring）——复制一份的代价是新增内容模式时要改两处，
# 而漏改的那一处不报错，只导出一份内容不全的草稿。
from server.services.episode_timeline import (
    NoCompletedSegmentsError,
    collect_video_clips,
    has_subtitle_track,
    resolve_canvas_size,
    resolve_music_track,
    script_content_mode,
)

logger = logging.getLogger(__name__)

__all__ = ["JianyingDraftService", "NoCompletedSegmentsError"]


class JianyingDraftService:
    """剪映草稿导出服务"""

    def __init__(self, project_manager: ProjectManager):
        self.pm = project_manager

    # ------------------------------------------------------------------
    # 内部方法：数据提取
    # ------------------------------------------------------------------

    def _find_episode_script(self, project_name: str, project: dict, episode: int) -> tuple[dict, str]:
        """定位指定集的剧本文件，返回 (script_dict, filename)"""
        episodes = project.get("episodes", [])
        ep_entry = next((e for e in episodes if e.get("episode") == episode), None)
        if ep_entry is None:
            raise FileNotFoundError(f"第 {episode} 集不存在")

        script_file = ep_entry.get("script_file", "")
        filename = Path(script_file).name
        script_data = self.pm.load_script(project_name, filename)
        return script_data, filename

    @staticmethod
    def _stage_file(src: Path, staging_dir: Path) -> Path:
        """将素材文件硬链接（失败时复制）到暂存区，返回暂存路径

        暂存区为扁平目录：来源文件同名时自动改名，避免覆盖已暂存的素材。
        同一来源的去重由调用方按源路径判定（不依赖 inode 比较，FAT/exFAT 等
        无稳定文件 ID 的文件系统上 samefile 会误判）。
        """
        dst = staging_dir / src.name
        rename_index = 1
        while dst.exists():
            dst = staging_dir / f"{src.stem}_{rename_index}{src.suffix}"
            rename_index += 1
        try:
            dst.hardlink_to(src)
        except OSError:
            shutil.copy2(src, dst)
        return dst

    # ------------------------------------------------------------------
    # 内部方法：草稿生成
    # ------------------------------------------------------------------

    def _generate_draft(
        self,
        *,
        draft_dir: Path,
        draft_name: str,
        clips: list[dict],
        width: int,
        height: int,
        content_mode: str,
        music_path: str | None = None,
    ) -> None:
        """使用 pyJianYingDraft 在 draft_dir 中生成草稿文件"""
        draft_dir.parent.mkdir(parents=True, exist_ok=True)
        folder = draft.DraftFolder(str(draft_dir.parent))
        script_file = folder.create_draft(draft_name, width=width, height=height, allow_replace=True)

        # 视频轨
        script_file.add_track(TrackType.video)

        # 字幕轨：注册为字幕模式的内容模式生成（narration / ad 单字段、drama 从 utterances 派生 span）
        has_subtitle = has_subtitle_track(content_mode)
        text_style: TextStyle | None = None
        text_border: TextBorder | None = None
        text_shadow: TextShadow | None = None
        subtitle_position: ClipSettings | None = None
        is_portrait = height > width
        if has_subtitle:
            script_file.add_track(TrackType.text, "字幕")
            text_style = TextStyle(
                size=12.0 if is_portrait else 8.0,
                color=(1.0, 1.0, 1.0),
                align=1,
                bold=True,
                auto_wrapping=True,
                max_line_width=0.82 if is_portrait else 0.6,
            )
            text_border = TextBorder(
                color=(0.0, 0.0, 0.0),
                width=30.0,
            )
            text_shadow = TextShadow(
                color=(0.0, 0.0, 0.0),
                alpha=0.7,
                diffuse=8.0,
                distance=3.0,
                angle=-45.0,
            )
            subtitle_position = ClipSettings(
                transform_y=-0.75 if is_portrait else -0.8,
            )

        # 逐片段添加
        offset_us = 0
        last_index = len(clips) - 1
        narration_placements: list[tuple[int, str]] = []
        for index, clip in enumerate(clips):
            # 预读实际视频时长
            material = VideoMaterial(clip["local_path"])
            actual_duration_us = material.duration

            # 声明了绝对入点就钉在那里：MV 的镜头对着歌曲时间轴，而实际产出时长按供应商档位
            # 取整、偏离规划值是常态——累加排布会让后面整条错位，且演唱镜的口型是按绝对歌曲
            # 位置切的驱动音频生成的，一漂移就对不上音乐。只有 MVShot 声明该字段，其余骨架
            # 取 None 走原有的累加。
            declared_start = clip.get("start_seconds")
            if declared_start is not None:
                offset_us = int(float(declared_start) * 1_000_000)

            # 视频片段
            video_seg = VideoSegment(
                material,
                trange(offset_us, actual_duration_us),
            )

            # 转场：剪映约定挂在前一段上，因此最后一段不挂；cut 不挂。
            if index < last_index:
                transition_type = _TRANSITION_MAP.get(clip.get("transition_to_next", "cut"))
                if transition_type is not None:
                    video_seg.add_transition(transition_type)

            script_file.add_segment(video_seg)

            # 字幕片段：unit 级片段（ad 参考直出）携带 subtitle_spans，按成员镜头
            # 在片段内逐镜头对齐；其余片段沿用整段单字幕。span 用规划时长定位，
            # 实际视频更短时夹到片段末尾，越界 span 跳过。
            if has_subtitle:
                spans = clip.get("subtitle_spans")
                if spans:
                    for span in spans:
                        span_start = offset_us + int(span["offset_seconds"] * 1_000_000)
                        span_duration = int(span["duration_seconds"] * 1_000_000)
                        clip_end = offset_us + actual_duration_us
                        if span_start >= clip_end or not span.get("text"):
                            continue
                        span_duration = min(span_duration, clip_end - span_start)
                        script_file.add_segment(
                            TextSegment(
                                text=span["text"],
                                timerange=trange(span_start, span_duration),
                                style=text_style,
                                border=text_border,
                                shadow=text_shadow,
                                clip_settings=subtitle_position,
                            )
                        )
                elif clip.get("subtitle_text"):
                    text_seg = TextSegment(
                        text=clip["subtitle_text"],
                        timerange=trange(offset_us, actual_duration_us),
                        style=text_style,
                        border=text_border,
                        shadow=text_shadow,
                        clip_settings=subtitle_position,
                    )
                    script_file.add_segment(text_seg)

            # 旁白音频：记录摆放位置（按视频片段 offset），统一在视频排布完成后添加
            narration_audio_local = clip.get("narration_audio_local")
            if narration_audio_local:
                narration_placements.append((offset_us, narration_audio_local))

            offset_us += actual_duration_us

        # 旁白素材：先解析全部音频文件，不可解析（截断/空文件等）的跳过不报错
        narration_materials: list[tuple[int, AudioMaterial]] = []
        for start_us, audio_path in narration_placements:
            try:
                narration_materials.append((start_us, AudioMaterial(audio_path)))
            except Exception as exc:
                # 解析失败不阻断导出：文件占用/损坏/底层库自定义异常均按跳过处理
                logger.warning("旁白音频无法解析，已跳过: %s (%s)", audio_path, exc)

        # 旁白音频段：时长取音频文件真实时长，不与视频对齐；
        # 仅当超长音频会与下一段旁白重叠时收口到其起点，保证草稿可导出（用户在剪映手动精调）
        narration_track_added = False
        for material_index, (start_us, audio_material) in enumerate(narration_materials):
            duration_us = audio_material.duration
            if material_index + 1 < len(narration_materials):
                window_us = narration_materials[material_index + 1][0] - start_us
                if duration_us > window_us:
                    logger.warning("旁白音频长过下一段起点，已收口: %s", audio_material.path)
                    duration_us = window_us
            if duration_us <= 0:
                logger.warning("旁白音频有效时长不足，已跳过: %s", audio_material.path)
                continue
            # 音轨仅在确有有效片段时创建，避免全部被过滤后留下空轨
            if not narration_track_added:
                script_file.add_track(TrackType.audio, "旁白")
                narration_track_added = True
            audio_seg = AudioSegment(audio_material, trange(start_us, duration_us))
            script_file.add_segment(audio_seg, "旁白")

        self._add_music_track(script_file, music_path)

        script_file.save()

    def _add_music_track(self, script_file: Any, music_path: str | None) -> None:
        """项目有配乐时挂一条独立音乐轨，从 0 起铺满自身时长。

        与旁白轨分开而非混在一条：剪映里两者要分别调音量（配乐通常压到 -20dB 给人声让位），
        同轨无法单独调整。不按视频总长裁剪——曲子长过片子时由用户在剪映裁，短过片子时留白，
        这里不替用户决定循环还是留白。

        ``music_path`` 为 None 即项目没有配乐，静默跳过：绝大多数项目（narration /
        drama / ad）本就没有，缺文件是常态而非异常。
        """
        if music_path is None:
            return

        try:
            material = AudioMaterial(music_path)
        except Exception as exc:
            logger.warning("配乐无法解析，已跳过: %s (%s)", music_path, exc)
            return

        if material.duration <= 0:
            logger.warning("配乐有效时长不足，已跳过: %s", music_path)
            return

        script_file.add_track(TrackType.audio, "配乐")
        script_file.add_segment(AudioSegment(material, trange(0, material.duration)), "配乐")

    def _replace_paths_in_draft(self, *, json_path: Path, tmp_prefix: str, target_prefix: str) -> None:
        """JSON 安全地替换 draft_content.json 中的临时路径"""
        try:
            real = str(safe_join(tempfile.gettempdir(), json_path))
        except PathTraversalError as exc:
            raise ValueError(f"路径越界，拒绝写入: {json_path}") from exc

        with open(real, encoding="utf-8") as f:  # noqa: PTH123
            data = json.load(f)

        def _walk(obj: Any) -> Any:
            if isinstance(obj, str) and tmp_prefix in obj:
                return obj.replace(tmp_prefix, target_prefix)
            if isinstance(obj, dict):
                return {k: _walk(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_walk(v) for v in obj]
            return obj

        data = _walk(data)
        with open(real, "w", encoding="utf-8") as f:  # noqa: PTH123
            json.dump(data, f, ensure_ascii=False)

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    def export_episode_draft(
        self,
        project_name: str,
        episode: int,
        draft_path: str,
        *,
        use_draft_info_name: bool = True,
    ) -> Path:
        """
        导出指定集的剪映草稿 ZIP。

        Returns:
            ZIP 文件路径（临时文件，调用方负责清理）

        Raises:
            FileNotFoundError: 项目或剧本不存在
            NoCompletedSegmentsError: 无可导出的视频片段
            ValueError: 暂存/写入阶段检测到路径越界（安全告警，不代表可预期的空态）
        """
        project = self.pm.load_project(project_name)
        project_dir = self.pm.get_project_path(project_name)

        # 1. 定位剧本
        script_data, _ = self._find_episode_script(project_name, project, episode)

        # 2. 收集已完成视频（生成路径按 project.json 解析：ad 参考直出收集 unit 级片段）
        content_mode = script_content_mode(script_data)
        ep_entry = next((e for e in project.get("episodes", []) if e.get("episode") == episode), None)
        # drama 字幕语速按项目源语言取（source_language 是唯一真相源，缺失 / 脏值时回退默认语速）
        source_language = project.get("source_language")
        clips = collect_video_clips(
            script_data,
            project_dir,
            generation_mode=effective_mode(project=project, episode=ep_entry or {}),
            language=source_language if isinstance(source_language, str) else None,
        )
        if not clips:
            raise NoCompletedSegmentsError(f"第 {episode} 集没有已完成的视频片段，请先生成视频")

        # 3. 画布尺寸（项目未设 aspect_ratio 时从首个视频自动检测）
        width, height = resolve_canvas_size(project, clips[0]["abs_path"])

        # 4. 创建临时目录 + 复制素材到暂存区
        raw_title = project.get("title")
        if not isinstance(raw_title, str) or not raw_title.strip():
            raw_title = project_name
        safe_title = raw_title.replace("/", "_").replace("\\", "_").replace("..", "_")
        # 恒单集的模式（ad / mv）界面不暴露「集」概念，草稿名直接用项目标题——
        # 名单取自 ProjectManager，不在此另列，否则新增单件成品模式会导出成「标题_第1集」
        draft_name = (
            safe_title if content_mode in ProjectManager.SINGLE_EPISODE_MODES else f"{safe_title}_第{episode}集"
        )
        # 消毒后可能只剩 pathlib 会丢弃的空段（如标题为 "."）：塌缩的草稿目录会让
        # create_draft(allow_replace=True) 把 rmtree 落到上层临时目录，这里回退项目名兜底
        if not draft_name.replace(".", "").strip():
            draft_name = project_name
        tmp_dir = Path(tempfile.mkdtemp(prefix="arcreel_jy_"))
        try:
            staging_dir = tmp_dir / "staging"
            staging_dir.mkdir()

            # 同一来源文件（safe_resolve 已规范化路径）只暂存一次，多段引用共享同一暂存副本
            staged_by_src: dict[Path, Path] = {}
            project_root = project_dir.resolve()

            def stage_once(src: Path) -> str:
                if src not in staged_by_src:
                    # 暂存前重校验：收集与暂存之间文件可能被替换（如换成越界 symlink）
                    try:
                        resolved = safe_join(project_root, src, require_file=True)
                    except (PathTraversalError, FileNotFoundError) as exc:
                        raise ValueError(f"路径越界，拒绝导出: {src}") from exc
                    staged_by_src[src] = self._stage_file(resolved, staging_dir)
                return str(staged_by_src[src])

            local_clips = []
            for clip in clips:
                local_clip = {**clip, "local_path": stage_once(clip["abs_path"])}
                audio_src = clip.get("narration_audio_abs")
                if audio_src:
                    local_clip["narration_audio_local"] = stage_once(audio_src)
                local_clips.append(local_clip)

            # 配乐与视频素材同样要暂存：草稿里的路径会被整体替换成用户本地剪映目录，
            # 直接引用项目内原路径会让草稿在对方机器上找不到文件。
            music_local: str | None = None
            music_src = resolve_music_track(project_dir)
            if music_src is not None:
                music_local = stage_once(music_src)

            # 5. 生成草稿（create_draft 会重建 draft_dir；草稿放独立父目录下，
            # 避免草稿名与暂存区等临时目录同级重名时被 allow_replace 误删）
            draft_dir = tmp_dir / "draft" / draft_name
            self._generate_draft(
                draft_dir=draft_dir,
                draft_name=draft_name,
                clips=local_clips,
                width=width,
                height=height,
                content_mode=content_mode,
                music_path=music_local,
            )

            # 6. 将素材移入草稿目录（暂存区内容即全部已暂存素材）
            assets_dir = draft_dir / "assets"
            assets_dir.mkdir(exist_ok=True)
            for staged in staging_dir.iterdir():
                # 源、目的地都过一遍 safe_join：目的地此前已校验，源（staged 文件名）
                # 未经校验直接传给 shutil.move 时，CodeQL 会把它当未经校验的 sink 输入
                try:
                    src = safe_join(staging_dir, staged.name, require_file=True)
                    dest = safe_join(assets_dir, staged.name)
                except (PathTraversalError, FileNotFoundError) as exc:
                    raise ValueError(f"路径越界，拒绝写入: {staged.name}") from exc
                # sink 前贴身补一道 realpath + startswith 冗余校验：safe_join 内部的
                # barrier 是否跨函数边界传播到这里的返回值未经证实，直接在 sink 所在
                # 函数内重做一遍最小化、CodeQL 已知可识别的收敛判断，不依赖跨函数推导。
                # 必须用 realpath 而非 normpath 与 src/dest（已是 safe_join 返回的
                # realpath 结果）对齐——staging_dir/assets_dir 所在的系统临时目录在
                # macOS 上是指向 /private/tmp 的 symlink，normpath 不展开会导致误判越界。
                src_str, dest_str = str(src), str(dest)
                staging_root = os.path.realpath(str(staging_dir)) + os.sep
                assets_root = os.path.realpath(str(assets_dir)) + os.sep
                if not src_str.startswith(staging_root):
                    raise ValueError(f"路径越界，拒绝写入: {staged.name}")
                if not dest_str.startswith(assets_root):
                    raise ValueError(f"路径越界，拒绝写入: {staged.name}")
                shutil.move(src_str, dest_str)

            # 7. 路径后处理：staging 路径 → 用户本地路径
            draft_content_path = draft_dir / "draft_content.json"
            self._replace_paths_in_draft(
                json_path=draft_content_path,
                tmp_prefix=str(staging_dir),
                target_prefix=f"{draft_path}/{draft_name}/assets",
            )

            # 8. 剪映 6+ 使用 draft_info.json，低版本使用 draft_content.json
            if use_draft_info_name:
                draft_content_path.rename(draft_dir / "draft_info.json")

            # 9. 打包 ZIP
            zip_path = tmp_dir / f"{draft_name}.zip"
            video_suffixes = {".mp4", ".webm", ".mov", ".avi", ".mkv"}
            with zipfile.ZipFile(zip_path, "w") as zf:
                for file in draft_dir.rglob("*"):
                    if file.is_file():
                        arcname = f"{draft_name}/{file.relative_to(draft_dir)}"
                        compress = zipfile.ZIP_STORED if file.suffix.lower() in video_suffixes else zipfile.ZIP_DEFLATED
                        zf.write(file, arcname, compress_type=compress)

            return zip_path
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise
