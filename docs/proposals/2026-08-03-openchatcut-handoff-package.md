# OpenChatCut 交接包（ChatCut 专用草稿）

ArcReel 交给 OpenChatCut 继续剪辑的专用格式。与既有的剪映草稿包**并存**，不替代它。

## 为什么不是改剪映草稿

剪映草稿包的用途是「拷到 Windows 上给剪映用」，必须**自包含**——素材打进 ZIP、路径改写成用户
本地剪映目录。把它改成引用式，它就在别的机器上打不开了。

而给 OpenChatCut 的交接是另一回事：两者通常同机/同网部署，素材本来就在 ArcReel 手里，且
OpenChatCut 有能力承载比时间线更丰富的结构。这两条需求塞进一个格式只会互相牵制。

| | 剪映草稿（保持不变） | **ChatCut 交接包（新增）** |
|---|---|---|
| 素材 | 打包进 ZIP | 只写可拉取的 URL |
| 体积（实测 30 秒 MV） | ~103 MB | **KB 级** |
| 携带创作结构 | ❌ 只有时间线 | ✅ 角色/段落/意图/备选版本 |
| 用途 | 拷走给剪映 | 交给 OpenChatCut 继续剪 |

## 素材怎么过去：HTTP 拉取，不是共享文件系统

最初的设想是「只给 NFS 路径，OpenChatCut 直接读」。看过 OpenChatCut 的实现后改用 **HTTP 拉取**，
理由有三：

1. **OpenChatCut 的素材模型不接受外部路径**。它的时间线 `src` 恒为 `/media/uploads/<uid>.<ext>`，
   由自己的 server 提供、blob 存 IndexedDB（`mediaBlobStore`）。素材最终一定要落进它自己的
   存储，「直接从别处读」与 local-first 的设计相悖。
2. **它的导入通道本来就是惰性的**。`ProjectImportMedia.load()` 是个函数而非字节——把它实现成
   一次 `fetch` 即可，不必先把素材塞进包里。
3. **HTTP 不要求共享文件系统**，只要求网络可达。同机 docker 网络下
   `http://arcreel:1241` 直接可达；异机部署也照样能用。共享挂载则把两个产品的部署绑死。

ArcReel 侧不需要新增端点——`GET /api/v1/files/{project_name}/{path}` 已经在服务项目内的
图片与视频（前端就靠它显示素材）。

## 包的形态

单个 JSON 文件（`*.ccdraft.json`），无 ZIP、无二进制。

```jsonc
{
  "format": "arcreel-chatcut-handoff@1",
  "source": {
    "product": "ArcReel",
    "project": "深夜天台",
    "episode": 1,
    "content_mode": "mv",
    "base_url": "http://arcreel:1241"      // 素材拉取的根，导入时可被用户覆盖
  },

  // ── 第一层：时间线（与剪映草稿等价的信息，OpenChatCut 直接建轨） ──
  "canvas": { "width": 1920, "height": 1080, "fps": 30 },
  "tracks": [
    { "kind": "video", "name": "主轨", "clips": [
      { "id": "E1S01", "start": 0.0, "duration": 4.0,
        "src": "/api/v1/files/深夜天台/videos/scene_E1S01.mp4",
        "transition_to_next": "dissolve" }
    ]},
    { "kind": "audio", "name": "人声", "clips": [
      { "start": 0.0, "duration": 92.4, "src": "/api/v1/files/深夜天台/music/vocal_main.wav" }
    ]},
    { "kind": "caption", "name": "歌词", "clips": [
      { "start": 0.0, "duration": 4.0, "text": "夜色漫过天台" }
    ]}
  ],

  // ── 第二层：创作结构（剪映草稿承载不了的部分） ──
  "structure": {
    "song": { "duration_seconds": 92.4, "bpm": 88,
              "sections": [{ "name": "verse", "start": 0, "duration": 32 }] },
    "shots": [
      { "id": "E1S01",
        "section": "verse",              // 属于哪个歌曲段落
        "is_performance": true,           // 是否演唱镜（口型驱动出的）
        "lyrics_line": "夜色漫过天台",
        "characters": ["小雨"],           // 出场角色/场景/道具的身份
        "scenes": ["天台"],
        "props": [],
        "image_prompt": { "...": "..." }, // 生成意图，供回 ArcReel 重生成时定位
        "video_prompt": { "...": "..." },
        "versions": [                     // 备选版本，剪辑侧可一键切换
          { "v": 2, "src": "/api/v1/files/深夜天台/videos/scene_E1S01.mp4", "current": true },
          { "v": 1, "src": "/api/v1/files/深夜天台/versions/videos/scene_E1S01_v1.mp4" }
        ]
      }
    ],
    "assets": {
      "characters": { "小雨": { "sheet": "/api/v1/files/深夜天台/characters/小雨.png" } },
      "scenes": { "天台": { "sheet": "/api/v1/files/深夜天台/scenes/天台.png" } }
    }
  }
}
```

**分两层的理由**：第一层让 OpenChatCut 不理解 ArcReel 也能把时间线建起来（与现有剪映导入器
同等能力）；第二层是增量，不认识它的消费方直接忽略即可。格式演进时第二层可以随内容模式扩展，
不动第一层。

## 剪辑侧因此能做什么

| 结构 | 剪辑侧的能力 |
|---|---|
| `characters` / `scenes` | 「把所有小雨的特写挑出来」——按角色筛选片段 |
| `section` / `is_performance` | 「副歌段换个机位」「所有演唱镜提亮」——按段落与镜头性质批量操作 |
| `lyrics_line` | 歌词字幕已对齐，且能按词检索定位 |
| `versions` | 换备选版本**零成本**（当前要回 ArcReel 重新生成） |
| `image_prompt` / `video_prompt` | 认出「这一镜想要什么」，将来可回调 ArcReel 重生成 |

这正是把交付物从「一条剪好的片子」变成「一个**结构化的素材工程**」。

## 实现分工

**ArcReel 侧**（本仓）

1. 新增导出端点 `POST /api/v1/projects/{name}/chatcut-handoff`，产出单个 JSON
2. 时间线部分复用 `jianying_draft_service` 已有的片段收集与字幕派生逻辑——那部分
   （按骨架取条目、算起止、取字幕文案）与目标格式无关，抽成共用函数，不复制一份
3. 结构部分按 content_mode 分派：mv 出 `song`/`section`/`is_performance`，ad 出
   `products_in_shot`，narration/drama 出各自的字段。**用现有的模式分派表**
   （`SUBTITLE_TEXT_FIELDS`、`SKELETONS` 等），不新起一套
4. `base_url` 由请求方传入或读配置——ArcReel 自己不知道别人怎么访问它

**OpenChatCut 侧**（另一仓）

5. 新增 `src/persist/chatcutHandoff.ts`，把上述 JSON 翻成 `ProjectImportPayload`；
   `media[].load()` 实现为 `fetch(base_url + src)`
6. 第二层结构存进工程的扩展字段，供 agent 与筛选 UI 消费
7. **顺带修**：现有剪映导入器 `transition` 出现 0 次——ArcReel 已把转场写进草稿却没被读取

## 需要确认的两件事

- **鉴权**：ArcReel 的文件端点是否要求 token？若要求，交接包得带一个临时下载凭据
  （类似现有的 `create_download_token`），或让用户在 OpenChatCut 侧配置
- **依赖方向**：导入器放在 OpenChatCut 仓库（与剪映导入器一致），保持
  「OpenChatCut 适配 ArcReel 的格式」的单向依赖。ArcReel 不引入对剪辑器的任何依赖

## 不做什么

- **不改剪映草稿包** —— 它的自包含是刚需
- **不做双向联动**（剪辑中回调 ArcReel 重生成）—— OpenChatCut 当前的外部 MCP 只有 6 个
  项目级工具，没有写时间线的接口；等那边成熟再议
- **不共享文件系统** —— 见上文
