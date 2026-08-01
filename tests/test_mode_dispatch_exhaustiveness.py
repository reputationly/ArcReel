"""按 content_mode / task_type 分派的表必须覆盖全部取值——这是本仓最容易退化的一类缺陷。

新增一个内容模式（或一类生成任务）时，代码里有十几张按它键控的表：骨架解析、step1 探测、
路由字段白名单、生成分支、完成事件、资源指纹……漏掉其中任何一张都不会有编译错误，也不会有
既有测试报红——每张表单看都是对的，缺的是「这张表和那张表说的不是同一件事」。表现出来是
「新模式建得出项目，但走到第 N 步报一个指向别处的错」，而 N 因人而异。

本文件的断言全部指向**表之间的一致性**，不测单张表的内容。
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from lib.episode_paths import NO_STEP1_CONTENT_MODES, STEP1_FILENAMES
from lib.profile_manifest import VALID_CONTENT_MODES
from lib.project_manager import ProjectManager
from lib.script_skeleton import SKELETONS, resolve_declared_kind

#: _resolve_step1_path 只在返回非 None 时才拼路径，路径本身不必存在。
_DUMMY_PATH = Path("/nonexistent-project")


class TestContentModeCoverage:
    @pytest.mark.parametrize("mode", sorted(VALID_CONTENT_MODES))
    def test_every_mode_resolves_to_a_skeleton(self, mode: str):
        """resolve_declared_kind 对未知模式 fail-loud，新模式漏登记即在此报红。"""
        assert resolve_declared_kind(mode, None) in SKELETONS

    @pytest.mark.parametrize("mode", sorted(VALID_CONTENT_MODES))
    def test_step1_presence_is_declared_exactly_once(self, mode: str):
        """每个模式要么登记了 step1 文件名，要么声明无 step1——不能两者皆非。

        两者皆非的模式会掉进各消费方的「未知模式兜底」，被当作 drama 去找它永远不会有的
        step1_normalized_script.json，报出的错误指向缺文件而非模式未登记。
        """
        assert (mode in STEP1_FILENAMES) != (mode in NO_STEP1_CONTENT_MODES), (
            f"{mode!r} 既没登记 step1 文件名、也没声明无 step1"
        )

    @pytest.mark.parametrize("mode", sorted(VALID_CONTENT_MODES))
    def test_step1_consumers_agree_with_the_declaration(self, mode: str):
        """三个 step1 消费方必须与声明一致：探测候选、web 文件名映射、MCP 生成前置检查。

        它们曾各写一份 ``content_mode == "ad"``，于是 mv 在三处全被当成「该有 step1」：
        状态计算卡在 script_status=none、web 源文审阅页指向不存在的文件、生成工具直接
        返回「未找到 Step 1 文件」。
        """
        from lib.status_calculator import _draft_candidates
        from server.agent_runtime.sdk_tools.text_generation import _resolve_step1_path
        from server.routers.files import _get_step_files

        expected_none = mode in NO_STEP1_CONTENT_MODES
        assert (_draft_candidates(mode) == ()) is expected_none
        assert (_get_step_files(mode) == {}) is expected_none
        assert (_resolve_step1_path(_DUMMY_PATH, 1, {"content_mode": mode}) is None) is expected_none

    @pytest.mark.parametrize("mode", sorted(NO_STEP1_CONTENT_MODES))
    def test_one_shot_modes_have_their_own_generation_branch(self, mode: str):
        """无 step1 的模式必须在 generate() 与 build_prompt() 里各有一条分支。

        两者少任何一条都会掉进 drama 两段式：generate 少了会去找不存在的 step1 中间文件，
        build_prompt 少了则是 dry_run 预览出的 prompt 与实际生成用的不是同一个——后者尤其
        隐蔽，预览看着正常、真跑出来是另一回事。
        """
        from lib.script_generator import ScriptGenerator

        for method in (ScriptGenerator.generate, ScriptGenerator.build_prompt):
            src = inspect.getsource(method)
            assert f'self.content_mode == "{mode}"' in src, f"{method.__name__} 缺 {mode} 分支"

    def test_shots_skeleton_modes_can_be_edited_through_the_shots_endpoint(self):
        """走 shots 骨架的模式必须都能经 script-shots 端点改写。

        骨架表说「ad 与 mv 同形状」，而路由的字段白名单只列了 ad —— 结果是 MV 剧本渲染得
        出来、一保存就 400。两张表说的是同一件事，必须逐字对上。
        """
        from server.routers.projects import _SHOT_UPDATABLE_FIELDS_BY_MODE

        shots_modes = {m for m in VALID_CONTENT_MODES if resolve_declared_kind(m, None) == "shots"}
        assert set(_SHOT_UPDATABLE_FIELDS_BY_MODE) == shots_modes

    @pytest.mark.parametrize("mode", sorted(VALID_CONTENT_MODES))
    def test_generation_mode_table_only_names_known_modes(self, mode: str):
        """不开放的生成方式表不得出现未知取值（拼错的模式名等于该约束静默失效）。"""
        known = {"storyboard", "grid", "reference_video"}
        assert ProjectManager.UNSUPPORTED_GENERATION_MODES.get(mode, frozenset()) <= known

    def test_single_episode_and_no_default_duration_tables_name_known_modes(self):
        assert ProjectManager.SINGLE_EPISODE_MODES <= VALID_CONTENT_MODES
        assert ProjectManager.NO_DEFAULT_DURATION_MODES <= VALID_CONTENT_MODES
        assert set(ProjectManager.UNSUPPORTED_GENERATION_MODES) <= VALID_CONTENT_MODES


class TestTextFieldTables:
    """字幕表与口播表的关系：超集，且差集恰是「有词但不能念」的模式。

    两张表曾是同一张。合并的理由成立（narration / ad 的字幕与配音读同一份文案，分开写会
    漂移成「字幕有词、配音没声」），但 mv 打破了前提：歌词是字幕、不是可朗读的口播。
    合表时给 mv 补字幕，就顺手给它开了 TTS 的门——TTS 的准入判定就是「这张表查不查得到」。
    """

    def test_subtitle_table_is_a_superset_of_the_voiceover_table(self):
        from lib.script_models import SUBTITLE_TEXT_FIELDS, VOICEOVER_TEXT_FIELDS

        # 能朗读的文案必然也是字幕文案；且同一模式两侧字段名必须一致
        for mode, field in VOICEOVER_TEXT_FIELDS.items():
            assert SUBTITLE_TEXT_FIELDS.get(mode) == field, f"{mode} 的字幕与配音读了不同字段"

    def test_mv_has_subtitles_but_is_not_tts_eligible(self):
        """MV 的歌词进字幕、不进 TTS。反了的话产物是「被念出来的歌词」。"""
        from lib.script_models import subtitle_text_field, voiceover_text_field

        assert subtitle_text_field("mv") == "lyrics_line"
        assert voiceover_text_field("mv") is None

    def test_both_tts_entry_points_gate_on_the_voiceover_table(self):
        """TTS 两个入口都只凭这张表放行——所以「登记一个模式」就等于「给它开配音」。

        这条锁的是那层因果：将来谁想给新模式加字幕，会先在这里看到「登记 = 开门」。
        """
        import inspect as _inspect

        from server.agent_runtime.sdk_tools.enqueue_narration_audio import generate_narration_audio_tool
        from server.services.generation_tasks import execute_tts_task

        for fn in (execute_tts_task, generate_narration_audio_tool):
            assert "voiceover_text_field(" in _inspect.getsource(fn), fn.__name__


class TestCustomProviderCleanupCoverage:
    """凡是能存 ``custom-N/model`` 的设置键，删 provider / 删 model 时都必须被清理。

    漏登记的表现很隐蔽：用户删掉自定义供应商后设置项仍留着悬空引用，界面上看不出异常，
    直到下次生成才报「供应商不存在」——而那时用户已经不记得自己删过什么。
    """

    def test_every_backend_setting_is_cleaned_up_on_provider_deletion(self):
        from server.routers._validators import _FIELD_MEDIA_TYPES
        from server.routers.custom_providers import _BACKEND_SETTING_KEYS, _PROJECT_BACKEND_KEYS

        # _FIELD_MEDIA_TYPES 登记的都是 provider/model 形态的字段，按 default_ 前缀分全局 / 项目级
        cleaned = set(_BACKEND_SETTING_KEYS) | set(_PROJECT_BACKEND_KEYS)
        missing = set(_FIELD_MEDIA_TYPES) - cleaned
        assert not missing, f"这些 backend 设置删 provider 后会留下悬空引用: {sorted(missing)}"

    def test_project_level_music_overrides_are_cleaned_up(self):
        """resolver 支持的项目级覆盖键必须在项目清理表里——全局清了、项目没清同样会失败。"""
        from lib.config.resolver import _MUSIC_TASK_SETTING_KEYS
        from server.routers.custom_providers import _BACKEND_SETTING_KEYS, _PROJECT_BACKEND_KEYS

        for keys in _MUSIC_TASK_SETTING_KEYS.values():
            assert keys.project_field in _PROJECT_BACKEND_KEYS, keys.project_field
            assert keys.setting_key in _BACKEND_SETTING_KEYS, keys.setting_key


class TestAccountingCoverage:
    """每条真实调用供应商的生成路径都要记账——漏一条就是费用页少一块。

    症状是「用量对不上」，而这句话定位不到具体哪条路径漏了；等到有人去查，中间已经积了
    一堆无从追溯的调用。故按「执行器」穷举，而不是靠记得。
    """

    #: 不直接调供应商、因此不该有记账括号的任务类型。
    #: grid 的切割是本地图像处理（其宫格母图由 storyboard 路径生成时已记账）；
    #: image_edit 走 image_edit_tasks 自己的记账链。
    _NO_DIRECT_PROVIDER_CALL = {"grid", "image_edit"}

    @staticmethod
    def _accounts_for_usage(fn: object, module: object, depth: int = 0) -> bool:
        """该执行器是否记账：自己开括号，或委托给会记账的一方。

        两条记账形态——音乐 / 歌声自己开 ``Ledger.record`` 括号；图片 / 视频 / TTS 经
        ``generator.*_async``（记账在 MediaGenerator 内）。资产类执行器（character / scene /
        prop / product）是一层薄委托，故要跟进被委托函数再判一次。
        """
        import inspect as _inspect
        import re as _re

        src = _inspect.getsource(fn)  # type: ignore[arg-type]
        if "record(" in src or "generator." in src:
            return True
        if depth >= 2:
            return False
        # 同模块内的委托（资产类薄封装）
        candidates: list[object] = [
            t for name in set(_re.findall(r"\b(execute_\w+)\(", src)) if (t := getattr(module, name, None)) is not None
        ]
        # 跨模块的惰性委托（reference_video / image_edit 的 proxy 在函数体内 import 破循环依赖）
        for mod_path, name in _re.findall(r"from ([\w.]+) import (execute_\w+)", src):
            target_mod = __import__(mod_path, fromlist=[name])
            target = getattr(target_mod, name, None)
            if target is not None:
                candidates.append(target)
        for target in candidates:
            if target is not fn and TestAccountingCoverage._accounts_for_usage(target, module, depth + 1):
                return True
        return False

    def test_every_executor_records_usage(self):
        from server.services import generation_tasks

        for task_type, executor in generation_tasks._TASK_EXECUTORS.items():
            if task_type in self._NO_DIRECT_PROVIDER_CALL:
                continue
            assert self._accounts_for_usage(executor, generation_tasks), (
                f"{task_type} 的执行器既不记账也不委托给会记账的一方"
            )

    def test_music_channel_is_registered_end_to_end(self):
        """新增记账通道要三处齐全：CallType 枚举、结算分发、自定义供应商计价。

        少任一处的表现各不相同且都不报错——枚举少了是 typing-only、结算少了抛
        「unknown ledger channel」、计价少了费用恒为 0。
        """
        import inspect as _inspect

        from lib.cost_calculator import CostCalculator
        from lib.ledger import _settlement_from_result
        from lib.providers import CALL_TYPE_MUSIC

        assert CALL_TYPE_MUSIC == "music"
        assert '"music"' in _inspect.getsource(_settlement_from_result)
        assert '"music"' in _inspect.getsource(CostCalculator._calculate_custom_cost)


class TestValidatorMirrorsModeTables:
    """校验器与写入边界必须读同一张模式表——两者分叉时归档导入是绕过约束的现成入口。"""

    def test_project_validator_reads_the_mode_tables(self):
        import inspect as _inspect

        from lib.data_validator import DataValidator

        src = _inspect.getsource(DataValidator._validate_mode_project_fields)
        assert "NO_DEFAULT_DURATION_MODES" in src
        assert "SINGLE_EPISODE_MODES" in src

    @pytest.mark.parametrize(
        ("mode", "banned"),
        [(m, g) for m, gs in ProjectManager.UNSUPPORTED_GENERATION_MODES.items() for g in sorted(gs)],
    )
    def test_banned_generation_modes_rejected_at_both_layers(self, mode: str, banned: str, tmp_path):
        """每条「模式 × 禁用生成方式」在写入边界与校验闸门都要拦。

        只拦路由的话，归档导入 / 脚本 / 测试夹具能把项目配成一个之后必然生成失败的状态，
        而失败信息报的是生成期的能力不匹配，指不回「这个模式压根不该选这条路径」。
        """
        import json

        from lib.data_validator import DataValidator

        pm = ProjectManager(str(tmp_path))
        pm.create_project("demo")
        # 数据层写入边界
        with pytest.raises(ValueError, match=banned):
            pm.create_project_metadata(
                "demo", title="T", style="s", content_mode=mode, extras={"generation_mode": banned}
            )

        # 校验闸门（归档导入走这条）
        project = {
            "title": "T",
            "content_mode": mode,
            "style": "s",
            "characters": {},
            "scenes": {},
            "props": {},
            "episodes": [{"episode": 1, "title": "", "script_file": "scripts/episode_1.json"}],
            "generation_mode": banned,
        }
        if mode == "ad":
            project["target_duration"] = 60
        (tmp_path / "demo" / "project.json").write_text(json.dumps(project, ensure_ascii=False), encoding="utf-8")
        result = DataValidator(projects_root=str(tmp_path)).validate_project("demo")
        assert not result.valid
        assert any(banned in e for e in result.errors)

    @pytest.mark.parametrize("mode", sorted(ProjectManager.NO_DEFAULT_DURATION_MODES))
    def test_default_duration_rejected_for_every_such_mode(self, mode: str, tmp_path):
        import json

        from lib.data_validator import DataValidator

        project_dir = tmp_path / "projects" / "demo"
        project_dir.mkdir(parents=True)
        project = {
            "title": "T",
            "content_mode": mode,
            "style": "s",
            "characters": {},
            "scenes": {},
            "props": {},
            "episodes": [{"episode": 1, "title": "", "script_file": "scripts/episode_1.json"}],
            "default_duration": 5,
        }
        if mode == "ad":
            project["target_duration"] = 60
        (project_dir / "project.json").write_text(json.dumps(project, ensure_ascii=False), encoding="utf-8")

        result = DataValidator(projects_root=str(tmp_path / "projects")).validate_project("demo")
        assert not result.valid
        assert any("default_duration" in e for e in result.errors)


class TestSelectorBlankSemantics:
    """三个 MV 模型选择器的空值标签不得写成「自动选择」。

    旁白 TTS 留空会自动推断 provider；作曲 / 歌声 / 口型驱动的解析器**刻意不推断**
    （配了 key 不代表部署了 ACE-Step / SoulX-Singer，见 _resolve_default_music_backend
    docstring），留空在生成时报「尚未配置」。标签撒谎的后果：用户按「自动」的字面意思
    留空、然后在生成期撞错——错误离配置页隔了整条生成链。
    """

    def test_mv_selectors_label_blank_as_not_configured(self):
        from pathlib import Path as _Path

        src = _Path("frontend/src/components/pages/settings/MediaModelSection.tsx").read_text(encoding="utf-8")
        for field in ("default_music_backend", "default_singing_backend", "default_lip_sync_backend"):
            # 锚在 onChange 的 setter 上（首处出现是 draft 读取，不在选择器附近）
            after = src.split(f"{field}: v")[1][:300]
            assert "not_configured_option" in after, f"{field} 的空值标签不对"
            assert 't("auto_select")' not in after, f"{field} 不该标成自动选择"
        # TTS 保持 auto_select——它的空值真的会自动推断
        tts = src.split("default_audio_backend: v")[1][:300]
        assert 't("auto_select")' in tts


class TestCostTypeCoverage:
    """记账通道 → 费用页的分桶表必须齐全：账上有、页面没有，是最难对账的一种缺口。"""

    def test_actual_cost_types_cover_every_segment_bearing_channel(self):
        from lib.providers import CallType
        from server.services.cost_estimation import ACTUAL_COST_TYPES

        # text 不挂 segment（走各自的会话用量视图），其余通道都要能进费用页
        expected = {c for c in CallType.__args__ if c != "text"}  # pyright: ignore[reportAttributeAccessIssue]
        assert set(ACTUAL_COST_TYPES) == expected

    def test_usage_stats_counts_every_call_type(self):
        """用量汇总的分类计数必须覆盖全部通道。

        ``total_count`` 数的是全部，分类少一种就变成「总数含它、分类栏没有」——汇总静默少报，
        且没有任何一栏显示为 0 来提示。这张表曾是四条内联 SQL 字面量（没有名字，按表名 grep
        找不到），故改为按 CallType 驱动并在此锁死。
        """
        import inspect as _inspect

        from lib.db.repositories.usage_repo import _CALL_TYPE_VALUES, UsageRepository
        from lib.providers import CallType

        # 断言消费方由表驱动，而不是「表等于表」的同义反复
        src = _inspect.getsource(UsageRepository.get_stats)
        assert "_CALL_TYPE_VALUES" in src, "分类计数没有由 CallType 驱动"
        for call_type in CallType.__args__:  # pyright: ignore[reportAttributeAccessIssue]
            assert f'"{call_type}", 1' not in src, f"{call_type} 仍是写死的字面量"
        assert set(_CALL_TYPE_VALUES) == set(CallType.__args__)  # pyright: ignore[reportAttributeAccessIssue]

    def test_frontend_summary_lists_every_call_type(self):
        """前端汇总栏与后端分类计数同口径——少一块就是界面上看不到那类用量。"""
        import re as _re
        from pathlib import Path as _Path

        from lib.providers import CallType

        src = _Path("frontend/src/components/layout/UsageDrawer.tsx").read_text(encoding="utf-8")
        listed = set(_re.findall(r'"(\w+)"', src.split("const CALL_TYPES")[1].split("];")[0]))
        assert listed == set(CallType.__args__)  # pyright: ignore[reportAttributeAccessIssue]

    def test_project_level_channels_are_merged_into_totals(self):
        """项目级（无 segment）记账要并进 project_totals，否则那笔钱在费用页彻底不可见。"""
        import inspect as _inspect

        from server.services.cost_estimation import CostEstimationService

        src = _inspect.getsource(CostEstimationService)
        for channel in ("video", "audio", "music"):
            assert f'project_level_actual.get("{channel}"' in src, f"{channel} 的项目级记账没并进项目总计"


class TestAudioOptionBuckets:
    """三个音频下拉框各读各的桶——它们背后是三种互不兼容的协议。"""

    def test_options_expose_one_bucket_per_audio_capability(self):
        """每个 AudioCapability 都要有对应的选项桶，否则该能力的模型在设置页永远选不到。"""
        import inspect as _inspect

        from lib.audio_backends.base import AudioCapability
        from server.routers.system_config import _build_options

        src = _inspect.getsource(_build_options)
        for capability in AudioCapability:
            assert f"AudioCapability.{capability.name}" in src, f"{capability.name} 没有对应的选项桶"

    def test_frontend_option_type_matches_the_backend_buckets(self):
        """前端 GetSystemConfigResponse 的桶名与后端一致——漏一个就是空下拉框。"""
        from pathlib import Path as _Path

        src = _Path("frontend/src/types/system.ts").read_text(encoding="utf-8")
        for bucket in ("audio_backends", "music_backends", "singing_backends"):
            assert bucket in src, bucket


class TestTaskTypeCoverage:
    """每个可执行的 task_type 都要能发出完成事件——否则产物落盘了、前端不知道。"""

    def test_every_executor_has_a_completion_event(self):
        from server.services.generation_tasks import (
            _SKELETON_DRIVEN_TASK_ACTIONS,
            _TASK_CHANGE_SPECS,
            _TASK_EXECUTORS,
        )

        covered = set(_TASK_CHANGE_SPECS) | set(_SKELETON_DRIVEN_TASK_ACTIONS)
        missing = set(_TASK_EXECUTORS) - covered
        # image_edit 的完成事件由 image_edit_tasks 自行发出（它有自己的 diff 语义）。
        assert missing <= {"image_edit"}, f"这些任务类型跑完不发完成事件: {sorted(missing)}"

    def test_media_producing_tasks_report_asset_fingerprints(self):
        """产出文件的任务必须进指纹表，否则前端一直读缓存里的旧文件。"""
        from server.services.generation_tasks import compute_affected_fingerprints

        src = inspect.getsource(compute_affected_fingerprints)
        for task_type in ("storyboard", "video", "tts", "music", "singing", "grid", "reference_video"):
            assert f'"{task_type}"' in src, f"{task_type} 的产物不进资源指纹"
