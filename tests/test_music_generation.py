"""音乐生成链路：backend payload 契约、执行器、入队工具。"""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.audio_backends import (
    AudioCapability,
    MusicGenerationRequest,
    SingingSynthesisRequest,
    create_music_backend,
    get_registered_music_backends,
)
from lib.audio_backends.newapi_music import NewAPIMusicBackend, _resolve_task_type
from lib.resource_paths import resource_relative_path
from lib.video_backends.newapi import normalize_newapi_task_state


class TestMusicSubmitRetry:
    """提交阶段的重试必须与视频侧同档——同一个端点，两条链不该有不同的可靠性。

    ``submit_post`` 只负责把歧义态（请求可能已送达）转成不可重试的终态异常，它自己不重试；
    真正的重试由 ``with_retry_async(retry_if=should_retry_submit)`` 做。只用包装不加装饰器
    的话，连接建立失败这类「请求确定未送达」的瞬态错误会直接判整次生成失败。
    """

    @pytest.mark.unit
    def test_create_task_is_wrapped_with_submit_retry(self):
        from lib.audio_backends.newapi_music import NewAPIMusicBackend

        # 装饰器把原函数包起来后留下 __wrapped__；没有它说明只调了 submit_post
        assert hasattr(NewAPIMusicBackend._create_task, "__wrapped__")

    @pytest.mark.unit
    async def test_transient_connect_failure_is_retried(self):
        """连接建立失败重试而非直接失败——这类错误请求确定未送达，重试无重复计费风险。"""
        import httpx

        from lib.audio_backends.newapi_music import NewAPIMusicBackend

        attempts = 0

        class _Client:
            async def post(self, *a, **kw):  # noqa: ANN002, ANN003, ARG002
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise httpx.ConnectError("connection refused")
                return httpx.Response(200, json={"task_id": "t-1"}, request=httpx.Request("POST", "https://x"))

        backend = NewAPIMusicBackend(api_key="k", base_url="https://x/v1")
        task_id = await backend._create_task(_Client(), {"model": "m"})  # type: ignore[arg-type]
        assert task_id == "t-1"
        assert attempts == 3

    @pytest.mark.unit
    async def test_ambiguous_failure_is_not_retried(self):
        """读超时是歧义态：请求可能已建任务，重试会重复计费——必须一次就终态失败。"""
        import httpx

        from lib.audio_backends.newapi_music import NewAPIMusicBackend
        from lib.video_backends.base import AmbiguousSubmitError

        attempts = 0

        class _Client:
            async def post(self, *a, **kw):  # noqa: ANN002, ANN003, ARG002
                nonlocal attempts
                attempts += 1
                raise httpx.ReadTimeout("read timeout")

        backend = NewAPIMusicBackend(api_key="k", base_url="https://x/v1")
        with pytest.raises(AmbiguousSubmitError):
            await backend._create_task(_Client(), {"model": "m"})  # type: ignore[arg-type]
        assert attempts == 1


class TestLipSyncProviderRouting:
    """口型驱动任务的 provider 判定必须三处同源:入队派生 / worker 限流 / 执行层选模型。

    MV 演唱镜与普通镜同为 ``task_type="video"``，只有执行层跟着 is_performance 换模型的话，
    任务会排进常规视频供应商的并发池、实际请求打到口型驱动供应商——并发账目对不上、超发打爆
    自建网关；重启后 resume 还会按错的 provider 去锁一个已提交给另一家的任务。
    这类错配没有任何一条报错指向它。
    """

    @pytest.mark.unit
    def test_all_four_sites_share_the_predicate(self):
        """入队派生 / worker 限流 / 执行层 / 重启恢复——四处都要用同一判据。

        少任何一处的表现都是「排在 A 供应商的额度里、请求打到 B」，而没有任何一条报错指向它。
        """
        import inspect

        from lib.generation_queue import _derive_provider_id_for_enqueue
        from lib.generation_worker import _extract_provider
        from server.services.generation_tasks import _resolve_lip_sync_source
        from server.services.resume_executor import execute_resume_video_task

        assert "task_is_lip_sync" in inspect.getsource(_derive_provider_id_for_enqueue)
        assert "task_is_lip_sync" in inspect.getsource(_extract_provider)
        assert "task_is_lip_sync" in inspect.getsource(execute_resume_video_task)
        assert "item_is_lip_sync" in inspect.getsource(_resolve_lip_sync_source)

    @pytest.mark.unit
    def test_resume_declares_the_lip_sync_lane(self):
        """恢复路径要把判据接到 VideoLaneRequest 上，只算不用等于没算。"""
        import inspect

        from server.services.resume_executor import execute_resume_video_task

        assert "VideoLaneRequest(lip_sync=is_lip_sync)" in inspect.getsource(execute_resume_video_task)

    @pytest.mark.unit
    def test_both_provider_sites_resolve_the_lip_sync_backend(self):
        """光有判据不够——分流后必须真的去解析 lip_sync backend。"""
        import inspect

        from lib.generation_queue import _derive_provider_id_for_enqueue
        from lib.generation_worker import _extract_provider

        for fn in (_derive_provider_id_for_enqueue, _extract_provider):
            assert "resolve_lip_sync_backend(" in inspect.getsource(fn), fn.__name__

    @pytest.mark.unit
    def test_item_predicate_requires_both_mv_and_performance(self):
        from lib.lip_sync import item_is_lip_sync

        assert item_is_lip_sync({"content_mode": "mv"}, {"is_performance": True})
        # 非演唱镜传驱动音频会让画面主体被强行对口型
        assert not item_is_lip_sync({"content_mode": "mv"}, {"is_performance": False})
        assert not item_is_lip_sync({"content_mode": "ad"}, {"is_performance": True})
        assert not item_is_lip_sync(None, {"is_performance": True})

    @pytest.mark.unit
    def test_task_predicate_reads_the_script(self, tmp_path, monkeypatch):
        import lib.project_manager as pm_mod
        from lib.lip_sync import task_is_lip_sync
        from lib.project_manager import ProjectManager

        pm = ProjectManager(str(tmp_path))
        # 判定走全局 ProjectManager（生产上入队与 worker 都用它），测试里换成 tmp 根
        monkeypatch.setattr(pm_mod, "get_project_manager", lambda: pm)
        pm.create_project("mv-route")
        pm.create_project_metadata("mv-route", title="T", style="s", content_mode="mv")
        pm.save_script(
            "mv-route",
            {
                "content_mode": "mv",
                "title": "T",
                "song": {"duration_seconds": 90},
                "lyrics": "词",
                "shots": [
                    {"shot_id": "E1S01", "is_performance": True},
                    {"shot_id": "E1S02", "is_performance": False},
                ],
            },
            "episode_1.json",
            validate=False,
        )
        project = pm.load_project("mv-route")
        payload = {"script_file": "episode_1.json"}
        assert task_is_lip_sync("mv-route", project, payload, "E1S01")
        assert not task_is_lip_sync("mv-route", project, payload, "E1S02")
        # 拿不准就别硬塞：条目未命中 / 缺 script_file 一律按常规视频模型
        assert not task_is_lip_sync("mv-route", project, payload, "E1S99")
        assert not task_is_lip_sync("mv-route", project, {}, "E1S01")


class TestMusicAccounting:
    """音乐 / 歌声也要记账——生成路径少一条记账就是费用页少一块。

    这两条路径不经 MediaGenerator（音乐是项目级单件产物），于是很容易漏掉记账括号：
    产物照常落盘、任务照常成功，只有费用页对不上，而「用量对不上」最难定位到具体是哪条路径。
    """

    @pytest.mark.unit
    def test_both_executors_open_a_ledger_bracket(self):
        import inspect

        from server.services.generation_tasks import execute_music_task, execute_singing_task

        for fn in (execute_music_task, execute_singing_task):
            src = inspect.getsource(fn)
            assert "_ledger().record(" in src, f"{fn.__name__} 未开记账括号"
            assert "call.success(" in src, f"{fn.__name__} 未递交结算对象"

    @pytest.mark.unit
    def test_music_settles_by_duration_not_characters(self):
        """音乐按产出时长计价；套用 TTS 的按字符计价会让费用恒为 0（音乐没有字符数）。"""
        from lib.audio_backends import MusicGenerationResult
        from lib.ledger import _settlement_from_result

        result = MusicGenerationResult(
            provider="custom-1", model="acestep", output_path=Path("x.wav"), duration_seconds=92.4
        )
        settlement = _settlement_from_result("music", result)
        assert settlement.billed_duration_seconds == 92
        assert settlement.usage_tokens is None

    @pytest.mark.unit
    def test_missing_duration_settles_to_zero_not_a_guess(self):
        """provider 漏报时长按 0 计，不拿申请值近似——那会把估算悄悄写成实际支出。"""
        from lib.audio_backends import MusicGenerationResult
        from lib.ledger import _settlement_from_result

        result = MusicGenerationResult(provider="custom-1", model="acestep", output_path=Path("x.wav"))
        assert _settlement_from_result("music", result).billed_duration_seconds is None

    @pytest.mark.unit
    def test_custom_provider_music_cost_is_per_second(self):
        from lib.cost_calculator import cost_calculator
        from lib.pricing.strategies import PricingParams

        amount, currency = cost_calculator.calculate_cost(
            "custom-1",
            PricingParams(call_type="music", model="acestep", duration_seconds=90),
            custom_price_input=0.01,
            custom_currency="CNY",
        )
        assert amount == pytest.approx(0.9)
        assert currency == "CNY"


class TestAudioProtocolSeparation:
    """``media_type="audio"`` 下面装着三种互不兼容的协议，选项与执行两层都要分开。

    TTS 只有 ``synthesize``、作曲只有 ``generate_music``、歌声只有 ``synthesize_singing``。
    只按 media_type 分类会让三个下拉框互相提供对方用不了的模型，配错的表现是执行期
    ``AttributeError``——错误信息里完全看不出是配错了模型。
    """

    @pytest.mark.unit
    def test_every_audio_endpoint_declares_its_capabilities(self):
        from lib.custom_provider.endpoints import ENDPOINT_REGISTRY

        for key, spec in ENDPOINT_REGISTRY.items():
            if spec.media_type == "audio":
                assert spec.audio_capabilities, f"audio endpoint {key} 未声明 audio_capabilities"
            else:
                assert spec.audio_capabilities is None, f"非 audio endpoint {key} 不该声明 audio_capabilities"

    @pytest.mark.unit
    def test_music_and_tts_endpoints_do_not_overlap(self):
        """作曲 endpoint 不得被当成 TTS 可选项，反之亦然。"""
        from lib.audio_backends.base import AudioCapability
        from lib.custom_provider.endpoints import endpoint_supports_audio_capability

        assert endpoint_supports_audio_capability("newapi-music", AudioCapability.TEXT_TO_MUSIC)
        assert endpoint_supports_audio_capability("newapi-music", AudioCapability.SINGING_SYNTHESIS)
        assert not endpoint_supports_audio_capability("newapi-music", AudioCapability.TEXT_TO_SPEECH)

        assert endpoint_supports_audio_capability("openai-tts", AudioCapability.TEXT_TO_SPEECH)
        assert not endpoint_supports_audio_capability("openai-tts", AudioCapability.TEXT_TO_MUSIC)

        # 非 audio endpoint 不抛、返回 False（选项分桶要遍历全部 endpoint）
        assert not endpoint_supports_audio_capability("newapi-video", AudioCapability.TEXT_TO_MUSIC)

    @pytest.mark.unit
    async def test_tts_rejects_a_music_backend_with_an_actionable_error(self, tmp_path: Path):
        """把作曲模型配成旁白模型时报「不具备语音合成能力」，不是 AttributeError。"""
        from lib.media_generator import MediaGenerator

        gen = MediaGenerator(
            project_path=tmp_path,
            audio_backend=NewAPIMusicBackend(api_key="k", base_url="https://x/v1"),  # type: ignore[arg-type]
            audio_provider_id="custom-1",
        )

        with pytest.raises(ValueError, match="语音合成能力"):
            await gen.generate_audio_async(text="旁白", resource_id="E1S01", voice="Cherry")

    @pytest.mark.unit
    def test_music_and_singing_tasks_guard_their_capability(self):
        """两个执行器都要在调用前查能力——singing 早有这道守卫，music 曾经没有。"""
        import inspect

        from server.services.generation_tasks import execute_music_task, execute_singing_task

        assert "_require_audio_capability(" in inspect.getsource(execute_music_task)
        assert "_require_audio_capability(" in inspect.getsource(execute_singing_task)


class TestMusicBackendRegistry:
    @pytest.mark.unit
    def test_music_backend_registered_separately_from_tts(self):
        # TTS 与音乐分表：同一 provider 名下可能只有其一，共用一张表会拿到不满足协议的实例。
        assert "newapi" in get_registered_music_backends()

    @pytest.mark.unit
    def test_create_music_backend(self):
        backend = create_music_backend("newapi", api_key="k", base_url="https://x/v1")
        # 同一 backend 承载作曲与歌声合成：两者共用门面的提交/轮询链，只是 task_type 不同
        assert backend.capabilities == {
            AudioCapability.TEXT_TO_MUSIC,
            AudioCapability.SINGING_SYNTHESIS,
        }
        assert backend.model == "acestep-v15-xl-turbo"

    @pytest.mark.unit
    def test_provider_name_attributes_to_custom_provider(self):
        # 记账与日志要归因到用户配的 provider，而非内置 newapi。
        backend = NewAPIMusicBackend(api_key="k", base_url="https://x/v1", provider_name="custom-1")
        assert backend.name == "custom-1"


class TestMusicPayload:
    def _backend(self) -> NewAPIMusicBackend:
        return NewAPIMusicBackend(api_key="k", base_url="https://x/v1")

    @pytest.mark.unit
    def test_t2m_payload_declares_task_type(self, tmp_path: Path):
        payload = self._backend()._build_payload(
            MusicGenerationRequest(prompt="舒缓钢琴", output_path=tmp_path / "m.wav")
        )
        assert payload["model"] == "acestep-v15-xl-turbo"
        assert payload["prompt"] == "舒缓钢琴"
        assert payload["metadata"]["task_type"] == "t2m"
        # 时长未指定时不发顶层 duration，交由引擎决定
        assert "duration" not in payload

    @pytest.mark.unit
    def test_duration_goes_to_metadata_audio_duration(self, tmp_path: Path):
        """时长走 metadata.audio_duration，不是顶层 duration。

        顶层 duration 是视频任务的受控字段，ACE-Step 读的是 metadata.audio_duration——
        发错位置不会报错，只是时长静默不生效，拿到一首长度不对的曲子。
        """
        payload = self._backend()._build_payload(
            MusicGenerationRequest(prompt="p", output_path=tmp_path / "m.wav", duration_seconds=30)
        )
        assert payload["metadata"]["audio_duration"] == 30
        assert "duration" not in payload

    @pytest.mark.unit
    def test_lyrics_switch_off_sample_mode(self, tmp_path: Path):
        """给了歌词就按词唱；没给才让引擎自动作词。

        MV 的歌词是用户定稿的，若被 sample 模式覆盖成引擎自编的词，就与剧本里排好的
        lyrics_line 对不上——镜头逐句对歌词，错一句后面全错。
        """
        with_lyrics = self._backend()._build_payload(
            MusicGenerationRequest(prompt="民谣", output_path=tmp_path / "m.wav", lyrics="第一句\n第二句")
        )
        assert with_lyrics["metadata"]["lyrics"] == "第一句\n第二句"
        assert "sample_mode" not in with_lyrics["metadata"]

        without = self._backend()._build_payload(MusicGenerationRequest(prompt="民谣", output_path=tmp_path / "m.wav"))
        assert without["metadata"]["sample_mode"] is True
        assert without["metadata"]["sample_query"] == "民谣"

    @pytest.mark.unit
    def test_optional_engine_params(self, tmp_path: Path):
        payload = self._backend()._build_payload(
            MusicGenerationRequest(prompt="p", output_path=tmp_path / "m.wav", bpm=90, vocal_language="chinese")
        )
        assert payload["metadata"]["bpm"] == 90
        assert payload["metadata"]["vocal_language"] == "chinese"

    @pytest.mark.unit
    def test_reference_audio_switches_to_cover(self, tmp_path: Path):
        ref = tmp_path / "ref.wav"
        ref.write_bytes(b"RIFFfake")
        payload = self._backend()._build_payload(
            MusicGenerationRequest(prompt="p", output_path=tmp_path / "m.wav", reference_audio=ref)
        )
        assert payload["metadata"]["task_type"] == "cover"
        assert payload["metadata"]["reference_audio"].startswith("data:audio/wav;base64,")

    @pytest.mark.unit
    def test_missing_reference_audio_fails_loud(self, tmp_path: Path):
        # 静默跳过会让翻唱退化成随机作曲——用户拿到一首无关的曲子且照常计费。
        with pytest.raises(FileNotFoundError):
            self._backend()._build_payload(
                MusicGenerationRequest(
                    prompt="p", output_path=tmp_path / "m.wav", reference_audio=tmp_path / "nope.wav"
                )
            )

    @pytest.mark.unit
    def test_task_type_resolution(self, tmp_path: Path):
        assert _resolve_task_type(MusicGenerationRequest(prompt="p", output_path=tmp_path / "m.wav")) == "t2m"


class TestStaleAudioSiblings:
    """同一 resource_id 只应留下一个文件——换了产出格式不同的模型时，新产物写在旧文件**旁边**。

    读取侧（导出、口型驱动、指纹）按固定优先级 ``(.wav, .mp3)`` 遍历候选，旧的 ``.wav`` 一直
    赢过新的 ``.mp3``：任务报告了新路径，成片却用着上一个模型留下的音轨，两边说法不一。
    清理放在写侧而非读侧——读侧加「按新鲜度选」只是让两个文件继续并存然后猜。
    """

    @pytest.mark.unit
    def test_regenerating_with_new_format_removes_the_old_file(self, tmp_path: Path):
        from lib.audio_backends.newapi_music import _drop_stale_siblings

        stale = tmp_path / "main.wav"
        stale.write_bytes(b"old-wav")
        fresh = tmp_path / "main.mp3"
        fresh.write_bytes(b"new-mp3")

        _drop_stale_siblings(fresh)

        assert not stale.exists()
        assert fresh.read_bytes() == b"new-mp3"

    @pytest.mark.unit
    def test_reader_priority_would_pick_the_stale_file_without_cleanup(self, tmp_path: Path):
        """反向确认这条清理确实有用：不清理时读取侧选中的就是旧文件。"""
        from lib.resource_paths import resource_candidate_paths

        (tmp_path / "music").mkdir()
        (tmp_path / "music/main.wav").write_bytes(b"old")
        (tmp_path / "music/main.mp3").write_bytes(b"new")

        first_hit = next(rel for rel in resource_candidate_paths("music", "main") if (tmp_path / rel).exists())
        assert first_hit == "music/main.wav"  # 固定优先级下旧格式在前

    @pytest.mark.unit
    def test_keeps_the_file_just_written(self, tmp_path: Path):
        """当前产物本身不能被误删——扩展名恰好等于默认值时清理逻辑会遍历到它自己。"""
        from lib.audio_backends.newapi_music import _drop_stale_siblings

        kept = tmp_path / "vocal_main.wav"
        kept.write_bytes(b"fresh")
        _drop_stale_siblings(kept)
        assert kept.read_bytes() == b"fresh"

    @pytest.mark.unit
    def test_cleanup_failure_does_not_fail_the_task(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """产物已经落好，为删不掉旧文件而让整个生成任务失败不划算——告警即可。"""
        from lib.audio_backends.newapi_music import _drop_stale_siblings

        (tmp_path / "main.wav").write_bytes(b"old")
        fresh = tmp_path / "main.mp3"
        fresh.write_bytes(b"new")
        monkeypatch.setattr(Path, "unlink", lambda self, **kw: (_ for _ in ()).throw(OSError("locked")))

        _drop_stale_siblings(fresh)  # 不抛

    @pytest.mark.unit
    def test_both_download_paths_clean_up(self):
        """守连接点：作曲与歌声两条下载路径都要清理，只改一条会让另一条继续留下双份。"""
        import inspect

        from lib.audio_backends.newapi_music import NewAPIMusicBackend

        for method in (NewAPIMusicBackend.generate_music, NewAPIMusicBackend.synthesize_singing):
            assert "_drop_stale_siblings(" in inspect.getsource(method), method.__name__


class TestStaleVocalAgainstMusic:
    """人声轨派生自作曲产物，作曲重跑过它就作废。

    现实顺序：作曲 → 歌声合成 → 觉得曲子不行 → 重新作曲 → 直接导出。用户忘了重跑歌声合成，
    而两处读取侧都无条件「人声轨优先」，于是成片配的是上一版曲子的人声、演唱镜口型对的是旧
    旋律——ArcReel 内部完全看不出来（分镜、视频、字幕都对）。

    两处的决策**故意不同**：导出退回作曲产物（可撤销，且用户一听嗓子不对就会回去补跑），
    口型驱动直接拦住（口型会烧进视频文件，纠正要再花一次视频生成的钱）。
    """

    @staticmethod
    def _write_pair(root: Path, *, vocal_first: bool) -> None:
        import os

        (root / "music").mkdir(parents=True, exist_ok=True)
        music, vocal = root / "music/main.wav", root / "music/vocal_main.wav"
        music.write_bytes(b"m")
        vocal.write_bytes(b"v")
        older, newer = (vocal, music) if vocal_first else (music, vocal)
        os.utime(older, (1_700_000_000, 1_700_000_000))
        os.utime(newer, (1_700_000_100, 1_700_000_100))

    @pytest.mark.unit
    def test_predicate_compares_generation_time(self, tmp_path: Path):
        import os

        from lib.resource_paths import is_outdated_by

        src, derived = tmp_path / "main.wav", tmp_path / "vocal_main.wav"
        src.write_bytes(b"m")
        derived.write_bytes(b"v")

        os.utime(derived, (1_700_000_000, 1_700_000_000))
        os.utime(src, (1_700_000_100, 1_700_000_100))
        assert is_outdated_by(derived, src) is True

        os.utime(derived, (1_700_000_200, 1_700_000_200))
        assert is_outdated_by(derived, src) is False

    @pytest.mark.unit
    def test_equal_mtime_is_not_stale(self, tmp_path: Path):
        """同一次流水线里先后落盘的两个文件可能落在同一秒，不能判过期。"""
        import os

        from lib.resource_paths import is_outdated_by

        src, derived = tmp_path / "main.wav", tmp_path / "vocal_main.wav"
        src.write_bytes(b"m")
        derived.write_bytes(b"v")
        os.utime(src, (1_700_000_000, 1_700_000_000))
        os.utime(derived, (1_700_000_000, 1_700_000_000))

        assert is_outdated_by(derived, src) is False

    @pytest.mark.unit
    def test_missing_file_does_not_block(self, tmp_path: Path):
        """判定本身失败（文件被挪走）不该让导出或生成停摆。"""
        from lib.resource_paths import is_outdated_by

        derived = tmp_path / "vocal_main.wav"
        derived.write_bytes(b"v")
        assert is_outdated_by(derived, tmp_path / "gone.wav") is False

    @pytest.mark.unit
    def test_export_falls_back_to_composition(self, tmp_path: Path):
        from server.services.jianying_draft_service import _resolve_music_track

        self._write_pair(tmp_path, vocal_first=True)
        resolved = _resolve_music_track(tmp_path)
        assert resolved is not None and resolved.name == "main.wav"

    @pytest.mark.unit
    def test_export_still_prefers_a_current_vocal(self, tmp_path: Path):
        """反向：人声轨是新的时仍然优先它——退回逻辑不能把正常情况一起改坏。"""
        from server.services.jianying_draft_service import _resolve_music_track

        self._write_pair(tmp_path, vocal_first=False)
        resolved = _resolve_music_track(tmp_path)
        assert resolved is not None and resolved.name == "vocal_main.wav"

    @pytest.mark.unit
    def test_lip_sync_refuses_stale_vocal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import server.services.generation_tasks as gt

        self._write_pair(tmp_path, vocal_first=True)
        monkeypatch.setattr(gt, "item_is_lip_sync", lambda _project, _item: True)

        with pytest.raises(ValueError, match="比作曲产物"):
            gt._resolve_lip_sync_source({}, tmp_path, object())

    @pytest.mark.unit
    def test_lip_sync_accepts_a_current_vocal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import server.services.generation_tasks as gt

        self._write_pair(tmp_path, vocal_first=False)
        monkeypatch.setattr(gt, "item_is_lip_sync", lambda _project, _item: True)

        resolved = gt._resolve_lip_sync_source({}, tmp_path, object())
        assert resolved is not None and resolved.name == "vocal_main.wav"

    @pytest.mark.unit
    def test_both_readers_check_staleness(self):
        """守连接点：两处「人声轨优先」的读取侧都要判过期，只改一处另一处继续用旧轨。"""
        import inspect

        from server.services.generation_tasks import _resolve_lip_sync_source
        from server.services.jianying_draft_service import _resolve_music_track

        for fn in (_resolve_lip_sync_source, _resolve_music_track):
            assert "is_outdated_by(" in inspect.getsource(fn), fn.__name__


class TestAudioMimeSingleSource:
    """音频 MIME 只能有一张表。

    曾经作曲侧按扩展名取 MIME、口型侧另写一份硬编码 ``data:audio/wav``——同一条规则两份实现，
    改了一处另一处继续贴错标签。现在两侧都走 ``lib.resource_paths.audio_data_uri``。
    """

    @pytest.mark.unit
    def test_lip_sync_labels_mp3_correctly(self, tmp_path: Path):
        from lib.video_backends.newapi import NewAPIVideoBackend

        backend = NewAPIVideoBackend(api_key="k", base_url="https://x", model="infinitetalk-480p")
        mp3 = tmp_path / "driving.mp3"
        mp3.write_bytes(b"ID3")
        metadata: dict = {}

        backend._apply_lip_sync(metadata, mp3)

        assert metadata["audio"].startswith("data:audio/mpeg;base64,")

    @pytest.mark.unit
    def test_no_hardcoded_audio_mime_remains(self):
        """守连接点：任何模块里再写死 ``data:audio/`` 字面量都会重新引入漂移。"""
        import lib.audio_backends.newapi_music as music_mod
        import lib.video_backends.newapi as video_mod

        for mod in (music_mod, video_mod):
            assert "data:audio/" not in Path(mod.__file__).read_text(encoding="utf-8"), mod.__name__

    @pytest.mark.unit
    def test_mime_keys_match_audio_extensions(self):
        """两张表的键集必须一致：候选路径认的格式、编码器就得认。"""
        from lib.resource_paths import AUDIO_EXTENSIONS, AUDIO_MIME_BY_SUFFIX

        assert set(AUDIO_MIME_BY_SUFFIX) == set(AUDIO_EXTENSIONS)


class TestAudioExtensionFidelity:
    """落盘扩展名必须跟随实际产物——同一 backend 下不同模型格式不同。

    实测：ACE-Step 作曲产物是 .mp3，SoulX-Singer 歌声合成产物是 .wav。写死一种的后果有两处：
    后续按后缀推导 MIME 会给门面贴错标签（错误发生在引擎侧、指不回标签这一层），以及
    播放器/剪辑器按后缀选解码器。
    """

    @pytest.mark.unit
    def test_extension_follows_result_url(self, tmp_path: Path):
        from lib.audio_backends.newapi_music import _output_with_real_ext

        base = tmp_path / "main.wav"
        mp3_url = "https://obs.example.com/t2m-ace-step/2026/08/03/1/abc.mp3?AccessKeyId=x&Expires=1"
        assert _output_with_real_ext(base, mp3_url).name == "main.mp3"

        wav_url = "https://obs.example.com/svs-soulx-singer/2026/08/03/1/def.wav?Signature=y"
        assert _output_with_real_ext(base, wav_url).name == "main.wav"

    @pytest.mark.unit
    def test_unknown_extension_keeps_caller_default(self, tmp_path: Path):
        """URL 认不出扩展名时保留调用方给的默认值，不臆造一个。"""
        from lib.audio_backends.newapi_music import _output_with_real_ext

        base = tmp_path / "main.wav"
        assert _output_with_real_ext(base, "https://obs.example.com/no-ext-here").name == "main.wav"

    @pytest.mark.unit
    def test_readers_search_all_candidate_extensions(self):
        """写侧按实际格式落盘，读侧就必须按候选找——否则「文件明明生成了却读不到」。"""
        from lib.resource_paths import resource_candidate_paths

        assert resource_candidate_paths("music", "main") == ("music/main.wav", "music/main.mp3")
        assert resource_candidate_paths("singing", "main") == ("music/vocal_main.wav", "music/vocal_main.mp3")
        # 非音频类只有一种形状，调用方无需分支
        assert resource_candidate_paths("videos", "E1S01") == ("videos/scene_E1S01.mp4",)

    @pytest.mark.unit
    def test_all_audio_readers_use_candidates(self):
        """守连接点：三处读取侧都要按候选找，认死默认扩展名就会漏掉另一格式。"""
        import inspect

        from server.services.generation_tasks import _resolve_lip_sync_source, compute_affected_fingerprints
        from server.services.jianying_draft_service import _first_existing_audio

        for fn in (_resolve_lip_sync_source, compute_affected_fingerprints, _first_existing_audio):
            assert "resource_candidate_paths(" in inspect.getsource(fn), fn.__name__

    @pytest.mark.unit
    def test_both_backend_paths_apply_the_real_extension(self):
        """守连接点：作曲与歌声**两条**下载路径都要按真实扩展名落盘。

        只改一条的表现是「作曲对了、歌声还错着」——而两者格式恰好不同（.mp3 / .wav），
        漏掉的那条会一直把内容与后缀不符的文件写进项目。
        """
        import inspect

        from lib.audio_backends.newapi_music import NewAPIMusicBackend

        for method in (NewAPIMusicBackend.generate_music, NewAPIMusicBackend.synthesize_singing):
            src = inspect.getsource(method)
            assert "_output_with_real_ext(request.output_path, url)" in src, method.__name__
            # 不得再拿请求时假定的路径去下载
            assert "self._download_with_retry(url, request.output_path)" not in src, method.__name__

    @pytest.mark.unit
    def test_executors_report_the_real_landed_path(self):
        """执行器要报 backend 实际落盘的路径，不是请求时假定的那个。"""
        import inspect

        from server.services.generation_tasks import execute_music_task, execute_singing_task

        for fn in (execute_music_task, execute_singing_task):
            src = inspect.getsource(fn)
            assert "result.output_path.relative_to(project_path)" in src, fn.__name__


class TestAudioEncoding:
    """data-uri 的 MIME 必须如实反映字节内容——标签贴错的失败发生在引擎侧，指不回真因。"""

    @pytest.mark.unit
    def test_mime_follows_suffix(self, tmp_path: Path):
        from lib.audio_backends.newapi_music import _encode_reference_audio

        wav = tmp_path / "voice.wav"
        wav.write_bytes(b"RIFFfake")
        assert _encode_reference_audio(wav).startswith("data:audio/wav;base64,")

        # 上传路由同时接受 .mp3（character_audio_ref），编码侧必须原样认它
        mp3 = tmp_path / "voice.MP3"  # 大小写不敏感
        mp3.write_bytes(b"ID3fake")
        assert _encode_reference_audio(mp3).startswith("data:audio/mpeg;base64,")

    @pytest.mark.unit
    def test_unknown_suffix_fails_loud(self, tmp_path: Path):
        """贴一个猜的标签比报错更难排查——未知格式在编码阶段就拦。"""
        from lib.audio_backends.newapi_music import _encode_reference_audio

        m4a = tmp_path / "voice.m4a"
        m4a.write_bytes(b"fake")
        with pytest.raises(ValueError, match="m4a"):
            _encode_reference_audio(m4a)

    @pytest.mark.unit
    def test_mime_map_covers_the_upload_whitelist(self):
        """编码表与上传白名单同口径：上传放进来的格式，编码侧必须都认识。

        白名单加了新格式而编码表没跟上的话，用户能传上去、一用就报「不支持」。
        """
        from lib.resource_paths import AUDIO_MIME_BY_SUFFIX
        from server.routers.files import ALLOWED_EXTENSIONS

        upload_exts = set(ALLOWED_EXTENSIONS["character_audio_ref"])
        assert upload_exts <= set(AUDIO_MIME_BY_SUFFIX), upload_exts - set(AUDIO_MIME_BY_SUFFIX)


class TestSingingSynthesis:
    def _backend(self) -> NewAPIMusicBackend:
        return NewAPIMusicBackend(api_key="k", base_url="https://x/v1", model="soulx-singer")

    def _wav(self, tmp_path: Path, name: str) -> Path:
        p = tmp_path / name
        p.write_bytes(b"RIFFfake")
        return p

    @pytest.mark.unit
    async def test_svs_payload_carries_both_audios(self, tmp_path: Path, monkeypatch):
        """两段音频语义不同、都必发：prompt_audio 是音色、target_audio 是旋律。"""
        captured: dict = {}

        async def fake_create(self, client, payload):  # noqa: ANN001
            captured.update(payload)
            raise RuntimeError("stop-after-payload")

        monkeypatch.setattr(NewAPIMusicBackend, "_create_task", fake_create)
        backend = self._backend()
        with pytest.raises(RuntimeError, match="stop-after-payload"):
            await backend.synthesize_singing(
                SingingSynthesisRequest(
                    voice_reference=self._wav(tmp_path, "voice.wav"),
                    target_song=self._wav(tmp_path, "song.wav"),
                    output_path=tmp_path / "out.wav",
                )
            )

        assert captured["metadata"]["task_type"] == "svs"
        assert captured["metadata"]["prompt_audio"].startswith("data:audio/wav;base64,")
        assert captured["metadata"]["target_audio"].startswith("data:audio/wav;base64,")
        # 门面对 svs 的 prompt 仅作占位，但显式带上让日志能一眼看出任务性质
        assert captured["prompt"] == "soulx-singer"

    @pytest.mark.unit
    async def test_missing_audio_fails_before_submit(self, tmp_path: Path):
        # 缺输入时引擎会产出一段无关音频并照常计费，故在编码阶段就拦下
        with pytest.raises(FileNotFoundError):
            await self._backend().synthesize_singing(
                SingingSynthesisRequest(
                    voice_reference=tmp_path / "nope.wav",
                    target_song=self._wav(tmp_path, "song.wav"),
                    output_path=tmp_path / "out.wav",
                )
            )


class TestSingingEnqueue:
    """入队守卫必须认识 singing 这个 task_type——backend 会做不等于任务进得去队列。

    上一版的歌声测试全部直调 backend，绕开了 ``TaskSpec.from_request`` 这个入队唯一守卫点：
    backend 单测全绿，而 ``generate_singing`` 每一次调用都在入队前就被 prompt 校验挡掉。
    """

    @pytest.mark.unit
    def test_singing_spec_builds_without_a_prompt(self):
        from lib.generation_queue_client import TaskSpec

        spec = TaskSpec.from_request(
            task_type="singing",
            media_type="audio",
            resource_id="main",
            extra_payload={"voice_reference": "characters/a.wav", "target_song": "music/main.wav"},
        )
        assert spec.payload is not None
        assert spec.payload["voice_reference"] == "characters/a.wav"

    @pytest.mark.unit
    def test_singing_rejects_a_prompt(self):
        """svs 结构上没有 prompt：喂进去的文字不会被引擎读取，却会让人以为控制得住产出。"""
        from lib.generation_queue_client import TaskSpec, TaskSpecValidationError

        with pytest.raises(TaskSpecValidationError) as exc:
            TaskSpec.from_request(
                task_type="singing",
                media_type="audio",
                resource_id="main",
                prompt="唱得深情一点",
            )
        assert exc.value.code == "prompt_not_accepted"

    @pytest.mark.unit
    def test_music_still_requires_its_prompt(self):
        """放行只针对 singing：作曲的 prompt 是曲风描述，是真会被引擎读的。"""
        from lib.generation_queue_client import TaskSpec, TaskSpecValidationError

        with pytest.raises(TaskSpecValidationError):
            TaskSpec.from_request(task_type="music", media_type="audio", resource_id="main")

    @pytest.mark.unit
    async def test_generate_singing_tool_reaches_the_queue(self, tmp_path: Path, monkeypatch):
        """跑通工具处理函数本身：入队被守卫挡掉时，这条会以 is_error 报红。"""
        from lib.project_manager import ProjectManager
        from server.agent_runtime.sdk_tools._context import ToolContext
        from server.agent_runtime.sdk_tools.enqueue_singing import generate_singing_tool

        captured: list = []

        async def fake_batch(*, project_name, specs, **kwargs):  # noqa: ANN001, ARG001
            captured.extend(specs)
            return [type("S", (), {"result": {"file_path": "music/vocal_main.wav", "duration_seconds": 92.0}})()], []

        monkeypatch.setattr("server.agent_runtime.sdk_tools.enqueue_singing.batch_enqueue_and_wait", fake_batch)

        pm = ProjectManager(str(tmp_path / "projects"))
        pm.create_project("demo")
        ctx = ToolContext(project_name="demo", projects_root=tmp_path / "projects", pm=pm)
        result = await generate_singing_tool(ctx).handler(
            {"voice_reference": "characters/a.wav", "target_song": "music/main.wav"}
        )

        assert result.get("is_error") is not True, result["content"][0]["text"]
        assert len(captured) == 1 and captured[0].task_type == "singing"


class TestLipSyncDispatch:
    @pytest.mark.unit
    def test_driving_audio_triggers_s2v(self, tmp_path: Path):
        """驱动音频（口型）与参考音频（音色）语义不同，走独立字段与独立 task_type。"""
        from lib.video_backends.newapi import NewAPIVideoBackend, supports_lip_sync

        audio = tmp_path / "vocal.wav"
        audio.write_bytes(b"RIFFfake")
        backend = NewAPIVideoBackend(api_key="k", base_url="https://x/v1", model="infinitetalk-720p")
        metadata: dict = {}
        backend._apply_lip_sync(metadata, audio)

        assert metadata["task_type"] == "s2v"
        assert metadata["audio"].startswith("data:audio/wav;base64,")
        assert supports_lip_sync("infinitetalk-720p") is True
        assert supports_lip_sync("wan2.2-i2v") is False

    @pytest.mark.unit
    def test_missing_driving_audio_fails_loud(self, tmp_path: Path):
        # 静默跳过会产出一段口型乱动的视频——比直接报错更难发现
        from lib.video_backends.newapi import NewAPIVideoBackend

        backend = NewAPIVideoBackend(api_key="k", base_url="https://x/v1", model="infinitetalk-720p")
        with pytest.raises(FileNotFoundError):
            backend._apply_lip_sync({}, tmp_path / "nope.wav")


class TestMusicProviderRouting:
    """music / singing 的 provider 解析不能落到 TTS 那条 lane。"""

    @pytest.mark.unit
    def test_queue_derivation_checks_music_before_audio(self):
        """两处判定里 is_music 必须排在 is_audio 之前。

        music/singing 的 media_type 就是 audio（共用同一条 worker 并发通道），
        排在后面永远命中不到，会用 TTS 的 provider 去认领与限流——TTS 在厂商托管、
        音乐在自建网关时，音乐任务会去占 TTS 供应商的并发额度。
        """
        import inspect

        from lib.generation_queue import _derive_provider_id_for_enqueue

        src = inspect.getsource(_derive_provider_id_for_enqueue)
        assert src.index("is_music") < src.index("is_audio = ")
        assert src.index("elif is_music") < src.index("elif is_audio")

    @pytest.mark.unit
    def test_worker_extraction_checks_music_before_audio(self):
        import inspect

        from lib.generation_worker import _extract_provider

        src = inspect.getsource(_extract_provider)
        assert src.index("is_music") < src.index("is_audio = ")
        assert src.index("elif is_music") < src.index("elif is_audio")

    @pytest.mark.unit
    def test_singing_resolves_a_separate_model_from_composition(self):
        """作曲与歌声是两个模型，配置键必须分开。

        ACE-Step 只会作曲、SoulX-Singer 只会唱；共用一个配置会把 svs 请求发给
        只会作曲的模型，而调用侧的 hasattr 检查拦不住——同一 backend 类承载两种能力，
        方法恒在，差异在模型上。
        """
        from lib.config.resolver import _MUSIC_TASK_SETTING_KEYS

        music = _MUSIC_TASK_SETTING_KEYS["music"]
        singing = _MUSIC_TASK_SETTING_KEYS["singing"]
        # 四个查找位点都必须分开，否则两种能力仍会共用同一份配置
        assert music.setting_key != singing.setting_key
        assert music.project_field != singing.project_field
        assert music.payload_provider != singing.payload_provider
        assert music.payload_model != singing.payload_model

    @pytest.mark.unit
    def test_no_builtin_default_points_at_unregistered_provider(self):
        """默认值必须为空，不能指向构造不出 backend 的裸 provider id。

        ACE-Step / SoulX-Singer 都经**自定义供应商**接入（provider_id 形如 custom-N）；
        ``newapi`` 这个裸 id 不在 PROVIDER_REGISTRY 里，assemble_backend 会抛
        「no builtin ProviderSpec」——一句用户看不懂的话。留空让「未配置」可判定，
        由执行器给出可操作的指引。
        """
        from lib.config.registry import PROVIDER_REGISTRY
        from lib.config.resolver import _MUSIC_TASK_SETTING_KEYS

        for keys in _MUSIC_TASK_SETTING_KEYS.values():
            assert keys.default_value == ""
        assert "newapi" not in PROVIDER_REGISTRY


class TestLipSyncWiring:
    """口型驱动必须在**编排层**接通，不能只有 backend 会消费。

    上一版只到 backend 就停了：字段有、backend 会读，但没有任何生产代码写入它，
    演唱镜头照走普通图生视频、口型对不上。测试直接调 backend 私有方法则全绿——
    绕过编排链的测试证明不了链路通。
    """

    @pytest.mark.unit
    def test_media_generator_accepts_driving_audio(self):
        import inspect

        from lib.media_generator import MediaGenerator

        for method in (MediaGenerator.generate_video, MediaGenerator.generate_video_async):
            assert "driving_audio" in inspect.signature(method).parameters, method.__name__

    @pytest.mark.unit
    def test_media_generator_forwards_driving_audio_to_request(self):
        import inspect

        from lib.media_generator import MediaGenerator

        src = inspect.getsource(MediaGenerator.generate_video_async)
        assert "driving_audio=driving_audio" in src

    @pytest.mark.unit
    def test_video_task_derives_driving_audio_only_for_mv_performance_shots(self, tmp_path: Path):
        from server.services.generation_tasks import _resolve_lip_sync_source

        # 非 MV 项目：任何镜头都不走口型驱动
        assert _resolve_lip_sync_source({"content_mode": "ad"}, tmp_path, {"is_performance": True}) is None
        # MV 但非演唱镜头（氛围镜/空镜）：传驱动音频反而会强行给画面主体对口型
        assert _resolve_lip_sync_source({"content_mode": "mv"}, tmp_path, {"is_performance": False}) is None

    @pytest.mark.unit
    def test_missing_vocal_track_fails_loud(self, tmp_path: Path):
        """歌声轨缺失时报错而非降级。

        降级的后果是演唱镜头照常出片、口型却对不上——看成片才发现，且看不出原因。
        """
        from server.services.generation_tasks import _resolve_lip_sync_source

        with pytest.raises(ValueError, match="generate_singing"):
            _resolve_lip_sync_source({"content_mode": "mv"}, tmp_path, {"is_performance": True})

    @pytest.mark.unit
    def test_performance_shots_resolve_a_separate_video_model(self):
        """演唱镜头解析口型驱动模型，不是项目配置的常规视频模型。"""
        import inspect

        from server.services.generation_context import VideoLaneRequest
        from server.services.generation_tasks import execute_video_task

        assert "lip_sync" in VideoLaneRequest.__dataclass_fields__
        src = inspect.getsource(execute_video_task)
        assert "VideoLaneRequest(lip_sync=" in src

    @pytest.mark.unit
    async def test_driving_audio_is_sliced_to_the_shot_window(self, tmp_path: Path, monkeypatch):
        """整轨不能直接当驱动音频——s2v 从音频第 0 秒起驱动口型。

        MV 镜头钉在歌曲的绝对时间轴上，第 40 秒那一镜若拿到整轨，演员会去对唱歌曲开头的词。
        除 start_seconds=0 的第一镜外全部错位，且成片能听能看、只是对不上。
        """
        from server.services.generation_tasks import _slice_lip_sync_window

        calls: list[dict] = []

        async def _fake_slice(source, output, *, start_seconds, duration_seconds):
            calls.append({"start": start_seconds, "duration": duration_seconds})
            output.write_bytes(b"RIFF")
            return output

        monkeypatch.setattr("server.services.generation_tasks.slice_audio_window", _fake_slice)

        source = tmp_path / "vocal_main.wav"
        source.write_bytes(b"RIFF")
        out = await _slice_lip_sync_window(source, {"start_seconds": 40, "duration_seconds": 4}, tmp_path / "d.wav")

        assert out.is_file()
        assert calls == [{"start": 40.0, "duration": 4.0}]

    @pytest.mark.unit
    async def test_slice_window_rejects_unusable_shot_timing(self, tmp_path: Path):
        """入点/时长脏了就报错——按 0 兜底等于静默把这一镜对到歌曲开头。"""
        from server.services.generation_tasks import _slice_lip_sync_window

        source = tmp_path / "vocal_main.wav"
        source.write_bytes(b"RIFF")
        with pytest.raises(ValueError, match="start_seconds"):
            await _slice_lip_sync_window(source, {"duration_seconds": 4}, tmp_path / "d.wav")
        with pytest.raises(ValueError, match="duration_seconds"):
            await _slice_lip_sync_window(source, {"start_seconds": 8, "duration_seconds": 0}, tmp_path / "d.wav")

    @pytest.mark.unit
    def test_video_task_slices_before_calling_the_backend(self):
        """切分必须发生在 generate_video_async 之前，且送进去的是切片而非整轨。

        这条守的是「连起来」：_slice_lip_sync_window 单测全绿、execute_video_task 却仍把
        _resolve_lip_sync_source 的整轨直接传下去，是这个链路最容易退化的形态。
        """
        import inspect

        from server.services.generation_tasks import execute_video_task

        src = inspect.getsource(execute_video_task)
        slice_pos = src.index("_slice_lip_sync_window(")
        call_pos = src.index("generate_video_async(")
        assert slice_pos < call_pos
        assert "driving_audio=driving_audio" in src
        # 整轨变量不得直接进请求
        assert "driving_audio=lip_sync_source" not in src


class TestStateNormalization:
    """音乐与视频共用同一份回包归一——同一个端点，各写一份必然漂移。

    漂移过一次：音乐侧曾只认扁平回包，信封部署下任务永远等不到终态、轮询到超时。
    """

    @pytest.mark.unit
    @pytest.mark.parametrize("raw", ["completed", "succeeded", "SUCCESS"])
    def test_terminal_success_aliases(self, raw: str):
        assert normalize_newapi_task_state({"status": raw})["status"] == "completed"

    @pytest.mark.unit
    def test_envelope_shape_reaches_terminal(self):
        """``{"code","data"}`` 信封必须能判出终态并取到 result_url。"""
        state = normalize_newapi_task_state(
            {"code": "success", "data": {"status": "SUCCESS", "result_url": "http://x/a.wav"}}
        )
        assert state["status"] == "completed"
        assert state["url"] == "http://x/a.wav"

    @pytest.mark.unit
    def test_envelope_failure_reason(self):
        state = normalize_newapi_task_state(
            {"code": "success", "data": {"status": "FAILURE", "fail_reason": "engine oom"}}
        )
        assert state["status"] == "failed"
        assert state["error"]["message"] == "engine oom"

    @pytest.mark.unit
    def test_unknown_status_keeps_polling(self):
        # 把仍在跑的任务误判成终态会让一次成功的生成被丢弃并重复计费。
        assert normalize_newapi_task_state({"status": "SUBMITTED"})["status"] not in (
            "completed",
            "failed",
            "expired",
        )

    @pytest.mark.unit
    def test_non_dict_payload_does_not_crash(self):
        assert normalize_newapi_task_state("garbage")["status"] not in ("completed", "failed")

    @pytest.mark.unit
    def test_music_backend_uses_the_shared_normalizer(self):
        """音乐 backend 不得自带一份归一实现。"""
        import lib.audio_backends.newapi_music as mod

        assert not hasattr(mod, "_normalize_state")
        assert mod.normalize_newapi_task_state is normalize_newapi_task_state


class TestMusicResourcePath:
    @pytest.mark.unit
    def test_music_path_has_no_segment_prefix(self):
        # 音乐是项目级单件产物，不按分镜编号，故无 segment_ 前缀。
        assert resource_relative_path("music", "main") == "music/main.wav"
