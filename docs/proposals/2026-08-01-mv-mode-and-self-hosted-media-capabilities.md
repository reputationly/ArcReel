# MV 内容模式与自建媒体能力接入方案

- 状态：待评审
- 日期：2026-08-01
- 触发：自建 gpustackplus 渠道已具备音乐、歌声、口型驱动、首尾帧、视频编辑等能力，
  ArcReel 侧只接入了其中的 `i2v` 与 `tts` 两种，其余能力在产品上不可达。
- 目标：让 ArcReel 能产出**有音乐、有人开口唱的 MV**。

---

## 一、现状与差距

ArcReel 当前把 gpustackplus 当作普通 NewAPI 中转使用，只发 `i2v` 一种视频任务，
音频只走 `tts`。而该渠道实际暴露 21 种 `task_type`，其中与本方案相关的有 8 种全部未接入。

差距不只是「少几个模型」，而是**三条产品链路缺失**：

| 缺失链路 | 后果 |
|---|---|
| 音乐生成 | 成片没有 BGM，广告/短片只能导出后在剪映里自己配 |
| 歌声合成 + 口型驱动 | 无法做「人物开口唱」，MV 这一品类根本立不住 |
| 首尾帧 / 视频编辑 | 运动控制只能靠 prompt 文字描述，画面可控性差 |

---

## 二、gpustackplus 能力契约（调研结论）

来源：`new-api` 仓 `relay/channel/task/gpustackplus/`（`constants.go` / `adaptor.go`）与
`web/classic/src/constants/musicPlayground.constants.js`。以下为**已确认**的契约，
不是推测。

### 2.1 任务类型与输入

| task_type | 模型 | 输入契约 | 备注 |
|---|---|---|---|
| `i2v` | `wan2.2-i2v` | `image`（仅首帧，多传静默丢弃） | 已接入 |
| `flf2v` | `wan2.2-i2v` | `images=[首帧, 尾帧]`，**必须 2 张** | 未接入 |
| `t2v` | `wan2.2-t2v` | 纯文本，**带图会被拒** | 未接入 |
| `s2v` | `infinitetalk-480p/720p` | 人物图 `image` + 驱动音频 `metadata.audio` | 未接入 |
| `r2v` | `bernini` | **仅参考图** `metadata.src_ref_images` | 未接入 |
| `v2v` | `bernini` | 源视频 `metadata.src_video` | 未接入 |
| `rv2v` | `bernini` | 源视频 + 参考图 | 未接入 |
| `t2m` | `acestep-v15-xl-turbo` | 纯文本，文本必填 | 未接入 |
| `cover` | `acestep-v15-xl-turbo` | 参考音频 `metadata.reference_audio` + 文本 | 未接入 |
| `repaint` | `acestep-v15-xl-turbo` | 源音频 `metadata.src_audio` + 文本 | 未接入 |
| `svs` | `soulx-singer` | `metadata.prompt_audio`（音色参考）+ `metadata.target_audio`（目标曲/伴奏），**均必填、无需文本** | 未接入 |
| `t2a` | `audiox` | 纯文本，**中文需先中译英**（文本编码器仅认英文） | 未接入 |
| `v2a` | `ltx2-v2a` | 视频 + 可选文本 → **配好音的视频**（非纯音频） | 未接入 |
| `sr` | `seedvr2` | 源视频 `metadata.video` + `metadata.sr_ratio` | 未接入 |
| `tts` | `qwen3-tts` 等 | 文本 + `metadata.voice` | 已接入 |

### 2.2 两条硬约束

**输入统一走 `input_refs` 物化。** 门面维护一张 `legacyInputKeys` 剥离表
（`image`/`last_frame`/`audio`/`src_video`/`src_ref_images`/`prompt_audio`/`target_audio` …）：
这些裸键若混进请求体，门面会以「检测到原始输入字段」**整单 400**。

**task_type 由模型名推断，特例需显式指定。** 推断规则：
`i2v→i2v`、`infinitetalk→s2v`、`seedvr2→sr`、`bernini→v2v`、`acestep→t2m`、
`indextts/tts/voxcpm/cosyvoice/moss→tts`。
要走 `flf2v`/`r2v`/`rv2v`/`cover`/`repaint` 必须显式传 `metadata.task_type`。

> 今天线上那个 `400 需要图片输入，必须提供 image/input_reference`，原文就出自
> `adaptor.go:250` 的输入防呆——不是网关故障，是我们发的请求确实没有图。

---

## 三、MV 内容模式设计

### 3.1 为什么是新的 content_mode 而不是复用 ad

MV 与现有三种模式在**时间轴的组织方式**上根本不同：

- narration 按朗读节奏切片、drama 按场景对话组织、ad 按带货八段框架分配秒数；
- **MV 的时间轴由歌曲决定**——先有歌，再按歌的段落（前奏/主歌/副歌/间奏/尾奏）
  分配镜头。镜头时长不是创作选择，是音乐结构的产物。

这条差异决定了它必须有自己的剧本模型与 prompt builder，无法通过 ad 的参数化覆盖。

### 3.2 数据模型

新增骨架 `mv_shots`，登记进 `lib/script_skeleton.SKELETONS`：

```python
"mv_shots": Skeleton("shot_id", "characters_in_shot"),
```

新增 `MVShot` / `MVEpisodeScript`（`lib/script_models.py`）：

```python
class MVShot(BaseModel):
    shot_id: str                     # E{集}S{序号}
    section: str                     # intro / verse / chorus / bridge / outro
    start_seconds: float             # 相对歌曲起点的入点 —— MV 特有
    duration_seconds: int
    lyrics_line: str                 # 该镜对应的歌词行（可空：纯器乐段）
    is_performance: bool             # 是否人物出镜演唱（决定走不走 s2v）
    characters_in_shot: list[str]
    scenes: list[str]
    props: list[str]
    image_prompt: ImagePrompt
    video_prompt: VideoPrompt
    generated_assets: GeneratedAssets
```

`MVEpisodeScript` 顶层新增：

```python
song: SongMeta          # 歌曲元数据：风格、BPM、时长、段落表
lyrics: str             # 完整歌词（svs 与字幕共用）
```

**关键设计：`start_seconds` 是一等字段。** 其余模式的镜头只有时长、顺次排布；
MV 的镜头必须钉在歌曲时间轴的绝对位置上，否则口型与歌声对不齐。剪映导出时
视频轨按 `start_seconds` 摆放，而非累加 `duration_seconds`。

### 3.3 资产

复用现有 `character` 资产，不新增类型。角色的 `reference_audio` 字段
（上游 `832dc757` 已加）正好承载**歌手音色参考**，直接喂给 `svs` 的 `prompt_audio`。

新增一类项目级产物「歌曲」，存 `music/` 子目录，不进 `ASSET_SPECS`
（它不是可复用的设计资产，是单件产物，形态更接近 `overview`）。

---

## 四、音乐通道设计

### 4.1 复用 audio 通道还是新开 music 通道

**复用 audio。** 理由：`generation_worker` 的并发通道按 `media_type` 划分，
音乐与 TTS 都受同一个自建服务的算力约束，分成两条通道会让总并发翻倍、打满 GPU。
`lib/custom_provider/endpoints.py` 的 `media_type` 保持 `text|image|video|audio` 四值不变。

新增 `task_type`：`music`（对应门面的 `t2m`/`cover`/`repaint`）。与 `tts` 并列，
共用 audio 通道的并发额度。

### 4.2 新增 backend

`lib/audio_backends/` 下新增 `gpustack_music.py`，实现 `t2m`/`cover`/`repaint`。
现有 `audio_backends/` 只有 `dashscope` 与 `openai` 两个 TTS 实现，music 是新的
能力维度，需要在 `AudioCapabilities`（新建）里声明模型支持哪些音乐任务。

### 4.3 与画面对齐

歌曲先生成、时长确定后，剧本生成才能按段落分配镜头。这决定了 **MV 的流程顺序与
其余模式相反**：

```
现有模式：剧本 → 资产图 → 分镜图 → 视频 → （配音）
MV 模式：  歌曲 → 剧本（按歌的段落与时长）→ 资产图 → 分镜图 → 视频 → 口型
```

---

## 五、唱歌链路

三步串起来，每一步都已有确认的契约：

```
① t2m（ACE-Step）        文本描述曲风 → 伴奏/目标曲 .wav
        ↓ 产物作为 target_audio
② svs（SoulX-Singer）    prompt_audio（角色 reference_audio）
                        + target_audio（①的产物）→ 歌声 .wav
        ↓ 产物作为 metadata.audio
③ s2v（InfiniteTalk）    人物图 + 歌声 → 口型对上的演唱视频
```

只有 `is_performance=true` 的镜头走 ③；其余镜头走常规 `i2v`/`flf2v`，
最后在剪映时间轴上与歌曲音轨对齐。

**风险**：`svs` 的 `target_audio` 语义是「目标曲/伴奏」，但引擎如何从伴奏推导旋律
与歌词对齐，文档未说明。需要先用真实音频跑通一次，确认产出是否可用——
这是整条链路唯一没有把握的一环。

---

## 六、视频能力扩展（flf2v / bernini）

改动集中在 `lib/video_backends/newapi.py`：

1. **`video_capabilities_for_model` 按模型声明能力**——目前非 Seedance 一律
   `max_reference_images=0`，需按 gpustackplus 的模型清单展开：
   `wan2.2-i2v` 支持首尾帧（`last_frame=True`）、`bernini` 支持参考图
   （`r2v`，`max_reference_images>0`）。
2. **payload 组装按 task_type 分派键名**——现在无论什么模式都发
   `images[]` + `metadata.image_urls`，而门面对 `flf2v` 要 `images=[首,尾]`、
   对 `r2v` 要 `metadata.src_ref_images`。键名不对就是 400。
3. **显式下发 `metadata.task_type`**——`flf2v`/`r2v`/`rv2v` 无法由模型名推断。

---

## 七、分期实施

| 期 | 内容 | 依赖 | 量 |
|---|---|---|---|
| **1** | flf2v + bernini（r2v/v2v/rv2v）能力与 payload 分派 | 无 | 小 |
| **2** | 音乐通道：ACE-Step `t2m` + 音乐产物 + 剪映音乐轨 | 无 | 中 |
| **3** | MV 内容模式：骨架、剧本模型、prompt builder、校验、前端向导、`CLAUDE.mv.md` | 2 | 大 |
| **4** | 唱歌链路：`svs` + `s2v` 串联 | 2、3 | 中 |

第 1、2 期互不依赖，可并行；第 3 期是主要成本所在。

### 第 3 期的成本来源

`"drama"` 当前出现在 **29 个非测试文件**中（前端 15、lib 10、server 4）。
新增一个 content_mode 需要逐一评估这 29 处：

- `lib/script_skeleton.py` — 骨架注册与两个解析器
- `lib/script_models.py` — 剧本模型
- `lib/data_validator.py` — 结构校验
- `lib/status_calculator.py` — 阶段与进度
- `lib/profile_manifest.py` — agent profile 变体
- `server/services/jianying_draft_service.py` — 导出（字幕轨 + 音乐轨）
- 前端 15 处 — 向导、画布路由、时间轴、类型定义

---

## 八、未决问题

1. **`svs` 的实际产出质量**——`target_audio` 到歌词对齐的机制未文档化，须实测。
   建议第 2 期完成后先手工跑一次 `t2m → svs`，确认可用再启动第 3、4 期。
2. **歌词从哪来**——用户提供，还是 LLM 按主题创作？后者需要新的文本档位任务。
3. **MV 是否需要分集**——现有 `episodes` 结构对单曲 MV 是冗余的，
   是否比照 ad 的「恒单集」处理。
4. **`t2a`（AudioX）的中译英**——文本编码器仅认英文，接入时需要一层翻译，
   走哪个文本档位待定。
5. **`v2a`（LTX-2.3）的定位**——它直接产出「配好音的视频」，与我们「视频轨 +
   音乐轨分离」的剪映导出模型冲突，本方案暂不接入。

---

## 九、建议

先做第 1 期（视频能力对齐），它最小、无依赖，且能立刻改善现有广告项目的画面可控性；
同时它会验证我们读门面契约的方式是否准确——若第 1 期一次跑通，后续三期的契约理解
就有了可信基础。

第 2 期完成后**先做一次 `t2m → svs` 的手工验证**，再决定第 3、4 期是否按此方案推进。
唱歌链路是 MV 的立身之本，它若不成立，MV 模式的产品价值需要重新评估。
