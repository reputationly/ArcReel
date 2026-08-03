"""ChatCut 交接包导出的单元测试。

交接包的价值在两点：素材只写 URL（体积从百 MB 降到 KB）、额外携带剪映草稿承载不了的创作结构。
两点各自都可能静默退化成「一份能导入但没用的包」，故都要有守卫。
"""

import json
from typing import Any
from urllib.parse import unquote

import pytest


def _write_project(tmp_path, *, project: dict, script: dict, videos: tuple[str, ...] = ()):
    """落一个最小可导出的项目，返回 (ProjectManager, project_dir)。

    视频文件写假字节而非真实视频：交接包只写 URL、从不探测容器元数据，
    aspect_ratio 齐全时连 ffmpeg 都用不到。
    """
    from lib.project_manager import ProjectManager

    pm = ProjectManager(tmp_path / "projects")
    project_dir = tmp_path / "projects" / "demo"
    (project_dir / "videos").mkdir(parents=True)
    for name in videos:
        (project_dir / "videos" / name).write_bytes(b"fake")
    (project_dir / "project.json").write_text(json.dumps(project, ensure_ascii=False), encoding="utf-8")
    (project_dir / "scripts").mkdir()
    (project_dir / "scripts" / "episode_1.json").write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    return pm, project_dir


def _mv_fixture(tmp_path):
    project = {
        "title": "深夜天台",
        "content_mode": "mv",
        "aspect_ratio": {"video": "9:16"},
        "characters": {"小雨": {"character_sheet": "characters/小雨.png"}},
        "scenes": {"天台": {"scene_sheet": "scenes/天台.png"}},
        "episodes": [{"episode": 1, "title": "MV", "script_file": "scripts/episode_1.json"}],
    }
    script = {
        "content_mode": "mv",
        "song": {"duration_seconds": 92.4, "bpm": 88, "sections": [{"name": "verse", "start_seconds": 0}]},
        "shots": [
            {
                "shot_id": "E1S01",
                "section": "verse",
                "start_seconds": 0,
                "duration_seconds": 4,
                "lyrics_line": "夜色漫过天台",
                "is_performance": True,
                "characters_in_shot": ["小雨"],
                "scenes": ["天台"],
                "props": [],
                "image_prompt": {"subject": "少女"},
                "video_prompt": {"motion": "缓慢推近"},
                "transition_to_next": "dissolve",
                "generated_assets": {"video_clip": "videos/shot_E1S01.mp4", "status": "completed"},
            },
        ],
    }
    return _write_project(tmp_path, project=project, script=script, videos=("shot_E1S01.mp4",))


def _build(pm, **kwargs):
    from server.services.chatcut_handoff_service import ChatcutHandoffService

    return ChatcutHandoffService(pm).build_handoff(
        kwargs.pop("project_name", "demo"),
        kwargs.pop("episode", 1),
        base_url=kwargs.pop("base_url", "http://arcreel:1241"),
    )


class TestReferenceNotBytes:
    """交接包的立身之本：素材只写可拉取的 URL，不打包字节。这条退化了就只剩「另一个剪映包」。"""

    @pytest.mark.unit
    def test_media_are_urls_not_filesystem_paths(self, tmp_path):
        pm, project_dir = _mv_fixture(tmp_path)

        payload = _build(pm)

        src = payload["tracks"][0]["clips"][0]["src"]
        assert src == "/api/v1/files/demo/videos/shot_E1S01.mp4"
        # 绝对路径泄漏既暴露服务器目录结构，也让包在别的机器上失效
        assert str(project_dir) not in json.dumps(payload, ensure_ascii=False)

    @pytest.mark.unit
    def test_base_url_is_carried_separately_from_paths(self, tmp_path):
        """host 与 path 分开：ArcReel 不知道别人怎么访问它，消费方导入时还要能改。"""
        pm, _ = _mv_fixture(tmp_path)

        payload = _build(pm, base_url="http://10.0.0.5:1241")

        assert payload["source"]["base_url"] == "http://10.0.0.5:1241"
        assert payload["tracks"][0]["clips"][0]["src"].startswith("/api/v1/files/")

    @pytest.mark.unit
    def test_package_stays_small(self, tmp_path):
        """体积守卫：同内容的剪映包是百 MB 级，交接包必须是 KB 级。"""
        pm, _ = _mv_fixture(tmp_path)

        payload = _build(pm)

        assert len(json.dumps(payload, ensure_ascii=False).encode()) < 16 * 1024


class TestTimelineLayer:
    """第一层：不理解 ArcReel 的消费方也能据此建时间线。"""

    @pytest.mark.unit
    def test_clips_are_laid_out_end_to_end(self, tmp_path):
        project = {
            "title": "demo",
            "content_mode": "narration",
            "aspect_ratio": {"video": "9:16"},
            "episodes": [{"episode": 1, "script_file": "scripts/episode_1.json"}],
        }
        script = {
            "content_mode": "narration",
            "segments": [
                {
                    "segment_id": "S1",
                    "duration_seconds": 8,
                    "novel_text": "从前有座山",
                    "generated_assets": {"video_clip": "videos/segment_S1.mp4", "status": "completed"},
                },
                {
                    "segment_id": "S2",
                    "duration_seconds": 6,
                    "novel_text": "山上有座庙",
                    "generated_assets": {"video_clip": "videos/segment_S2.mp4", "status": "completed"},
                },
            ],
        }
        pm, _ = _write_project(tmp_path, project=project, script=script, videos=("segment_S1.mp4", "segment_S2.mp4"))

        clips = _build(pm)["tracks"][0]["clips"]

        assert [(c["id"], c["start"], c["duration"]) for c in clips] == [("S1", 0.0, 8.0), ("S2", 8.0, 6.0)]

    @pytest.mark.unit
    def test_transition_is_carried(self, tmp_path):
        """转场是 ArcReel 已经写进剪映草稿、却被剪辑侧丢掉的信息，交接包必须带上。"""
        pm, _ = _mv_fixture(tmp_path)

        assert _build(pm)["tracks"][0]["clips"][0]["transition_to_next"] == "dissolve"

    @pytest.mark.unit
    def test_music_track_uses_the_shared_selector(self, tmp_path):
        """音轨选取与剪映导出同源，否则两个导出口会给出不同的成片。"""
        pm, project_dir = _mv_fixture(tmp_path)
        (project_dir / "music").mkdir()
        (project_dir / "music" / "main.wav").write_bytes(b"song")

        audio = [t for t in _build(pm)["tracks"] if t["kind"] == "audio"]

        assert len(audio) == 1
        assert audio[0]["clips"][0]["src"] == "/api/v1/files/demo/music/main.wav"

    @pytest.mark.unit
    def test_no_music_yields_no_audio_track(self, tmp_path):
        pm, _ = _mv_fixture(tmp_path)

        assert not [t for t in _build(pm)["tracks"] if t["kind"] == "audio"]

    @pytest.mark.unit
    def test_mv_lyrics_become_captions(self, tmp_path):
        pm, _ = _mv_fixture(tmp_path)

        captions = [t for t in _build(pm)["tracks"] if t["kind"] == "caption"]

        assert captions[0]["clips"] == [{"start": 0.0, "duration": 4.0, "text": "夜色漫过天台"}]

    @pytest.mark.unit
    def test_span_subtitles_are_offset_by_the_clip_start(self, tmp_path):
        """drama 的 span 字幕要落在**全片时间轴**上，即片段起点 + 片段内偏移。

        必须用第二个片段验证：只看首个片段时 start 恒为 0，「忘了加片段起点」这个 bug
        看不出来——第二段的字幕会整体回卷到片头，成片里字幕比画面早一整段。
        """
        project = {
            "title": "demo",
            "content_mode": "drama",
            "aspect_ratio": {"video": "16:9"},
            "episodes": [{"episode": 1, "script_file": "scripts/episode_1.json"}],
        }

        def _scene(scene_id: str, texts: list[str]) -> dict:
            return {
                "scene_id": scene_id,
                "duration_seconds": 8,
                "utterances": [{"kind": "dialogue", "speaker": "小明", "text": t} for t in texts],
                "generated_assets": {"video_clip": f"videos/scene_{scene_id}.mp4", "status": "completed"},
            }

        script = {
            "content_mode": "drama",
            "scenes": [_scene("E1S01", ["我回来了", "三年后"]), _scene("E1S02", ["你终于来了"])],
        }
        pm, _ = _write_project(tmp_path, project=project, script=script, videos=("scene_E1S01.mp4", "scene_E1S02.mp4"))

        captions = [t for t in _build(pm)["tracks"] if t["kind"] == "caption"][0]["clips"]

        assert [c["text"] for c in captions] == ["我回来了", "三年后", "你终于来了"]
        # 首段内：第一条在 0，第二条按语速顺次后移
        assert captions[0]["start"] == 0.0
        assert 0.0 < captions[1]["start"] < 8.0
        # 第二段的字幕必须落在第二段之后，而不是回卷到片头
        assert captions[2]["start"] >= 8.0


class TestStructureLayer:
    """第二层：剪映草稿承载不了的创作结构。"""

    @pytest.mark.unit
    def test_mv_carries_song_and_shot_structure(self, tmp_path):
        pm, _ = _mv_fixture(tmp_path)

        structure = _build(pm)["structure"]

        assert structure["song"]["bpm"] == 88
        item = structure["items"][0]
        assert item["section"] == "verse"
        assert item["is_performance"] is True
        assert item["lyrics_line"] == "夜色漫过天台"
        assert item["start_seconds"] == 0

    @pytest.mark.unit
    def test_characters_are_normalised_across_skeletons(self, tmp_path):
        """角色字段名随骨架而变（characters_in_shot / _in_scene / _in_segment），
        归一到 characters，剪辑侧不必知道来源骨架。"""
        pm, _ = _mv_fixture(tmp_path)

        item = _build(pm)["structure"]["items"][0]

        assert item["characters"] == ["小雨"]
        assert "characters_in_shot" not in item

    @pytest.mark.unit
    def test_generation_intent_is_carried(self, tmp_path):
        """生成意图让剪辑侧认出「这一镜想要什么」，是将来回调重生成的锚点。"""
        pm, _ = _mv_fixture(tmp_path)

        item = _build(pm)["structure"]["items"][0]

        assert item["image_prompt"] == {"subject": "少女"}
        assert item["video_prompt"] == {"motion": "缓慢推近"}

    @pytest.mark.unit
    def test_asset_index_is_driven_by_asset_specs(self, tmp_path):
        """资产索引由 ASSET_SPECS 驱动而非逐类硬编码——新增资产类型时这里不用改。"""
        pm, _ = _mv_fixture(tmp_path)

        assets = _build(pm)["structure"]["assets"]

        # 中文名做百分号编码后落进 URL——消费方是 fetch(new URL(src, base))，裸中文不合法
        assert unquote(assets["characters"]["小雨"]["sheet"]) == "/api/v1/files/demo/characters/小雨.png"
        assert unquote(assets["scenes"]["天台"]["sheet"]) == "/api/v1/files/demo/scenes/天台.png"
        assert "%" in assets["characters"]["小雨"]["sheet"]
        # 项目里没有的桶不产空条目
        assert "props" not in assets

    @pytest.mark.unit
    def test_ad_carries_its_own_fields(self, tmp_path):
        project = {
            "title": "demo",
            "content_mode": "ad",
            "aspect_ratio": {"video": "9:16"},
            "products": {"某某洗面奶": {"product_sheet": "products/某某洗面奶.png"}},
            "episodes": [{"episode": 1, "script_file": "scripts/episode_1.json"}],
        }
        script = {
            "content_mode": "ad",
            "shots": [
                {
                    "shot_id": "E1S1",
                    "section": "hook",
                    "duration_seconds": 3,
                    "voiceover_text": "还在为脱妆烦恼？",
                    "products_in_shot": ["某某洗面奶"],
                    "generated_assets": {"video_clip": "videos/shot_E1S1.mp4", "status": "completed"},
                },
            ],
        }
        pm, _ = _write_project(tmp_path, project=project, script=script, videos=("shot_E1S1.mp4",))

        structure = _build(pm)["structure"]

        assert structure["items"][0]["products_in_shot"] == ["某某洗面奶"]
        assert structure["items"][0]["section"] == "hook"
        assert unquote(structure["assets"]["products"]["某某洗面奶"]["sheet"]).endswith("products/某某洗面奶.png")

    @pytest.mark.unit
    def test_unknown_content_mode_degrades_to_timeline_only(self, tmp_path):
        """未登记的模式少给而非猜：空 items 会退化成纯时间线，猜错字段名会产出看似有内容的垃圾。"""
        from server.services.chatcut_handoff_service import ChatcutHandoffService

        structure = ChatcutHandoffService.__new__(ChatcutHandoffService)._build_structure(
            "demo", tmp_path, {"characters": {}}, {"content_mode": "未知模式"}, "未知模式"
        )

        assert "items" not in structure
        assert structure["assets"] == {}


class TestExportErrors:
    @pytest.mark.unit
    def test_no_completed_videos_raises(self, tmp_path):
        from server.services.episode_timeline import NoCompletedSegmentsError

        project = {
            "title": "demo",
            "content_mode": "narration",
            "aspect_ratio": {"video": "9:16"},
            "episodes": [{"episode": 1, "script_file": "scripts/episode_1.json"}],
        }
        script = {"content_mode": "narration", "segments": [{"segment_id": "S1", "generated_assets": {}}]}
        pm, _ = _write_project(tmp_path, project=project, script=script)

        with pytest.raises(NoCompletedSegmentsError):
            _build(pm)

    @pytest.mark.unit
    def test_missing_episode_raises_file_not_found(self, tmp_path):
        pm, _ = _mv_fixture(tmp_path)

        with pytest.raises(FileNotFoundError):
            _build(pm, episode=99)


class TestSharedWithJianyingExport:
    """两个导出口对「哪些镜头已出片」的理解必须一致，各写一份的表现是给出不同的成片。"""

    @pytest.mark.unit
    def test_both_exporters_use_the_shared_collector(self):
        import inspect

        from server.services.chatcut_handoff_service import ChatcutHandoffService
        from server.services.jianying_draft_service import JianyingDraftService

        for fn in (ChatcutHandoffService.build_handoff, JianyingDraftService.export_episode_draft):
            assert "collect_video_clips(" in inspect.getsource(fn), fn.__qualname__


class TestHandoffRoute:
    """端点行为与剪映导出对齐：同一套 download_token 与错误映射。"""

    @staticmethod
    def _client(monkeypatch, pm):
        from fastapi.testclient import TestClient

        from server.routers import projects as proj_mod

        monkeypatch.setattr(proj_mod, "get_project_manager", lambda: pm)
        from server.app import app

        return TestClient(app)

    def _get(self, monkeypatch, pm, **params):
        from server.auth import create_download_token

        params.setdefault("episode", 1)
        params.setdefault("download_token", create_download_token("testuser", "demo"))
        return self._client(monkeypatch, pm).get("/api/v1/projects/demo/export/chatcut-handoff", params=params)

    @pytest.mark.unit
    def test_returns_json_as_a_download(self, tmp_path, monkeypatch):
        pm, _ = _mv_fixture(tmp_path)

        response = self._get(monkeypatch, pm)

        assert response.status_code == 200
        payload = response.json()
        assert payload["format"] == "arcreel-chatcut-handoff@1"
        assert ".ccdraft.json" in unquote(response.headers.get("content-disposition", ""))

    @pytest.mark.unit
    def test_base_url_defaults_to_the_incoming_request(self, tmp_path, monkeypatch):
        """ArcReel 不知道别人怎么访问它，缺省按传入请求推导，不写死 localhost。"""
        pm, _ = _mv_fixture(tmp_path)

        base = self._get(monkeypatch, pm).json()["source"]["base_url"]

        assert base.startswith("http") and not base.endswith("/")

    @pytest.mark.unit
    def test_explicit_base_url_wins(self, tmp_path, monkeypatch):
        """消费方与 ArcReel 之间的可达地址与浏览器的未必相同，要能显式指定。"""
        pm, _ = _mv_fixture(tmp_path)

        payload = self._get(monkeypatch, pm, base_url="http://arcreel:1241").json()

        assert payload["source"]["base_url"] == "http://arcreel:1241"

    @pytest.mark.unit
    def test_mismatched_token_returns_403(self, tmp_path, monkeypatch):
        from server.auth import create_download_token

        pm, _ = _mv_fixture(tmp_path)

        response = self._get(monkeypatch, pm, download_token=create_download_token("testuser", "other"))

        assert response.status_code == 403

    @pytest.mark.unit
    def test_missing_episode_returns_404(self, tmp_path, monkeypatch):
        pm, _ = _mv_fixture(tmp_path)

        assert self._get(monkeypatch, pm, episode=99).status_code == 404

    @pytest.mark.unit
    def test_no_completed_videos_returns_422(self, tmp_path, monkeypatch):
        project = {
            "title": "demo",
            "content_mode": "narration",
            "aspect_ratio": {"video": "9:16"},
            "episodes": [{"episode": 1, "script_file": "scripts/episode_1.json"}],
        }
        script = {"content_mode": "narration", "segments": [{"segment_id": "S1", "generated_assets": {}}]}
        pm, _ = _write_project(tmp_path, project=project, script=script)

        assert self._get(monkeypatch, pm).status_code == 422


class TestNarrationTrack:
    """旁白配音是 narration / drama / ad 的成片主音轨。

    漏掉它的表现是：导入剪辑器后画面、字幕、转场全都对，只有整片没声——这类错误在 ArcReel
    内部完全看不出来，要到剪辑器里播放才发现。
    """

    @staticmethod
    def _narration_project(tmp_path, *, with_audio: tuple[bool, ...]):
        project = {
            "title": "demo",
            "content_mode": "narration",
            "aspect_ratio": {"video": "9:16"},
            "episodes": [{"episode": 1, "script_file": "scripts/episode_1.json"}],
        }
        segments = []
        for index, has_audio in enumerate(with_audio, start=1):
            assets: dict[str, Any] = {"video_clip": f"videos/segment_S{index}.mp4", "status": "completed"}
            if has_audio:
                assets["narration_audio"] = f"audio/segment_S{index}.wav"
            segments.append(
                {
                    "segment_id": f"S{index}",
                    "duration_seconds": 8,
                    "novel_text": f"第{index}段",
                    "generated_assets": assets,
                }
            )
        script = {"content_mode": "narration", "segments": segments}
        pm, project_dir = _write_project(
            tmp_path,
            project=project,
            script=script,
            videos=tuple(f"segment_S{i}.mp4" for i in range(1, len(with_audio) + 1)),
        )
        (project_dir / "audio").mkdir()
        for index, has_audio in enumerate(with_audio, start=1):
            if has_audio:
                (project_dir / "audio" / f"segment_S{index}.wav").write_bytes(b"tts")
        return pm

    @pytest.mark.unit
    def test_narration_audio_lands_at_the_clip_offset(self, tmp_path):
        pm = self._narration_project(tmp_path, with_audio=(True, True))

        narration = [t for t in _build(pm)["tracks"] if t["name"] == "旁白"]

        assert len(narration) == 1
        assert narration[0]["clips"] == [
            {"start": 0.0, "duration": 8.0, "src": "/api/v1/files/demo/audio/segment_S1.wav"},
            {"start": 8.0, "duration": 8.0, "src": "/api/v1/files/demo/audio/segment_S2.wav"},
        ]

    @pytest.mark.unit
    def test_segments_without_audio_leave_a_gap(self, tmp_path):
        """缺配音的镜头留白，不能让后一段旁白顶到前面去——那会让整条音轨错位。"""
        pm = self._narration_project(tmp_path, with_audio=(False, True))

        clips = [t for t in _build(pm)["tracks"] if t["name"] == "旁白"][0]["clips"]

        assert len(clips) == 1
        assert clips[0]["start"] == 8.0

    @pytest.mark.unit
    def test_no_narration_yields_no_track(self, tmp_path):
        pm = self._narration_project(tmp_path, with_audio=(False, False))

        assert not [t for t in _build(pm)["tracks"] if t["name"] == "旁白"]

    @pytest.mark.unit
    def test_narration_and_music_coexist(self, tmp_path):
        """两条音轨是不同来源：旁白是逐镜头 TTS，音乐是整片单曲，不能互相顶掉。"""
        pm = self._narration_project(tmp_path, with_audio=(True,))
        (pm.projects_root / "demo" / "music").mkdir()
        (pm.projects_root / "demo" / "music" / "main.wav").write_bytes(b"bgm")

        audio_tracks = {t["name"] for t in _build(pm)["tracks"] if t["kind"] == "audio"}

        assert audio_tracks == {"旁白", "音乐"}

    @pytest.mark.unit
    def test_both_exporters_carry_narration_audio(self):
        """守连接点：两个导出口都要用 collect_video_clips 带出的旁白路径，
        只接一处的表现是「剪映草稿有声、交接包没声」。"""
        import inspect

        from server.services.chatcut_handoff_service import ChatcutHandoffService
        from server.services.jianying_draft_service import JianyingDraftService

        for fn in (ChatcutHandoffService._narration_clips, JianyingDraftService.export_episode_draft):
            assert "narration_audio_abs" in inspect.getsource(fn), fn.__qualname__


class TestSpanCaptionBounds:
    """span 字幕必须收口在片段内。

    drama 的 span 由语速估算逐条累加、**不按场景时长收口**（见 utterance_subtitle_spans），
    台词长过镜头是常态而非异常。原样导出的表现是字幕压到下一镜、或整条挂在视频之后。
    """

    @staticmethod
    def _drama_with_spans(tmp_path, *, scene_seconds: int, texts: list[str]):
        project = {
            "title": "demo",
            "content_mode": "drama",
            "aspect_ratio": {"video": "16:9"},
            "episodes": [{"episode": 1, "script_file": "scripts/episode_1.json"}],
        }
        script = {
            "content_mode": "drama",
            "scenes": [
                {
                    "scene_id": "E1S01",
                    "duration_seconds": scene_seconds,
                    "utterances": [{"kind": "dialogue", "speaker": "小明", "text": t} for t in texts],
                    "generated_assets": {"video_clip": "videos/scene_E1S01.mp4", "status": "completed"},
                },
                {
                    "scene_id": "E1S02",
                    "duration_seconds": 8,
                    "utterances": [{"kind": "dialogue", "speaker": "小红", "text": "下一镜"}],
                    "generated_assets": {"video_clip": "videos/scene_E1S02.mp4", "status": "completed"},
                },
            ],
        }
        pm, _ = _write_project(tmp_path, project=project, script=script, videos=("scene_E1S01.mp4", "scene_E1S02.mp4"))
        return pm

    @staticmethod
    def _captions(pm) -> list[dict[str, Any]]:
        return [t for t in _build(pm)["tracks"] if t["kind"] == "caption"][0]["clips"]

    @pytest.mark.unit
    def test_overlong_span_is_clamped_to_the_clip_end(self, tmp_path):
        """长台词配短镜头：字幕不能越过片段末尾压到下一镜。"""
        pm = self._drama_with_spans(tmp_path, scene_seconds=2, texts=["这是一段很长很长的台词" * 10])

        first = self._captions(pm)[0]

        assert first["start"] == 0.0
        assert first["start"] + first["duration"] <= 2.0

    @pytest.mark.unit
    def test_spans_starting_past_the_clip_are_dropped(self, tmp_path):
        """累加偏移越过片段末尾的 span 整条丢弃，而不是收口成一条零长字幕。"""
        pm = self._drama_with_spans(tmp_path, scene_seconds=2, texts=["很长的第一句" * 20, "第二句", "第三句"])

        captions = self._captions(pm)

        assert [c["text"] for c in captions] == ["很长的第一句" * 20, "下一镜"]
        # 每条都在自己片段内
        for caption in captions:
            assert caption["duration"] > 0

    @pytest.mark.unit
    def test_no_caption_bleeds_into_the_next_clip(self, tmp_path):
        """守全局不变量：任何一条字幕都不跨片段边界。"""
        pm = self._drama_with_spans(tmp_path, scene_seconds=2, texts=["长台词" * 30])

        captions = self._captions(pm)
        boundaries = [0.0, 2.0, 10.0]  # 片段起止

        for caption in captions:
            end = caption["start"] + caption["duration"]
            containing = next(
                (i for i in range(len(boundaries) - 1) if boundaries[i] <= caption["start"] < boundaries[i + 1]),
                None,
            )
            assert containing is not None, caption
            assert end <= boundaries[containing + 1] + 1e-9, f"{caption} 越过了片段边界"

    @pytest.mark.unit
    def test_short_spans_are_left_alone(self, tmp_path):
        """反向：装得下的 span 不被改动——收口逻辑不能把正常情况一起削短。"""
        pm = self._drama_with_spans(tmp_path, scene_seconds=60, texts=["短句"])

        first = self._captions(pm)[0]

        assert 0 < first["duration"] < 60

    @pytest.mark.unit
    def test_both_exporters_bound_spans(self):
        """守连接点：两个导出口对同一份 span 的处置要一致，只改一处的表现是
        「剪映草稿字幕规整、交接包字幕溢出」。"""
        import inspect

        from server.services.chatcut_handoff_service import ChatcutHandoffService
        from server.services.jianying_draft_service import JianyingDraftService

        for fn in (ChatcutHandoffService._caption_clips, JianyingDraftService._generate_draft):
            src = inspect.getsource(fn)
            assert "clip_end" in src, fn.__qualname__
            assert "min(" in src, fn.__qualname__

    @pytest.mark.unit
    def test_empty_span_text_produces_no_caption(self):
        """空文案不产条目。

        两个 span 生产者（utterance_subtitle_spans / collect_ad_reference_unit_clips）当前都在
        上游过滤了空文案，所以这条走不到——保留它是为了与剪映导出的规则逐条对齐，且 spans 是
        剧本里可被手工编辑的数据。故在函数层直接验，不绕经生产者。
        """
        from server.services.chatcut_handoff_service import ChatcutHandoffService

        clip = {
            "subtitle_spans": [
                {"offset_seconds": 0, "duration_seconds": 2, "text": ""},
                {"offset_seconds": 2, "duration_seconds": 2, "text": "有词"},
            ]
        }

        captions = ChatcutHandoffService._caption_clips(clip, 0.0, 8.0)

        assert [c["text"] for c in captions] == ["有词"]


class TestMVAbsoluteTiming:
    """MV 的镜头钉在歌曲时间轴的绝对位置上，不能顺次累加。

    见 MVShot 的模型注释：生成时长按供应商档位取整、偏离规划值是常态，累加排布会让后面整条
    错位。而演唱镜的口型是按**绝对**歌曲位置切出的驱动音频生成的（_slice_lip_sync_window），
    音乐轨又是从 0 连续播放的——一旦漂移，口型就对不上音乐，那正是 MV 的全部意义。
    """

    @staticmethod
    def _mv_with_gaps(tmp_path):
        project = {
            "title": "demo",
            "content_mode": "mv",
            "aspect_ratio": {"video": "9:16"},
            "episodes": [{"episode": 1, "script_file": "scripts/episode_1.json"}],
        }

        def _shot(shot_id: str, start: float, duration: int) -> dict:
            return {
                "shot_id": shot_id,
                "section": "verse",
                "start_seconds": start,
                "duration_seconds": duration,
                "lyrics_line": f"词-{shot_id}",
                "is_performance": True,
                "generated_assets": {"video_clip": f"videos/shot_{shot_id}.mp4", "status": "completed"},
            }

        # 第二镜之前有 5 秒间奏（无镜头），累加排布会把它顶到 4.0
        script = {
            "content_mode": "mv",
            "song": {"duration_seconds": 60},
            "shots": [_shot("E1S01", 0, 4), _shot("E1S02", 9, 4), _shot("E1S03", 13, 4)],
        }
        pm, _ = _write_project(
            tmp_path,
            project=project,
            script=script,
            videos=("shot_E1S01.mp4", "shot_E1S02.mp4", "shot_E1S03.mp4"),
        )
        return pm

    @pytest.mark.unit
    def test_shots_land_on_their_song_positions(self, tmp_path):
        pm = self._mv_with_gaps(tmp_path)

        clips = _build(pm)["tracks"][0]["clips"]

        assert [c["start"] for c in clips] == [0.0, 9.0, 13.0]

    @pytest.mark.unit
    def test_gaps_are_preserved_not_collapsed(self, tmp_path):
        """间奏段没有镜头，累加排布会把后面的镜头整体前移——这正是「对不上音乐」的成因。"""
        pm = self._mv_with_gaps(tmp_path)

        clips = _build(pm)["tracks"][0]["clips"]

        assert clips[1]["start"] > clips[0]["start"] + clips[0]["duration"], "间奏留白被吞掉了"

    @pytest.mark.unit
    def test_captions_follow_the_absolute_position(self, tmp_path):
        """歌词字幕必须跟着镜头走，否则字幕与画面各在一处。"""
        pm = self._mv_with_gaps(tmp_path)

        captions = [t for t in _build(pm)["tracks"] if t["kind"] == "caption"][0]["clips"]

        assert [c["start"] for c in captions] == [0.0, 9.0, 13.0]

    @pytest.mark.unit
    def test_non_mv_still_accumulates(self, tmp_path):
        """反向：不声明绝对入点的骨架照旧顺次累加，绝对定位不能把其余模式一起改坏。"""
        project = {
            "title": "demo",
            "content_mode": "narration",
            "aspect_ratio": {"video": "9:16"},
            "episodes": [{"episode": 1, "script_file": "scripts/episode_1.json"}],
        }
        script = {
            "content_mode": "narration",
            "segments": [
                {
                    "segment_id": f"S{i}",
                    "duration_seconds": 8,
                    "novel_text": f"第{i}段",
                    "generated_assets": {"video_clip": f"videos/segment_S{i}.mp4", "status": "completed"},
                }
                for i in (1, 2)
            ],
        }
        pm, _ = _write_project(tmp_path, project=project, script=script, videos=("segment_S1.mp4", "segment_S2.mp4"))

        clips = _build(pm)["tracks"][0]["clips"]

        assert [c["start"] for c in clips] == [0.0, 8.0]

    @pytest.mark.unit
    def test_both_exporters_honor_the_declared_start(self):
        """守连接点：两个导出口都要认绝对入点，只改一处的表现是
        「交接包卡上了拍、剪映草稿还在漂」。"""
        import inspect

        from server.services.chatcut_handoff_service import ChatcutHandoffService
        from server.services.jianying_draft_service import JianyingDraftService

        for fn in (ChatcutHandoffService._build_tracks, JianyingDraftService._generate_draft):
            assert "declared_start" in inspect.getsource(fn), fn.__qualname__


class TestReferenceUnitGrouping:
    """ad 参考直出时时间线是 unit 级，结构层必须给出 unit → 成员镜头的分组。

    没有它，剪辑侧手里是一段 unit_id 的视频和一堆 shot_id 的结构，两边对不上——产品、口播、
    生成意图全都关联不到画面，而那正是这个格式相对剪映草稿的全部增量。
    """

    @staticmethod
    def _ad_reference_project(tmp_path):
        project = {
            "title": "demo",
            "content_mode": "ad",
            "generation_mode": "reference_video",
            "aspect_ratio": {"video": "9:16"},
            "products": {"洗面奶": {"product_sheet": "products/p.png"}},
            "episodes": [{"episode": 1, "script_file": "scripts/episode_1.json"}],
        }
        script = {
            "content_mode": "ad",
            "shots": [
                {
                    "shot_id": "E1S1",
                    "section": "hook",
                    "duration_seconds": 3,
                    "voiceover_text": "还在为脱妆烦恼？",
                    "products_in_shot": ["洗面奶"],
                    "generated_assets": {},
                },
                {
                    "shot_id": "E1S2",
                    "section": "selling_point",
                    "duration_seconds": 3,
                    "voiceover_text": "一泵搞定",
                    "products_in_shot": ["洗面奶"],
                    "generated_assets": {},
                },
            ],
            "reference_units": [
                {
                    "unit_id": "U1",
                    "shot_ids": ["E1S1", "E1S2"],
                    "generated_assets": {"video_clip": "videos/unit_U1.mp4", "status": "completed"},
                }
            ],
        }
        pm, _ = _write_project(tmp_path, project=project, script=script, videos=("unit_U1.mp4",))
        return pm

    @pytest.mark.unit
    def test_units_bridge_timeline_ids_to_structure_ids(self, tmp_path):
        pm = self._ad_reference_project(tmp_path)

        payload = _build(pm)
        clip_ids = [c["id"] for c in payload["tracks"][0]["clips"]]
        structure = payload["structure"]

        assert clip_ids == ["U1"], "时间线是 unit 级"
        assert [i["id"] for i in structure["items"]] == ["E1S1", "E1S2"], "结构层是镜头级"
        # 桥：时间线的 id 能查到分组，分组里的 id 能查到结构条目
        assert structure["units"] == [{"id": "U1", "item_ids": ["E1S1", "E1S2"]}]

    @pytest.mark.unit
    def test_every_timeline_clip_resolves_to_structure_items(self, tmp_path):
        """守全局不变量：时间线上的每一段都能落到结构层的条目上。"""
        pm = self._ad_reference_project(tmp_path)

        payload = _build(pm)
        structure = payload["structure"]
        item_ids = {i["id"] for i in structure["items"]}
        by_unit = {u["id"]: u["item_ids"] for u in structure.get("units", [])}

        for clip in payload["tracks"][0]["clips"]:
            members = by_unit.get(clip["id"], [clip["id"]])
            assert members, f"片段 {clip['id']} 关联不到任何结构条目"
            for member in members:
                assert member in item_ids, f"分组指向了不存在的条目: {member}"

    @pytest.mark.unit
    def test_non_unit_paths_emit_no_grouping(self, tmp_path):
        """反向：时间线本就是镜头级时不产空分组——id 直接对得上，多一层徒增歧义。"""
        pm, _ = _mv_fixture(tmp_path)

        assert "units" not in _build(pm)["structure"]

    @pytest.mark.unit
    def test_grouping_predicate_matches_the_collector(self):
        """守连接点：分组的判据与收集器的 unit 分支必须同一条，
        否则会出现「时间线按 unit 排、结构层却不给分组」或反过来的空分组。"""
        import inspect

        from server.services.chatcut_handoff_service import ChatcutHandoffService
        from server.services.episode_timeline import collect_video_clips

        collector = inspect.getsource(collect_video_clips)
        grouping = inspect.getsource(ChatcutHandoffService._reference_unit_groups)
        for src in (collector, grouping):
            assert 'content_mode == "ad"' in src
            assert 'generation_mode == "reference_video"' in src


class TestVersionAlternates:
    """备选版本：剪辑侧换一版画面应当零成本，而不是回 ArcReel 重新生成。

    这是结构层相对剪映草稿的核心增量之一，退化的表现不是报错而是 `versions` 恒不出现——
    包看起来完全正常，只有拿真实的、生成过多版的项目导一次才看得出来。
    """

    @staticmethod
    def _write_versions(project_dir, payload: dict) -> None:
        versions_dir = project_dir / "versions"
        versions_dir.mkdir(exist_ok=True)
        (versions_dir / "versions.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    @pytest.mark.unit
    def test_alternates_ride_along_with_the_item(self, tmp_path):
        pm, project_dir = _mv_fixture(tmp_path)
        self._write_versions(
            project_dir,
            {
                "videos": {
                    "E1S01": {
                        "current_version": 2,
                        "versions": [
                            {"version": 1, "file": "versions/videos/E1S01_v1_20260801T000000.mp4"},
                            {"version": 2, "file": "versions/videos/E1S01_v2_20260802T000000.mp4"},
                        ],
                    }
                }
            },
        )

        shot = _build(pm)["structure"]["items"][0]

        assert [v["v"] for v in shot["versions"]] == [1, 2]
        assert [v["current"] for v in shot["versions"]] == [False, True], "当前版本要标出来"
        assert shot["versions"][0]["src"] == "/api/v1/files/demo/versions/videos/E1S01_v1_20260801T000000.mp4"

    @pytest.mark.unit
    def test_urls_are_escaped_like_the_rest_of_the_package(self, tmp_path):
        """守口径一致：备选版本的 URL 与素材 URL 必须同一种转义写法。

        VersionManager 自带的 file_url 不做百分号转义，直接透传会让同一个中文项目名在一个包里
        出现两种写法，消费方按其中一种拼接就会 404。
        """
        from server.services.chatcut_handoff_service import ChatcutHandoffService

        alternates = ChatcutHandoffService._version_alternates("深夜 天台", tmp_path, "videos", ["E1S01"])

        assert alternates == {}, "没有 versions.json 时不该有任何输出"

        (tmp_path / "versions").mkdir()
        (tmp_path / "versions" / "versions.json").write_text(
            json.dumps({"videos": {"E1S01": {"current_version": 1, "versions": [{"version": 1, "file": "a b.mp4"}]}}}),
            encoding="utf-8",
        )
        src = ChatcutHandoffService._version_alternates("深夜 天台", tmp_path, "videos", ["E1S01"])["E1S01"][0]["src"]

        assert " " not in src, f"空格未转义: {src}"
        assert unquote(src) == "/api/v1/files/深夜 天台/a b.mp4"

    @pytest.mark.unit
    def test_items_without_records_carry_no_key(self, tmp_path):
        """没有版本记录的条目不出空列表：`versions: []` 与「这一镜有备选」在消费方眼里不同，
        逐条塞空列表只会让结构层变胖而不增信息。"""
        pm, project_dir = _mv_fixture(tmp_path)
        self._write_versions(project_dir, {"videos": {"别的镜头": {"current_version": 1, "versions": []}}})

        assert "versions" not in _build(pm)["structure"]["items"][0]

    @pytest.mark.unit
    def test_export_does_not_create_directories(self, tmp_path):
        """导出是只读操作。VersionManager 的构造会建出整套版本目录，没有版本记录的项目不该
        因为被导出一次就多出一批空目录——只读挂载的项目目录下更会直接抛错。"""
        pm, project_dir = _mv_fixture(tmp_path)

        assert "versions" not in _build(pm)["structure"]["items"][0]
        assert not (project_dir / "versions").exists(), "导出不该在项目目录里建目录"

    @pytest.mark.unit
    def test_reference_units_carry_their_own_alternates(self, tmp_path):
        """时间线是 unit 级时备选版本挂在 unit 上：成片按 unit_id 记在 reference_videos 下，
        挂到镜头条目上会永远查不到记录——功能悄悄没了，还查不出错。"""
        pm = TestReferenceUnitGrouping._ad_reference_project(tmp_path)
        self._write_versions(
            tmp_path / "projects" / "demo",
            {
                "reference_videos": {
                    "U1": {
                        "current_version": 1,
                        "versions": [{"version": 1, "file": "versions/reference_videos/U1_v1.mp4"}],
                    }
                }
            },
        )

        structure = _build(pm)["structure"]

        assert structure["units"][0]["versions"][0]["v"] == 1
        assert all("versions" not in item for item in structure["items"]), "镜头条目上查不到 unit 的版本"


class TestNarrationDramaReferenceDirect:
    """narration / drama 走参考直出：成片挂在 video_units 下，不在 segments / scenes 下。

    两轴独立（content_mode × generation_mode）意味着骨架必须两轴一起解，只按 content_mode 解会
    回落到分镜骨架取到空列表——时间线报「请先生成视频」、结构层静默变空，而视频其实都已生成好。
    """

    @staticmethod
    def _project(tmp_path, content_mode: str = "narration"):
        project = {
            "title": "demo",
            "content_mode": content_mode,
            "generation_mode": "reference_video",
            "aspect_ratio": {"video": "9:16"},
            "characters": {"阿澈": {"character_sheet": "characters/阿澈.png"}},
            "scenes": {"码头": {"scene_sheet": "scenes/码头.png"}},
            "episodes": [{"episode": 1, "script_file": "scripts/episode_1.json"}],
        }
        script = {
            "content_mode": content_mode,
            "generation_mode": "reference_video",
            "video_units": [
                {
                    "unit_id": "E1U01",
                    "duration_seconds": 7,
                    "shots": [
                        {"duration": 4, "text": "@[阿澈] 站在 @[码头] 尽头"},
                        {"duration": 3, "text": "远景拉开"},
                    ],
                    "references": [
                        {"type": "character", "name": "阿澈"},
                        {"type": "scene", "name": "码头"},
                        {"type": "character", "name": "阿澈"},
                    ],
                    "transition_to_next": "dissolve",
                    "generated_assets": {"video_clip": "reference_videos/E1U01.mp4", "status": "completed"},
                },
                {
                    "unit_id": "E1U02",
                    "duration_seconds": 5,
                    "shots": [{"duration": 5, "text": "夜色收束"}],
                    "references": [],
                    "generated_assets": {"video_clip": "reference_videos/E1U02.mp4", "status": "completed"},
                },
            ],
        }
        pm, project_dir = _write_project(tmp_path, project=project, script=script)
        (project_dir / "reference_videos").mkdir()
        for unit_id in ("E1U01", "E1U02"):
            (project_dir / "reference_videos" / f"{unit_id}.mp4").write_bytes(b"fake")
        return pm, project_dir

    @pytest.mark.parametrize("content_mode", ["narration", "drama"])
    @pytest.mark.unit
    def test_units_become_the_timeline(self, tmp_path, content_mode):
        """回归：这条路径此前一段片段都收不到，导出恒报 422「请先生成视频」。"""
        pm, _ = self._project(tmp_path, content_mode)

        clips = _build(pm)["tracks"][0]["clips"]

        assert [c["id"] for c in clips] == ["E1U01", "E1U02"]
        assert [c["duration"] for c in clips] == [7, 5]
        assert clips[0]["src"] == "/api/v1/files/demo/reference_videos/E1U01.mp4"
        assert clips[0]["transition_to_next"] == "dissolve", "unit 间转场要带上"

    @pytest.mark.unit
    def test_structure_items_are_the_units_themselves(self, tmp_path):
        """结构层与时间线同一层：unit 自带内容，id 直接对得上，无需再出一份分组。"""
        pm, _ = self._project(tmp_path)

        structure = _build(pm)["structure"]

        assert [i["id"] for i in structure["items"]] == ["E1U01", "E1U02"]
        assert "units" not in structure, "条目与片段一一对应时多一层分组只是同义反复"

    @pytest.mark.unit
    def test_shots_carry_the_generation_intent(self, tmp_path):
        """unit 没有 image_prompt / video_prompt，「这一段想要什么」全在各 shot 的 text 上——
        不带走 shots，结构层就只剩一串 id，这个格式相对剪映草稿的增量归零。"""
        pm, _ = self._project(tmp_path)

        first = _build(pm)["structure"]["items"][0]

        assert [s["text"] for s in first["shots"]] == ["@[阿澈] 站在 @[码头] 尽头", "远景拉开"]

    @pytest.mark.unit
    def test_references_normalise_to_the_storyboard_asset_keys(self, tmp_path):
        """出场资产归一到与分镜条目同名的键：剪辑侧对两条生成路径看到同一套键。

        SKELETONS["video_units"].chars_field 为 None，骨架表要求消费方在此显式决策；
        照搬分镜写法（拿字段名去 get）只会静默得到空名单。
        """
        pm, _ = self._project(tmp_path)

        items = _build(pm)["structure"]["items"]

        assert items[0]["characters"] == ["阿澈"], "重复引用要按首现去重"
        assert items[0]["scenes"] == ["码头"]
        assert "props" not in items[0], "没有的类别不出空列表"
        assert "characters" not in items[1], "references 为空的 unit 不留一地空键"

    @pytest.mark.parametrize("content_mode", ["narration", "drama"])
    @pytest.mark.unit
    def test_shot_text_never_becomes_a_subtitle(self, tmp_path, content_mode):
        """ReferenceVideoUnit 全程没有口播文本字段，Shot.text 是给生成模型的画面描述。
        拿它凑字幕会把「远景拉开」这种指令打到成片上——比没有字幕糟得多。"""
        pm, _ = self._project(tmp_path, content_mode)

        payload = _build(pm)

        assert all(t["kind"] != "caption" for t in payload["tracks"]), "这条路径没有字幕可导"
        assert "远景拉开" not in json.dumps(payload["tracks"], ensure_ascii=False)

    @pytest.mark.unit
    def test_alternates_come_from_reference_videos(self, tmp_path):
        """成片按 unit_id 记在 reference_videos 下；照分镜直出去 videos 里查恒为空。"""
        pm, project_dir = self._project(tmp_path)
        TestVersionAlternates._write_versions(
            project_dir,
            {
                "reference_videos": {
                    "E1U01": {
                        "current_version": 2,
                        "versions": [
                            {"version": 1, "file": "versions/reference_videos/E1U01_v1.mp4"},
                            {"version": 2, "file": "versions/reference_videos/E1U01_v2.mp4"},
                        ],
                    }
                },
                "videos": {"E1U01": {"current_version": 9, "versions": [{"version": 9, "file": "wrong.mp4"}]}},
            },
        )

        items = _build(pm)["structure"]["items"]

        assert [v["v"] for v in items[0]["versions"]] == [1, 2]
        assert items[0]["versions"][1]["current"] is True
        assert "versions" not in items[1]

    @pytest.mark.unit
    def test_both_exporters_resolve_the_skeleton_with_both_axes(self, tmp_path):
        """守连接点：收集器是两个导出口共用的，剪映草稿此前同样收不到这条路径的片段。"""
        from server.services.episode_timeline import collect_video_clips

        _, project_dir = self._project(tmp_path)
        script = json.loads((project_dir / "scripts" / "episode_1.json").read_text(encoding="utf-8"))

        with_mode = collect_video_clips(script, project_dir, generation_mode="reference_video")
        without_mode = collect_video_clips(script, project_dir, generation_mode="storyboard")

        assert [c["id"] for c in with_mode] == ["E1U01", "E1U02"]
        assert without_mode == [], "切回分镜直出时残留的 video_units 不该抢走收集"
