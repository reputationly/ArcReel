"""MV 内容模式：骨架路由、剧本模型、prompt 构建器的守卫。"""

from __future__ import annotations

import pytest

from lib.profile_manifest import VALID_CONTENT_MODES
from lib.prompt_builders_mv import build_mv_prompt
from lib.script_models import MVEpisodeScript, MVShot, SongMeta
from lib.script_skeleton import resolve_declared_kind, resolve_script_kind
from lib.script_structure_validator import _select_model


def _song(duration: float = 90) -> dict:
    return {
        "duration_seconds": duration,
        "sections": [
            {"name": "intro", "start_seconds": 0, "duration_seconds": 8},
            {"name": "verse", "start_seconds": 8, "duration_seconds": 32},
            {"name": "chorus", "start_seconds": 40, "duration_seconds": 50},
        ],
    }


class TestSkeletonRouting:
    @pytest.mark.unit
    def test_mv_registered_as_content_mode(self):
        assert "mv" in VALID_CONTENT_MODES

    @pytest.mark.unit
    def test_mv_uses_shots_skeleton(self):
        # 与 ad 同形状（平铺数组 + shot_id），骨架表只描述形状、不区分模型
        assert resolve_declared_kind("mv", None) == "shots"
        assert resolve_declared_kind("mv", "storyboard") == "shots"
        assert resolve_script_kind({"content_mode": "mv", "shots": []}) == "shots"

    @pytest.mark.unit
    def test_shots_skeleton_selects_model_by_content_mode(self):
        # 同一骨架下 ad / mv 模型不同：字段不同（mv 有 start_seconds / section / lyrics_line）
        assert _select_model({"content_mode": "mv", "shots": []}).__name__ == "MVEpisodeScript"
        assert _select_model({"content_mode": "ad", "shots": []}).__name__ == "AdEpisodeScript"


class TestMVScriptModel:
    @pytest.mark.unit
    def test_song_and_lyrics_hidden_from_generation_schema(self):
        """song 与 lyrics 都不由剧本生成产出，但理由不同。

        song 是作曲步骤的产物；lyrics 是「用户给方向 → agent 优化 → 用户改定稿」的产物，
        在排镜头前就已确定。若让剧本生成一并产出，每次重排镜头都会冲掉用户改过的定稿——
        而重排镜头是常规操作（改段落划分、换视频模型档位都要重排）。
        """
        properties = MVEpisodeScript.model_json_schema()["properties"]
        assert "song" not in properties
        assert "lyrics" not in properties
        assert set(properties) == {"title", "shots"}

    @pytest.mark.unit
    def test_start_seconds_is_required(self):
        # MV 的镜头必须钉在歌曲绝对时间轴上：累加式排布下一镜偏移会让后面全部错位
        assert "start_seconds" in MVShot.model_fields
        assert MVShot.model_fields["start_seconds"].is_required()

    @pytest.mark.unit
    def test_song_duration_defaults_to_zero_not_none(self):
        # 默认 0 让「尚未作曲」可判定；None 会让调用方各自兜底
        assert SongMeta().duration_seconds == 0


class TestMVScriptGeneratorRouting:
    """MV 必须有自己的生成分支——否则会掉进 drama step2 去找不存在的 step1 中间文件。"""

    @pytest.mark.unit
    def test_schema_builder_dispatches_mv_not_ad(self):
        from lib.script_models import build_episode_script_model

        assert build_episode_script_model("mv", [3, 4]).__name__ == "MVEpisodeScript"
        assert build_episode_script_model("ad", [3, 4]).__name__ == "AdEpisodeScript"

    @pytest.mark.unit
    def test_mv_schema_keeps_song_and_lyrics_out_of_llm_output(self):
        from lib.script_models import build_episode_script_model

        props = build_episode_script_model("mv", [3, 4]).model_json_schema()["properties"]
        assert set(props) == {"title", "shots"}

    @pytest.mark.unit
    def test_generator_has_mv_branch_before_drama_fallback(self):
        """generate() 里 mv 分支必须在 drama step2 判定之前。

        drama 判定是 `gen_mode != "reference_video" and content_mode != "narration"`——
        mv 恒真，排在后面就永远进不了自己的分支。
        """
        import inspect

        from lib.script_generator import ScriptGenerator

        src = inspect.getsource(ScriptGenerator.generate)
        mv_pos = src.index('self.content_mode == "mv"')
        drama_pos = src.index("_generate_drama_step2")
        assert mv_pos < drama_pos


class TestMVProjectBootstrap:
    """新建 MV 项目必须能走出第一步——这里曾是死锁。

    song / lyrics 存在剧本顶层，而剧本生成要靠 song 的实测时长排镜头：两者互为前置。
    若建项目时既不落 episode 条目、patch_song 又拒绝为不存在的剧本创建骨架，用户建完
    项目就卡死——写不了歌（没剧本），也生成不了剧本（没歌）。
    """

    @pytest.mark.unit
    def test_mv_is_a_single_episode_mode(self):
        from lib.project_manager import ProjectManager

        assert "mv" in ProjectManager.SINGLE_EPISODE_MODES
        assert "ad" in ProjectManager.SINGLE_EPISODE_MODES

    @pytest.mark.unit
    def test_created_mv_project_has_an_episode_entry(self, tmp_path):
        from lib.project_manager import ProjectManager

        pm = ProjectManager(str(tmp_path))
        pm.create_project("mv-demo")
        pm.create_project_metadata("mv-demo", title="Demo", style="s", content_mode="mv")
        project = pm.load_project("mv-demo")

        episodes = project.get("episodes")
        assert isinstance(episodes, list) and len(episodes) == 1
        assert episodes[0]["episode"] == 1

    @pytest.mark.unit
    def test_data_layer_rejects_default_duration(self, tmp_path):
        """MV 不持有单镜时长偏好，数据层也要拦。

        路由与前端都已按模式表拒绝，数据层只认 ad 的话，非路由调用方（归档导入、脚本、
        测试夹具）仍能建出带该字段的 MV 项目——而这个字段之后既不生效也改不掉
        （PATCH 对它出现本身就返回 400）。
        """
        from lib.project_manager import ProjectManager

        pm = ProjectManager(str(tmp_path))
        pm.create_project("mv-dur")
        with pytest.raises(ValueError, match="default_duration"):
            pm.create_project_metadata("mv-dur", title="T", style="s", content_mode="mv", default_duration=5)

    @pytest.mark.unit
    def test_song_metadata_is_written_through_patch_song_only(self):
        """agent 指引必须指向 patch_song——song / lyrics 是剧本顶层字段。

        `patch_episode_script` 按分镜 id 定位、只改镜头字段，写不了顶层。指引写错的后果不是
        报错就完事：agent 会反复重试一个永远不会成功的写入，卡在作曲与排镜头之间。
        """
        from pathlib import Path as _Path

        docs = [
            _Path("agent_runtime_profile/CLAUDE.mv.md"),
            _Path("agent_runtime_profile/.claude/skills/manga-workflow/SKILL.mv.md"),
        ]
        for doc in docs:
            lines = doc.read_text(encoding="utf-8").splitlines()
            assert any("patch_song" in line for line in lines), f"{doc}: 没有提到 patch_song"
            for line in lines:
                # 「写进顶层」这个动作只能由 patch_song 承担。允许 patch_episode_script 与
                # 「顶层」同行出现在澄清句里（"改不了顶层"），故按动词短语而非「顶层」二字判定。
                if "写进剧本顶层" in line or "写进剧本" in line and "顶层" in line:
                    assert "patch_song" in line, f"{doc}: 顶层字段写入指向了错误的工具\n{line}"

    @pytest.mark.unit
    def test_seed_skeleton_is_not_reported_as_a_generated_script(self, tmp_path):
        """写歌落下的空骨架不能被当成「剧本已生成」。

        patch_song 必须先落一个空 shots 的骨架（song/lyrics 存剧本顶层，与剧本生成互为前置）。
        若按「文件存在即 generated」判定，刚写完歌的 MV 项目会直接跳进 production：时间线是
        空的，引导也不再提示去排镜头——用户看到一个「已完成到制作阶段」的空项目。
        """
        from lib.project_manager import ProjectManager
        from lib.status_calculator import StatusCalculator

        pm = ProjectManager(str(tmp_path))
        pm.create_project("mv-status")
        pm.create_project_metadata("mv-status", title="T", style="s", content_mode="mv")
        calc = StatusCalculator(pm)

        seed = {"content_mode": "mv", "title": "", "shots": [], "song": {}, "lyrics": ""}
        assert calc._status_for_loaded_script(seed) == "none"

        # 歌已作完（回写了实测时长）→ 等价于其余模式的「已分段」，下一步就是排镜头
        with_song = {**seed, "song": {"duration_seconds": 92.0}}
        assert calc._status_for_loaded_script(with_song) == "segmented"

        # 镜头表排好之后才是 generated
        with_shots = {**with_song, "shots": [{"shot_id": "E1S01"}]}
        assert calc._status_for_loaded_script(with_shots) == "generated"

    @pytest.mark.unit
    def test_song_progress_lands_the_project_in_worldbuilding_not_production(self, tmp_path):
        """端到端核对阶段：写完歌的 MV 项目应停在 worldbuilding（引导指向生成剧本）。"""
        from lib.project_manager import ProjectManager
        from lib.status_calculator import StatusCalculator

        pm = ProjectManager(str(tmp_path))
        pm.create_project("mv-phase")
        pm.create_project_metadata("mv-phase", title="T", style="s", content_mode="mv")
        pm.save_script(
            "mv-phase",
            {"content_mode": "mv", "title": "T", "shots": [], "song": {"duration_seconds": 92.0}, "lyrics": "词"},
            "episode_1.json",
            validate=True,
        )
        status = StatusCalculator(pm).calculate_project_status("mv-phase", pm.load_project("mv-phase"))
        assert status["current_phase"] == "worldbuilding"

    @pytest.mark.unit
    def test_seed_skeleton_passes_structure_validation(self):
        """patch_song 代建的最小骨架必须能过写盘校验，否则代建本身就会失败。"""
        from lib.script_structure_validator import validate_script_structure

        seed = {"content_mode": "mv", "title": "", "shots": [], "song": {}, "lyrics": ""}
        assert not validate_script_structure(seed).errors


class TestMVProjectValidation:
    """校验器要与创建/PATCH 的模式约束读同一张表。

    创建/PATCH 已按 ProjectManager 的模式表拒绝，校验器只认 ad 的话，归档导入进来的 MV 项目
    能带着一个之后既不生效也改不掉的 default_duration 通过校验——而 data_validator 正是
    project.json 的完整性闸门。
    """

    def _validate(self, tmp_path, project: dict):
        import json

        from lib.data_validator import DataValidator

        project_dir = tmp_path / "projects" / "demo"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "project.json").write_text(json.dumps(project, ensure_ascii=False), encoding="utf-8")
        return DataValidator(projects_root=str(tmp_path / "projects")).validate_project("demo")

    def _mv_project(self, **overrides) -> dict:
        project = {
            "title": "夜色",
            "content_mode": "mv",
            "style": "电影感",
            "characters": {},
            "scenes": {},
            "props": {},
            "episodes": [{"episode": 1, "title": "", "script_file": "scripts/episode_1.json"}],
        }
        project.update(overrides)
        return project

    @pytest.mark.unit
    def test_valid_mv_project_passes(self, tmp_path):
        result = self._validate(tmp_path, self._mv_project())
        assert result.valid, result.errors

    @pytest.mark.unit
    def test_default_duration_rejected(self, tmp_path):
        result = self._validate(tmp_path, self._mv_project(default_duration=5))
        assert not result.valid
        assert any("default_duration" in e for e in result.errors)

    @pytest.mark.unit
    def test_multi_episode_rejected(self, tmp_path):
        """mv 恒单集：多集元数据说明这份 project.json 不是 MV 该有的形状。"""
        project = self._mv_project(
            episodes=[
                {"episode": 1, "title": "", "script_file": "scripts/episode_1.json"},
                {"episode": 2, "title": "", "script_file": "scripts/episode_2.json"},
            ]
        )
        result = self._validate(tmp_path, project)
        assert not result.valid
        assert any("单条" in e for e in result.errors)

    @pytest.mark.unit
    def test_unsupported_generation_mode_rejected(self, tmp_path):
        """MV 不走参考直出（口型驱动要分镜图作人物首帧）。落盘之后才报，报的是生成期的
        能力不匹配，指不回「这个模式压根不该选这条路径」。"""
        result = self._validate(tmp_path, self._mv_project(generation_mode="reference_video"))
        assert not result.valid
        assert any("reference_video" in e for e in result.errors)

    @pytest.mark.unit
    def test_unsupported_generation_mode_rejected_at_episode_level(self, tmp_path):
        """集级覆盖能单独把某一集切到不支持的路径，只查项目级会漏掉。"""
        project = self._mv_project(
            episodes=[
                {
                    "episode": 1,
                    "title": "",
                    "script_file": "scripts/episode_1.json",
                    "generation_mode": "grid",
                }
            ]
        )
        result = self._validate(tmp_path, project)
        assert not result.valid
        assert any("grid" in e for e in result.errors)

    @pytest.mark.unit
    def test_data_layer_rejects_unsupported_generation_mode(self, tmp_path):
        """非路由调用方（归档导入 / 脚本 / 测试夹具）经 extras 写入同样要被挡。"""
        from lib.project_manager import ProjectManager

        pm = ProjectManager(str(tmp_path))
        pm.create_project("mv-gen")
        with pytest.raises(ValueError, match="reference_video"):
            pm.create_project_metadata(
                "mv-gen",
                title="T",
                style="s",
                content_mode="mv",
                extras={"generation_mode": "reference_video"},
            )

    @pytest.mark.unit
    def test_ad_only_fields_still_rejected_for_mv(self, tmp_path):
        result = self._validate(tmp_path, self._mv_project(brief="卖点"))
        assert not result.valid
        assert any("brief" in e for e in result.errors)


class TestMVSongPreservation:
    """生成/重排镜头表不得冲掉已定稿的 song / lyrics——这是 MV 工作流的地基。

    两个字段对 LLM 隐藏（SkipJsonSchema），但 ``model_dump`` 会把模型默认值
    （duration_seconds=0 的空 song、空串 lyrics）带进解析结果。按「缺席才补」保留的话
    条件永远为假：定稿被默认值覆盖、后续重排因 duration=0 被 prompt 守卫拒绝，
    用户得重新作曲写词。必须端到端跑真实的 解析→保留→写盘 链路才能暴露。
    """

    _SONG = {
        "style": "民谣",
        "duration_seconds": 92.0,
        "bpm": 88,
        "audio_path": "music/main.wav",
        "sections": [{"name": "verse", "start_seconds": 0, "duration_seconds": 92}],
    }

    @pytest.mark.unit
    async def test_generate_keeps_finalized_song_and_lyrics(self, tmp_path):
        import json

        from lib.project_manager import ProjectManager
        from lib.script_generator import ScriptGenerator

        pm = ProjectManager(str(tmp_path))
        pm.create_project("mv-keep")
        pm.create_project_metadata("mv-keep", title="T", style="s", content_mode="mv")
        # patch_song 之后的状态：歌已作完、词已定稿、镜头表还空着
        pm.save_script(
            "mv-keep",
            {"content_mode": "mv", "title": "T", "shots": [], "song": dict(self._SONG), "lyrics": "第一句\n第二句"},
            "episode_1.json",
            validate=True,
        )

        # LLM 只会产出 title + shots（song / lyrics 已从生成 schema 排除）
        llm_output = {
            "title": "夜色",
            "shots": [
                {
                    "shot_id": "E1S01",
                    "section": "verse",
                    "start_seconds": 0,
                    "duration_seconds": 4,
                    "lyrics_line": "第一句",
                    "is_performance": True,
                    "image_prompt": {
                        "scene": "天台夜景",
                        "composition": {"shot_type": "中景", "lighting": "夜", "ambiance": "霓虹"},
                    },
                    "video_prompt": {"action": "演唱", "camera_motion": "Static", "ambiance_audio": "风声"},
                }
            ],
        }

        class _FakeResult:
            text = json.dumps(llm_output, ensure_ascii=False)

        class _FakeTextGenerator:
            model = "fake"

            async def generate(self, request, project_name=None):  # noqa: ANN001, ARG002
                return _FakeResult()

        gen = ScriptGenerator(tmp_path / "mv-keep", generator=_FakeTextGenerator())  # type: ignore[arg-type]
        await gen._generate_and_save("prompt", object, 1, None)

        saved = pm.load_script("mv-keep", "episode_1.json")
        assert saved["song"]["duration_seconds"] == 92.0, "实测时长被默认值冲掉"
        assert saved["song"]["sections"], "段落表被清空"
        assert saved["lyrics"] == "第一句\n第二句", "定稿歌词被清空"
        assert saved["shots"], "镜头表未写入"


class TestMVDurationNarrowing:
    """MV 的时长档位要按「常规视频模型 ∩ 口型驱动模型」出。

    镜头在生成时才按 is_performance 分流到两个模型，而档位是排镜头时就定死的。只按常规模型
    出档位，演唱镜头会拿到 s2v 模型不支持的时长，一路过完剧本审阅、到视频生成才逐个失败。
    """

    def _generator(self, tmp_path, project: dict | None = None):
        import json

        from lib.script_generator import ScriptGenerator

        (tmp_path / "project.json").write_text(
            json.dumps(project or {"content_mode": "mv", "title": "T", "style": "s"}, ensure_ascii=False),
            encoding="utf-8",
        )
        return ScriptGenerator(tmp_path)

    @pytest.mark.unit
    async def test_intersects_with_lip_sync_durations(self, tmp_path, monkeypatch):
        from lib.config.resolver import ConfigResolver, ProviderModel

        async def _lip(self, project, payload):  # noqa: ANN001, ARG002
            return ProviderModel("custom-1", "infinitetalk-480p")

        async def _caps(self, provider_id, model_id, project=None):  # noqa: ANN001, ARG002
            return {"supported_durations": [5, 8, 10]}

        monkeypatch.setattr(ConfigResolver, "resolve_lip_sync_backend", _lip)
        monkeypatch.setattr(ConfigResolver, "video_capabilities_for_model", _caps)

        gen = self._generator(tmp_path)
        assert await gen._narrow_to_lip_sync_durations([4, 5, 8]) == [5, 8]

    @pytest.mark.unit
    async def test_unconfigured_lip_sync_model_does_not_narrow(self, tmp_path, monkeypatch):
        """没配口型模型时演唱镜头回落常规模型，档位本就一致，不该凭空收窄。"""
        from lib.config.resolver import ConfigResolver, ProviderModel

        async def _lip(self, project, payload):  # noqa: ANN001, ARG002
            return ProviderModel("", "")

        monkeypatch.setattr(ConfigResolver, "resolve_lip_sync_backend", _lip)
        gen = self._generator(tmp_path)
        assert await gen._narrow_to_lip_sync_durations([4, 5, 8]) == [4, 5, 8]

    @pytest.mark.unit
    async def test_empty_intersection_fails_loud(self, tmp_path, monkeypatch):
        """交集为空时报错而非取其一——静默取一边只是把失败推迟到每一镜上。"""
        from lib.config.resolver import ConfigResolver, ProviderModel

        async def _lip(self, project, payload):  # noqa: ANN001, ARG002
            return ProviderModel("custom-1", "infinitetalk-480p")

        async def _caps(self, provider_id, model_id, project=None):  # noqa: ANN001, ARG002
            return {"supported_durations": [10, 15]}

        monkeypatch.setattr(ConfigResolver, "resolve_lip_sync_backend", _lip)
        monkeypatch.setattr(ConfigResolver, "video_capabilities_for_model", _caps)

        gen = self._generator(tmp_path)
        with pytest.raises(ValueError, match="没有交集"):
            await gen._narrow_to_lip_sync_durations([4, 5, 8])

    @pytest.mark.unit
    def test_compose_mv_applies_the_narrowing(self):
        """守连接点：收窄函数单测全绿、_compose_mv 却没调它，是这条链最容易退化的形态。"""
        import inspect

        from lib.script_generator import ScriptGenerator

        src = inspect.getsource(ScriptGenerator._compose_mv)
        narrow_pos = src.index("_narrow_to_lip_sync_durations(")
        schema_pos = src.index("build_episode_script_model(")
        assert narrow_pos < schema_pos


class TestMVPromptGuards:
    def _build(self, **overrides):
        kwargs = {
            "project_overview": {},
            "style": "电影感",
            "style_description": "",
            "characters": {"小雨": {}},
            "scenes": {"天台": {}},
            "props": {},
            "song": _song(),
            "lyrics": "第一句\n第二句",
            "generation_mode": "storyboard",
            "supported_durations": [2, 3, 4, 5],
        }
        kwargs.update(overrides)
        return build_mv_prompt(**kwargs)

    @pytest.mark.unit
    def test_includes_section_table_and_measured_duration(self):
        prompt = self._build()
        assert "intro" in prompt and "chorus" in prompt
        assert "90" in prompt

    @pytest.mark.unit
    def test_rejects_when_song_not_generated(self):
        # 没有实测时长说明作曲没跑或产物没回写，此时生成的镜头表没有意义
        with pytest.raises(ValueError, match="歌曲实测时长"):
            self._build(song={"duration_seconds": 0})
        with pytest.raises(ValueError, match="歌曲实测时长"):
            self._build(song={})

    @pytest.mark.unit
    def test_rejects_reference_video_mode(self):
        # 口型驱动依赖分镜图作人物首帧，参考直出没有这一步
        with pytest.raises(ValueError, match="参考直出"):
            self._build(generation_mode="reference_video")

    @pytest.mark.unit
    def test_rejects_missing_supported_durations(self):
        with pytest.raises(ValueError, match="supported_durations"):
            self._build(supported_durations=None)

    @pytest.mark.unit
    def test_instrumental_song_guidance(self):
        prompt = self._build(lyrics="")
        assert "纯器乐" in prompt
