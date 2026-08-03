"""ChatCut 交接包导出服务。

产出交给 OpenChatCut 继续剪辑的 ``arcreel-chatcut-handoff@1`` 单个 JSON。与剪映草稿包**并存**，
不替代它：剪映包要拷到 Windows 上给剪映用，必须自包含（素材打进 ZIP、路径改写成用户本机剪映
目录）；而 OpenChatCut 通常与 ArcReel 同机/同网，素材本来就在 ArcReel 手里。两条需求塞进一个
格式只会互相牵制，故各出各的。

包分两层：

- **时间线层**：与剪映草稿等价的信息（轨道、片段、字幕、转场），让不理解 ArcReel 的消费方也能
  把时间线建起来
- **结构层**：剪映草稿承载不了的创作结构（角色、段落、生成意图、备选版本），不认识它的消费方
  直接忽略即可

素材只写可拉取的 URL 而非打包字节：剪映包的体积随片长与素材数增长到百 MB 量级，交接包恒为 KB 级。URL 走
``GET /api/v1/files/{project}/{path}``（前端显示素材用的就是它），无需新增端点。
"""

import logging
from pathlib import Path
from typing import Any

from lib.asset_types import ASSET_SPECS
from lib.project_manager import ProjectManager, effective_mode
from lib.script_skeleton import SKELETONS, resolve_declared_kind
from lib.version_manager import VersionManager
from server.services.episode_timeline import (
    NoCompletedSegmentsError,
    collect_video_clips,
    has_subtitle_track,
    resolve_canvas_size,
    resolve_music_track,
    script_content_mode,
)

logger = logging.getLogger(__name__)

__all__ = ["ChatcutHandoffService", "HANDOFF_FORMAT", "STRUCTURE_ITEM_FIELDS"]

#: 包格式标识。消费方按它分派解析器（``.ccproj.json`` 与 ``.ccdraft.json`` 都是 .json，
#: 按扩展名分不开），故写在顶层且带版本号。
HANDOFF_FORMAT = "arcreel-chatcut-handoff@1"

#: 每种内容模式在结构层携带的**条目级**字段。
#:
#: 新增 content_mode 必须在此登记，由 tests/test_mode_dispatch_exhaustiveness.py 守住——漏登记
#: 不会报错，只会导出一份没有创作结构的包，而那正是这个格式存在的理由。
#:
#: 出场资产（角色/场景/道具/产品）不在表内：角色字段名随骨架而变，取 ``SKELETONS[kind].chars_field``；
#: scenes / props 各模式同名，统一携带。
STRUCTURE_ITEM_FIELDS: dict[str, tuple[str, ...]] = {
    "narration": ("novel_text",),
    "drama": ("utterances",),
    "ad": ("section", "voiceover_text", "products_in_shot"),
    "mv": ("section", "start_seconds", "is_performance", "lyrics_line"),
}

#: 结构层携带的生成意图字段。剪辑侧据此认出「这一镜想要什么」，将来可回调 ArcReel 重生成。
_PROMPT_FIELDS: tuple[str, ...] = ("image_prompt", "video_prompt")

#: 参考直出（``video_units`` 骨架）的条目级字段。按**骨架**登记而非塞进 STRUCTURE_ITEM_FIELDS：
#: 那张表按 content_mode 登记，而 narration / drama 走参考直出时条目形状与各自的分镜直出完全
#: 不同（unit 无 novel_text / utterances / image_prompt），却共用同一个 content_mode。
#: unit 的生成意图落在各 shot 的 ``text`` 上，故整份 shots 带走。
_UNIT_ITEM_FIELDS: tuple[str, ...] = ("shots",)

#: unit 的 ``references``（``{type, name}``）按 type 归到与分镜条目同名的出场资产键，
#: 让剪辑侧对两条生成路径看到同一套键，不必知道来源骨架。
_REFERENCE_TYPE_TO_FIELD: dict[str, str] = {"character": "characters", "scene": "scenes", "prop": "props"}

#: 各模式在文档级附带的额外字段（条目级之外）。mv 的镜头钉在歌曲时间轴上，
#: 没有 song 就无法解释 start_seconds 与 section。
_STRUCTURE_DOC_FIELDS: dict[str, tuple[str, ...]] = {
    "mv": ("song",),
}


def _file_url(project_name: str, relative_path: str) -> str:
    """项目内相对路径 → 可拉取的 URL 路径。

    只写 path 不写 host：ArcReel 不知道别人怎么访问它（同机 docker 网络、反代、隧道各不相同），
    host 由 ``source.base_url`` 单独给出，消费方导入时还能覆盖。
    """
    from urllib.parse import quote

    return f"/api/v1/files/{quote(project_name)}/{quote(relative_path)}"


class ChatcutHandoffService:
    """ChatCut 交接包导出服务。

    与 :class:`~server.services.jianying_draft_service.JianyingDraftService` 共用
    ``episode_timeline`` 的片段收集：两者对「哪些镜头已出片、各自多长、配什么字幕」的理解必须
    一致，各写一份的表现是两个导出口给出不同的成片。
    """

    def __init__(self, project_manager: ProjectManager):
        self.pm = project_manager

    def build_handoff(self, project_name: str, episode: int, *, base_url: str) -> dict[str, Any]:
        """产出交接包 dict（由路由序列化为 JSON）。

        Raises:
            FileNotFoundError: 项目或剧集不存在
            NoCompletedSegmentsError: 无可导出的视频片段
        """
        project = self.pm.load_project(project_name)
        project_dir = self.pm.get_project_path(project_name)
        script = self._load_episode_script(project_name, project, episode)

        content_mode = script_content_mode(script)
        ep_entry = next((e for e in project.get("episodes", []) if e.get("episode") == episode), None)
        source_language = project.get("source_language")
        generation_mode = effective_mode(project=project, episode=ep_entry or {})
        clips = collect_video_clips(
            script,
            project_dir,
            generation_mode=generation_mode,
            language=source_language if isinstance(source_language, str) else None,
        )
        if not clips:
            raise NoCompletedSegmentsError(f"第 {episode} 集没有已完成的视频片段，请先生成视频")

        width, height = resolve_canvas_size(project, clips[0]["abs_path"])

        return {
            "format": HANDOFF_FORMAT,
            "source": {
                "product": "ArcReel",
                "project": project_name,
                "episode": episode,
                "content_mode": content_mode,
                "base_url": base_url,
            },
            "canvas": {"width": width, "height": height, "fps": 30},
            "tracks": self._build_tracks(project_name, project_dir, clips, content_mode),
            "structure": self._build_structure(
                project_name, project_dir, project, script, content_mode, generation_mode
            ),
        }

    def _load_episode_script(self, project_name: str, project: dict, episode: int) -> dict:
        """定位并载入指定集的剧本。"""
        ep_entry = next((e for e in project.get("episodes", []) if e.get("episode") == episode), None)
        if ep_entry is None:
            raise FileNotFoundError(f"第 {episode} 集不存在")
        return self.pm.load_script(project_name, Path(ep_entry.get("script_file", "")).name)

    # ------------------------------------------------------------------
    # 第一层：时间线
    # ------------------------------------------------------------------

    def _build_tracks(
        self,
        project_name: str,
        project_dir: Path,
        clips: list[dict[str, Any]],
        content_mode: str,
    ) -> list[dict[str, Any]]:
        """视频轨 + 旁白轨 + 音乐轨 + 字幕轨。片段起点按时长顺次累加。"""
        video_clips: list[dict[str, Any]] = []
        caption_clips: list[dict[str, Any]] = []
        narration_clips: list[dict[str, Any]] = []
        start = 0.0
        for clip in clips:
            # 声明了绝对入点就钉在那里（MV 的镜头对着歌曲时间轴），否则顺次累加。
            # 只有 MVShot 声明该字段，故无需按 content_mode 分派。
            declared_start = clip.get("start_seconds")
            if declared_start is not None:
                start = float(declared_start)
            duration = float(clip["duration_seconds"])
            video_clips.append(
                {
                    "id": clip["id"],
                    "start": start,
                    "duration": duration,
                    "src": _file_url(project_name, clip["video_clip"]),
                    "transition_to_next": clip["transition_to_next"],
                }
            )
            caption_clips.extend(self._caption_clips(clip, start, duration))
            narration_clips.extend(self._narration_clips(project_name, project_dir, clip, start, duration))
            start += duration

        tracks: list[dict[str, Any]] = [{"kind": "video", "name": "主轨", "clips": video_clips}]

        if narration_clips:
            tracks.append({"kind": "audio", "name": "旁白", "clips": narration_clips})

        music_src = resolve_music_track(project_dir)
        if music_src is not None:
            # 整支音轨从 0 起铺满全片：MV 的镜头本就钉在歌曲时间轴上，音轨不随镜头切分
            tracks.append(
                {
                    "kind": "audio",
                    "name": "音乐",
                    "clips": [
                        {
                            "start": 0.0,
                            "duration": start,
                            "src": _file_url(project_name, music_src.relative_to(project_dir).as_posix()),
                        }
                    ],
                }
            )

        if caption_clips and has_subtitle_track(content_mode):
            tracks.append({"kind": "caption", "name": "字幕", "clips": caption_clips})
        return tracks

    @staticmethod
    def _narration_clips(
        project_name: str,
        project_dir: Path,
        clip: dict[str, Any],
        start: float,
        duration: float,
    ) -> list[dict[str, Any]]:
        """该片段的旁白配音（TTS 产物），摆在片段起点。

        narration / drama / ad 的成片主音轨就是这条：漏掉它，导入剪辑器后除了背景音乐整片没声，
        而画面、字幕、转场全都对——这类错误在 ArcReel 内部完全看不出来。

        时长取**镜头时长**而非音频真实时长，与剪映导出（读 AudioMaterial.duration 逐条精算）不同：
        交接包是纯元数据导出，为拿音频时长去 ffprobe 会把 KB 级的快导出拖成逐文件探测，而这正是
        它相对剪映包的意义所在。差异由剪辑侧承担——OpenChatCut 拉到音频后本就知道真实时长，
        用户在时间线上一拖即调；宁可给一个诚实的近似，也不为精确牺牲这个格式的定位。
        """
        audio_abs = clip.get("narration_audio_abs")
        if audio_abs is None:
            return []
        return [
            {
                "start": start,
                "duration": duration,
                "src": _file_url(project_name, Path(audio_abs).relative_to(project_dir).as_posix()),
            }
        ]

    @staticmethod
    def _caption_clips(clip: dict[str, Any], start: float, duration: float) -> list[dict[str, Any]]:
        """一个片段的字幕条目。

        span 派生模式（drama / ad 参考直出）按片段内偏移逐条摆放；单字段模式整段一条。
        两者都可能为空（纯器乐段、氛围镜），空则不产条目而非产一条空字幕。

        span 一律收口到片段末尾，与剪映导出同一套规则（见 ``jianying_draft_service`` 的字幕分支）：
        drama 的 span 由语速估算逐条累加、**不按场景时长收口**（见 ``utterance_subtitle_spans``），
        台词长过镜头时原样导出会让字幕压到下一镜甚至越过整条视频。三条规则缺一不可——起点越界的
        整条丢弃、空文案不产条目、时长夹到片段末尾。
        """
        clip_end = start + duration
        spans = clip.get("subtitle_spans")
        if spans:
            bounded: list[dict[str, Any]] = []
            for span in spans:
                text = span.get("text")
                span_start = start + float(span["offset_seconds"])
                if not text or span_start >= clip_end:
                    continue
                bounded.append(
                    {
                        "start": span_start,
                        "duration": min(float(span["duration_seconds"]), clip_end - span_start),
                        "text": text,
                    }
                )
            return bounded
        text = clip.get("subtitle_text")
        if isinstance(text, str) and text:
            return [{"start": start, "duration": duration, "text": text}]
        return []

    # ------------------------------------------------------------------
    # 第二层：创作结构
    # ------------------------------------------------------------------

    def _build_structure(
        self,
        project_name: str,
        project_dir: Path,
        project: dict,
        script: dict,
        content_mode: str,
        generation_mode: str | None = None,
    ) -> dict[str, Any]:
        """按内容模式表驱动地导出创作结构。

        未登记的 content_mode（未知脏值）只出资产清单、不出条目结构——与其猜哪些字段有意义，
        不如少给：消费方拿到空的 items 会退化成纯时间线，而猜错字段名会产出看似有内容的垃圾。
        """
        structure: dict[str, Any] = {"assets": self._asset_index(project_name, project)}

        item_fields = STRUCTURE_ITEM_FIELDS.get(content_mode)
        if item_fields is None:
            logger.warning("content_mode %r 未登记结构层字段，交接包只出时间线与资产清单", content_mode)
            return structure

        for field in _STRUCTURE_DOC_FIELDS.get(content_mode, ()):
            if field in script:
                structure[field] = script[field]

        # generation_mode 一并传入：narration / drama 走参考直出时条目在 video_units 下，
        # 漏传会回落到 segments / scenes 取到空列表——结构层静默变空，而这一层正是这个格式
        # 存在的理由。
        kind = resolve_declared_kind(script.get("content_mode"), generation_mode)
        id_field = SKELETONS[kind].id_field
        chars_field = SKELETONS[kind].chars_field
        is_unit_skeleton = kind == "video_units"
        copied = _UNIT_ITEM_FIELDS if is_unit_skeleton else (*item_fields, *_PROMPT_FIELDS, "scenes", "props")

        items: list[dict[str, Any]] = []
        for item in script.get(kind, []):
            if not isinstance(item, dict):
                continue
            entry: dict[str, Any] = {"id": item.get(id_field, "")}
            for field in copied:
                if field in item:
                    entry[field] = item[field]
            if is_unit_skeleton:
                entry.update(self._reference_asset_names(item.get("references")))
            elif chars_field and chars_field in item:
                # 角色字段名随骨架而变（characters_in_segment / _in_scene / _in_shot），
                # 归一到 characters，剪辑侧不必知道来源骨架
                entry["characters"] = item[chars_field]
            items.append(entry)
        structure["items"] = items

        # 时间线是 unit 级时，必须一并给出 unit → 成员镜头的分组：没有它，剪辑侧手里是一段
        # unit_id 的视频和一堆 shot_id 的结构，两边对不上，产品/口播/生成意图全都关联不到画面。
        groups = self._reference_unit_groups(script, content_mode, generation_mode)
        if groups:
            structure["units"] = groups

        # 备选版本挂在「谁对应一个视频文件」那一层：分镜直出时是条目，参考直出时是 unit。
        # 挂错层的表现不是报错而是 versions 恒为空——功能悄悄没了，且只有拿真实项目导一次才
        # 看得出来。两条参考路径的成片都按 unit_id 记在 reference_videos 下，差别只在 ad 的
        # unit 另立分组、narration / drama 的 unit 本身就是条目。
        is_reference = is_unit_skeleton or bool(groups)
        self._attach_versions(
            project_name,
            project_dir,
            groups or items,
            "reference_videos" if is_reference else "videos",
        )
        return structure

    @staticmethod
    def _reference_asset_names(references: object) -> dict[str, list[str]]:
        """unit 的 ``references`` → 与分镜条目同名的出场资产名单。

        ``SKELETONS["video_units"].chars_field`` 为 ``None``，按骨架表的约定消费方必须在此显式
        决策而不是拿空字段名去 ``get``（见 ``lib/script_skeleton`` 模块 docstring）：参考直出的
        出场资产不以平铺名称列表存在，而是混在 ``references`` 里按 type 区分。

        保序去重：``references`` 的先后决定 prompt 里 [图N] 的编号，重排会让结构层与生成意图
        对不上。type / name 的脏值整条跳过，不产半条目。
        """
        by_field: dict[str, list[str]] = {}
        for ref in references if isinstance(references, list) else []:
            if not isinstance(ref, dict):
                continue
            ref_type = ref.get("type")
            name = ref.get("name")
            if not isinstance(ref_type, str) or not isinstance(name, str) or not name:
                continue
            field = _REFERENCE_TYPE_TO_FIELD.get(ref_type)
            if field is None:
                continue
            names = by_field.setdefault(field, [])
            if name not in names:
                names.append(name)
        return by_field

    @classmethod
    def _attach_versions(
        cls,
        project_name: str,
        project_dir: Path,
        entries: list[dict[str, Any]],
        resource_type: str,
    ) -> None:
        """就地给结构条目补 ``versions``；无版本记录的条目不出这个键，不留一地空列表。"""
        alternates = cls._version_alternates(
            project_name, project_dir, resource_type, [str(entry.get("id", "")) for entry in entries]
        )
        if not alternates:
            return
        for entry in entries:
            found = alternates.get(str(entry.get("id", "")))
            if found:
                entry["versions"] = found

    @staticmethod
    def _reference_unit_groups(
        script: dict,
        content_mode: str,
        generation_mode: str | None,
    ) -> list[dict[str, Any]]:
        """ad 参考直出的 unit → 成员镜头分组；其余路径为空。

        判据与 ``collect_video_clips`` 的 ad unit 分支同一条（content_mode=ad 且
        generation_mode=reference_video），两处不一致就会出现「时间线按 unit 排、结构层却不给
        分组」或反过来的空分组。

        只 ad 需要这层分组：ad 的 unit 只存 ``shot_ids`` 索引、内容仍在 shots 里，条目与成片
        不是一一对应。narration / drama 的 ``video_units`` 自带内容、本身就是结构条目，
        unit_id 与时间线片段 id 直接对得上，再出一份分组只是同义反复。
        """
        if not (content_mode == "ad" and generation_mode == "reference_video"):
            return []
        groups: list[dict[str, Any]] = []
        units = script.get("reference_units")
        for unit in units if isinstance(units, list) else []:
            if not isinstance(unit, dict):
                continue
            shot_ids = unit.get("shot_ids")
            groups.append(
                {
                    "id": unit.get("unit_id", ""),
                    "item_ids": [s for s in shot_ids if isinstance(s, str)] if isinstance(shot_ids, list) else [],
                }
            )
        return groups

    @staticmethod
    def _version_alternates(
        project_name: str,
        project_dir: Path,
        resource_type: str,
        resource_ids: list[str],
    ) -> dict[str, list[dict[str, Any]]]:
        """按资源 id 取备选版本索引，供剪辑侧一键换版（当前只能回 ArcReel 重新生成）。

        URL 自己铸而非用 ``get_versions`` 附带的 ``file_url``：后者不做百分号转义，
        与包内其余素材路径（``_file_url``）口径不一致——同一个中文项目名在一个包里出现两种
        写法，消费方按其中一种拼接就会 404。

        先探 versions.json 是否存在再构造 ``VersionManager``：后者的 ``__init__`` 会建出
        整套版本目录，而导出是只读操作——没有版本记录的项目不该因为被导出一次就多出一批空
        目录，只读挂载的项目目录下更会直接抛错。
        """
        if not (project_dir / "versions" / "versions.json").exists():
            return {}
        manager = VersionManager(project_dir)
        index: dict[str, list[dict[str, Any]]] = {}
        for resource_id in resource_ids:
            if not resource_id:
                continue
            info = manager.get_versions(resource_type, resource_id)
            current = info.get("current_version", 0)
            alternates: list[dict[str, Any]] = []
            for record in info.get("versions", []):
                if not isinstance(record, dict):
                    continue
                number = record.get("version")
                relative = record.get("file")
                if not isinstance(number, int) or not isinstance(relative, str) or not relative:
                    continue
                alternates.append({"v": number, "src": _file_url(project_name, relative), "current": number == current})
            if alternates:
                index[resource_id] = alternates
        return index

    @staticmethod
    def _asset_index(project_name: str, project: dict) -> dict[str, Any]:
        """角色/场景/道具/产品的设定图索引，供剪辑侧按身份筛选片段。

        由 ``ASSET_SPECS`` 驱动而非逐类硬编码：新增资产类型时只在 spec 注册即可，
        这里不用改（与 ``_asset_router_factory`` 同一约定）。
        """
        index: dict[str, Any] = {}
        for spec in ASSET_SPECS.values():
            bucket = project.get(spec.bucket_key)
            if not isinstance(bucket, dict):
                continue
            entries: dict[str, Any] = {}
            for name, asset in bucket.items():
                if not isinstance(asset, dict):
                    continue
                sheet = asset.get(spec.sheet_field)
                entries[name] = {"sheet": _file_url(project_name, sheet)} if isinstance(sheet, str) and sheet else {}
            if entries:
                index[spec.bucket_key] = entries
        return index
