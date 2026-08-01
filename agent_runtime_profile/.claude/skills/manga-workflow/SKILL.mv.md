---
name: manga-workflow
description: MV 项目的工作流入口。当用户提到做 MV、做音乐视频、继续项目、查看进度时必须使用此 skill。触发场景包括但不限于："帮我做一支 MV"、"继续"、"下一步"、"看看项目进度"等。即使用户只说了简短的"继续"或"下一步"，只要当前上下文涉及 MV 项目，就应该触发。不要用于单个资产生成（如只重画某张分镜图或只重新生成某个角色设计图——那些有专门的 skill）。
---
<!-- mode: mv -->

# MV 工作流

本项目为 **MV 模式**（mv）：单支音乐视频、恒单集（剧本即 `scripts/episode_1.json`）、
**镜头按歌曲段落排布**。没有分集概念，不做分集规划或小说源文件处理。

## 顺序不可颠倒：先有歌，再有剧本

MV 的时间轴由歌曲决定——镜头时长不是创作选择，是音乐段落的产物。先写剧本再作曲，
镜头与歌对不上，只能推倒重排。

## 工作流步骤

1. **确认项目状态**：Read `project.json` 与 `scripts/episode_1.json`，确认 `title`、
   `content_mode`（固定 `mv`）、`generation_mode`（固定 `storyboard`）、`characters`（歌手）。
   再看剧本顶层 `song` 是否已回写——它是判断进行到哪一步的关键

2. **写歌**：走 `write-song` skill——用户给方向或初稿，你打磨成 ACE-Step 可用的
   曲风描述与歌词，**交用户确认或修改后才算定稿**。歌词是 MV 的骨架，
   用户改一个字都可能要重排镜头，不要自作主张定稿。
   定稿歌词经 `mcp__arcreel__patch_song({"script": "episode_1.json", "lyrics": "..."})` 写进剧本

3. **生成音乐**：走 `generate-music` skill 调
   `mcp__arcreel__generate_music({"prompt": "曲风描述", "lyrics": "定稿歌词", "duration_seconds": 可选})`。
   **务必传定稿歌词**——留空的话引擎会自己编一版词，与剧本里排好的 lyrics_line 对不上。
   产物落 `music/main.wav`

4. **回写歌曲元数据**：把**工具回报的实测时长**（不是申请值）、段落划分、音频路径经
   `mcp__arcreel__patch_song({"script": "episode_1.json", "song": {"duration_seconds": ..., "audio_path": "music/main.wav", "sections": [...]}})`
   写进剧本顶层 `song`。`song` 与 `lyrics` 是剧本**顶层**字段，只能用 `patch_song` 写——
   `patch_episode_script` 按分镜 id 定位、只改镜头字段，改不了顶层。
   段落表要给出每段的 `name` / `start_seconds` / `duration_seconds`——它是下一步排镜头的硬约束

5. **资产定义与设计图**：歌手、场景、道具定义写入 `project.json` 后 dispatch
   `generate-assets` 生成设计图。**歌手的 `reference_audio` 要单独跟用户确认**——
   它是歌声合成的音色参考，缺了只能用默认音色，出来的不是用户想要的嗓子

6. **生成剧本**：调 `mcp__arcreel__generate_episode_script({"episode": 1})`。
   生成后**必须核对四件事**，不满足就用 `patch_episode_script` 修：
   - 相邻镜头 `start_seconds` 首尾相接，不留空隙也不重叠
   - 最后一镜的结束时间等于歌曲总时长
   - 副歌段至少有一个 `is_performance=true` 的演唱镜头
   - 纯器乐段的 `lyrics_line` 为空串、`is_performance` 为 false

7. **分镜图生成**：走 `generate-storyboard` 逐镜出图。**演唱镜头要重点审**——
   它们会作为口型驱动的人物首帧，人物主体过小、背对镜头、被遮挡都会让口型驱动失败，
   这类问题在分镜阶段拦下来比出了视频再返工便宜得多

8. **合成歌声**：走 `generate-singing` skill 调
   `mcp__arcreel__generate_singing({"voice_reference": 角色的 reference_audio, "target_song": "music/main.wav"})`。
   产物 `music/vocal_main.wav` 是演唱镜头的口型驱动音频。
   缺音色参考时引导用户到角色资产页上传，不要拿默认音色顶替

9. **视频生成**：走 `generate-video`。非演唱镜头逐镜图生视频；
   演唱镜头以人声轨作驱动音频走口型驱动（模型需具备 s2v 能力，如 infinitetalk）

10. **导出剪映草稿**：视频齐全后引导用户在 Web 端导出。草稿含视频轨 + 歌词字幕轨 +
    **独立音乐轨**（配乐与人声分轨，方便在剪映里分别调音量）

工作流支持**灵活入口**：从剧本 `song` 是否已回写、`shots` 是否齐全判断进行到哪一步，
中断后从未完成的阶段继续。

## 歌词不走 TTS

`lyrics_line` 是**字幕来源**，不是配音来源——歌词要唱不要念。
`generate_narration_audio` 对 MV 不适用，人声由歌声合成产出。
