# MV 生产链路与 OpenChatCut 交接

回答三个问题：ArcReel 里 MV 怎么做、用到 NewAPI 的哪些接口能力、导出的包交给 OpenChatCut
能不能剪。前两部分是**现状梳理**，第三部分是**现状 + 已发现的缺口 + 修法**。

---

## 一、MV 在 ArcReel 里怎么做

### 决定整个流程顺序的那条约束

MV 的镜头**钉在歌曲的绝对时间轴上**——`MVShot.start_seconds` 是一等必填字段，不是由前序
镜头累加得来。因为口型、卡点、段落切换都依赖绝对位置：累加式排布只要有一镜的实际产出
时长偏离规划值，后面全部错位，而视频时长本就要按供应商档位取整，偏离是常态。

由此推出**歌必须先于剧本定稿**。先写剧本再作曲，镜头与歌对不上，只能推倒重排。

### 数据落在哪

| 数据 | 位置 | 理由 |
|---|---|---|
| `song`（曲风、实测时长、BPM、音频路径、段落表） | 剧本**顶层** | 与这一集的镜头表同生共死，不是项目级配置 |
| `lyrics`（定稿全文） | 剧本**顶层** | 同上；且是「用户给方向 → agent 优化 → 用户改定稿」的产物 |
| `shots[].lyrics_line` | 镜头级 | 该镜对应的歌词行，纯器乐段为空串 |
| `shots[].is_performance` | 镜头级 | 是否人物出镜演唱，决定走不走口型驱动 |
| `music/main.wav` | 项目文件 | 作曲产物 |
| `music/vocal_main.wav` | 项目文件 | 歌声合成产物 |

`song` / `lyrics` 对 LLM **隐藏**（`SkipJsonSchema`）：前者由作曲步骤写回、不该由 LLM 编造，
后者是用户定稿的，每次重排镜头都由 LLM 重出会把定稿冲掉——而重排是常规操作（改段落划分、
换视频模型档位都要重排）。两者只能经 `patch_song` 工具写入。

**由此产生的死锁与破解**：`song` 存剧本顶层，而剧本生成又要靠 `song` 的实测时长排镜头——
两者互为前置。破解方式是新建 MV 项目时直接落一条单集条目，且 `patch_song` 遇到剧本不存在
时代建一个空 `shots` 的骨架，让「先写歌」这条路走得通。这个空骨架在状态计算里记
`segmented`（等价于其余模式的「已分段、可以生成剧本了」），不记 `generated`——否则刚写完歌
的项目会直接跳进 production 阶段，时间线空的、引导也不再提示排镜头。

### 十步流程

1. **确认状态** — 读 `project.json` 与 `scripts/episode_1.json`，看 `song` 是否已回写，据此
   判断进行到哪一步。流程支持从任意断点续做。
2. **写歌**（`write-song` skill）— 用户给方向或初稿，agent 打磨成 ACE-Step 可用的曲风描述
   与歌词，**交用户确认或修改后才算定稿**，经 `patch_song` 写入 `lyrics`。
3. **作曲**（`generate-music` skill → `generate_music` 工具）— **必须带定稿歌词**，留空的话
   引擎自己编一版词，与后面排好的 `lyrics_line` 对不上。产物 `music/main.wav`。
4. **回写歌曲元数据**（`patch_song`）— 写**工具回报的实测时长**（不是申请值）、段落表
   `[{name, start_seconds, duration_seconds}]`、音频路径。段落表是排镜头的硬约束。
5. **资产** — 歌手 / 场景 / 道具定义 + 设计图。**歌手的 `reference_audio` 要单独跟用户确认**
   ——它是歌声合成的音色参考，缺了只能用默认音色。
6. **排镜头**（`generate_episode_script`）— prompt 注入段落表 + 实测曲长 + 可用时长档位。
   生成后核对四件事：相邻镜头 `start_seconds` 首尾相接、最后一镜结束时间等于曲长、副歌至少
   有一个 `is_performance=true`、纯器乐段 `lyrics_line` 为空且 `is_performance=false`。
7. **分镜图** — 逐镜出图。**演唱镜要重点审**：它们会作为口型驱动的人物首帧，人物过小、背对
   镜头、被遮挡都会让口型驱动失败，分镜阶段拦下来比出了视频再返工便宜。
8. **歌声合成**（`generate_singing`）— 音色参考 + 目标曲 → `music/vocal_main.wav`。
9. **出视频** — 两条路分流：非演唱镜走常规图生视频；演唱镜以人声轨作驱动音频走口型驱动。
10. **导出剪映草稿** — 见第三部分。

### 时长档位取两个模型的交集

镜头在生成时才按 `is_performance` 分流到两个模型（常规视频 / 口型驱动），而**档位是排镜头时
就定死的**。所以生成剧本时把 `supported_durations` 收窄为两者交集；交集为空则 fail loud
——静默取其一只是把失败推迟到视频生成，且推迟后错误分散在每一镜上、指不回配置本身。

### 口型驱动的四处同源判定

演唱镜与普通镜同为 `task_type="video"`，却走不同模型。这个分流在四处必须用同一判据
（`lib/lip_sync.py`）：

| 环节 | 用途 | 判不对的后果 |
|---|---|---|
| 入队 provider 派生 | 决定任务进哪个供应商的并发池 | 排在 A 的额度里、请求打到 B |
| worker 认领限流 | 限流槽路由 | 同上，且超发打爆自建网关 |
| 执行层选模型 | 实际调用 | 用普通模型跑演唱镜，口型对不上 |
| 重启恢复 | 重建 backend 轮询已提交的 job | 锁错 provider / 记错模型 |

**驱动音频按镜头时间窗切片，不送整轨**：s2v 从音频第 0 秒起驱动口型，第 40 秒那一镜若拿到
整轨，演员会去对唱歌曲开头的词——除第一镜外全部错位，且成片能听能看、只是对不上，最难排查。

### 歌词只作字幕、不走 TTS

`lyrics_line` 是字幕来源不是配音来源——歌要唱不要念。字幕表（`SUBTITLE_TEXT_FIELDS`）定义为
口播表（`VOICEOVER_TEXT_FIELDS`）的**超集**，差集恰是 mv：`generate_narration_audio` 与
`tts` 任务对 MV 明确拒绝，人声由歌声合成产出。

---

## 二、用到 NewAPI 的哪些接口能力

自建渠道（gpustackplus）的模型经 NewAPI 网关暴露，**视频与音乐共用同一个异步任务端点**，靠
`model` + `metadata.task_type` 区分：

```
POST /v1/video/generations          创建任务 → { task_id }
GET  /v1/video/generations/{id}     轮询 → { status, result_url }
```

### 用到的 task_type

| task_type | 模型 | 用途 | 关键入参 |
|---|---|---|---|
| `i2v` | wan2.2-i2v | 常规图生视频（默认） | `images[0]` = 分镜图 |
| `flf2v` | wan2.2-i2v | 首尾帧生视频 | `images[0]` 首帧、`images[1]` 尾帧 |
| `r2v` | bernini | 参考图直出（最多 4 张） | `metadata.src_ref_images` |
| `s2v` | infinitetalk-480p/720p | **口型驱动**（MV 演唱镜） | `images[0]` 人物图 + `metadata.audio` 驱动音频 |
| `t2m` | acestep-v15-xl-turbo | **作曲** | `metadata.lyrics` / `sample_mode` |
| `cover` | acestep-v15-xl-turbo | 翻唱（带参考音频） | `metadata.reference_audio` |
| `svs` | soulx-singer | **歌声合成** | `metadata.prompt_audio` 音色 + `metadata.target_audio` 目标曲 |

### 几处容易踩错的门面约定

- **时长走 `metadata.audio_duration`，不是顶层 `duration`**。顶层 `duration` 是视频任务的受控
  字段，ACE-Step 读的是前者——发错位置不报错，只是时长静默不生效。
- **输入统一走 data-uri 挂 `metadata` 下**。裸键 `image` / `last_frame` / `src_ref_images` /
  `audio` 混进请求体会被门面当作「原始输入字段」整单 400。
- **MIME 按扩展名取**。上传路由同时接受 `.wav` 与 `.mp3`，把 MP3 标成 `audio/wav` 会让门面
  物化/解码失败，而错误发生在引擎侧、指不回标签贴错。
- **svs 没有文本 prompt**。唱什么由 `target_audio` 决定、用谁的嗓子由 `prompt_audio` 决定，
  入队守卫对 `singing` 任务要求 prompt 恒缺省。
- **提交阶段的重试与轮询分开**。连接建立失败（请求确定未送达）重试；读超时等歧义态终态失败
  不重试——避免重复建任务 + 重复计费。

### 记账

音乐类调用单列 `music` 记账通道，**按产出时长计价**（与视频同形状），不套用 TTS 的按字符
计价——音乐没有字符数，套用后费用恒为 0，记了行却记不出钱。

---

## 三、导出的包交给 OpenChatCut 能不能剪

### 链路已经存在

`OpenChatCut/src/persist/jianyingDraft.ts` 就是为此写的，其开头注释：

> ArcReel「导出为剪映草稿」ZIP → OpenChatCut 工程。选这个格式对接的原因：草稿包里已经是
> 一条排好的时间线——视频/音频/字幕三轨、每段微秒级起止、画布尺寸与帧率俱全。所以本模块
> **只做格式翻译，不推算任何时间点**。

OpenChatCut README 亦明确写了 `including drafts exported by ArcReel`。

### MV 导出实际带什么

| 轨 | 状态 | 说明 |
|---|---|---|
| 视频轨 | ✅ | 逐镜 mp4，微秒级起止 |
| 字幕轨 | ✅ | 取 `lyrics_line`（`SUBTITLE_TEXT_FIELDS` 含 mv） |
| 旁白轨 | — | MV 不走 TTS，本就没有，符合预期 |
| 音乐轨 | ⚠️ **有缺口** | 见下 |
| 转场 | ❌ **丢失** | 见下 |

### 缺口 1：人声轨没进导出包（对 MV 致命）

`jianying_draft_service` 取配乐的路径是写死的：

```python
music_src = safe_resolve(project_dir, resource_relative_path("music", _MAIN_MUSIC_TRACK_ID))
# → music/main.wav
```

`music/vocal_main.wav`（SoulX-Singer 用歌手音色重唱的版本）**完全没有被导出**。

后果：用户在第 5 步专门确认过歌手音色、第 8 步花时间合成了人声，成片里听到的却是 ACE-Step
自带的嗓子。而且这个错误**在 ArcReel 内部不可见**——分镜、视频、字幕都对，只有音轨是另一个
人在唱，要到导入剪辑器试听才发现。

**修法**：导出时按「做过歌声合成就用 `vocal_main.wav`、否则用 `main.wav`」选取。理由是 svs
的产物是**换了音色的完整歌曲**（`target_audio` 传的就是 `main.wav`），二者是替代关系而非叠加。

进一步可以考虑双轨导出（伴奏 + 人声分轨），但那要求作曲侧能出纯伴奏——ACE-Step 的 t2m 带
歌词时产出的就是完整歌曲，当前拿不到分离的伴奏。所以先按替代关系修，双轨留待有伴奏产物时再议。

### 缺口 2：转场丢失

ArcReel 已把 `transition_to_next`（`fade` → 闪黑、`dissolve` → 叠化）写进草稿的
`TransitionType`，但 OpenChatCut 的导入器里 `transition` 出现 **0 次**——完全没读。

这是**确定的 bug**，不是设计取舍：ArcReel 侧已经写了，接收侧没接。改动在 OpenChatCut 仓库。

### 更深的一层：草稿格式承载「结果」，不承载「结构」

即便补齐上面两个缺口，交接过去的仍然只是**一条排好的时间线**。ArcReel 掌握的创作结构无法穿过
剪映草稿格式：

| 丢失的结构 | 剪辑侧的后果 |
|---|---|
| 角色 / 场景 / 道具身份 | 想「把所有小雨的特写挑出来」做不到，只看到一堆 mp4 |
| 镜头意图（image_prompt / video_prompt） | 想重出某镜只能回 ArcReel 手工找 |
| MV 的段落表与 `is_performance` | 剪辑时不知道哪些是副歌、哪些是演唱镜 |
| 素材可再生性 | 拿到成品 mp4，不知道怎么来的、能否重出 |
| 备选版本 | 每镜可能有多版（VersionManager），只传了当前版 |

### 三种拉通深度

**A. 补齐现有接缝的损耗（小，建议现在做）**
- ArcReel 侧：导出选对音轨（缺口 1）
- OpenChatCut 侧：导入器读转场（缺口 2）

工作量小、边界清楚，做完 MV 的交接就是**可用**的。

**B. 带创作结构的交付格式（中，值得做）**
ArcReel 另出一个包：时间线 + 镜头↔角色/场景/段落/prompt 的映射 + 多版本素材清单；
OpenChatCut 加对应导入器。剪辑侧能按角色、按段落、按「演唱镜」筛选，能一键换备选版本。

这才是把两个产品真正拉通的形态——ArcReel 交付的不是「一条剪好的片子」，而是
「一个**结构化的素材工程**」。

**C. 双向联动（大，暂缓）**
剪辑中发现某镜不行 → 经 MCP 回调 ArcReel 重生成 → 素材回流时间线。

但 OpenChatCut 当前的外部 MCP 只有 6 个项目级工具（`openchatcut_status` / `list_projects` /
`create_project` / `target_project` / `get_editor_url`），没有写时间线的外部接口；真正的编辑
工具在内建 agent 的 skills 里。C 需要两边都开新接口，且会让两个产品的部署互相耦合。

### 架构原则：依赖方向保持单向

当前导入器在 **OpenChatCut 仓库**里，即「OpenChatCut 适配 ArcReel 的格式」。这个方向应当保持：
交付格式由 ArcReel 定、OpenChatCut 适配，ArcReel 不为迁就剪辑器改自己的数据模型。这样两个
产品能各自独立演进，也不会让 ArcReel 的发布节奏被剪辑器绑住。

---

## 结论与建议顺序

1. **先修缺口 1**（ArcReel 侧，导出选对音轨）——这是 MV 功能自身的完成度问题，不修的话
   MV 做出来的成片音色是错的。
2. **再修缺口 2**（OpenChatCut 侧，导入器读转场）——独立的小改动。
3. **然后评估方案 B**——需要单独设计交付格式，建议届时另出文档。
4. 方案 C 暂缓，等 OpenChatCut 的外部编辑接口成熟。
