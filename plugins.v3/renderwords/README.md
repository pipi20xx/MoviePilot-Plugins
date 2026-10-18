# 渲染词（RenderWords）

把番剧管家（Anime-Manager）的「自定义渲染词」搬到 MoviePilot 上：
**在识别完成之后，对最终元数据做后处理修正**——改标题、改季、改集、改年份、强制重定向 TMDB ID。

> 与 MoviePilot 内置的「自定义识别词」的区别：
> - 识别词作用在**文件名**上（识别前），影响识别命中率；
> - 渲染词作用在**识别结果**上（识别后），影响最终季集/标题/年份，且支持 `tmdbid`、`type`、集数区间等条件。

**MoviePilot V3 专用插件**（要求 `>=3.0.0`），通过插件市场安装：

1. MoviePilot → 设置 → 插件 → 插件市场 → 添加仓库 `https://github.com/pipi20xx/MoviePilot-Plugins`
2. 刷新市场，搜索「渲染词」→ 安装 → 在「已安装插件」中启用。

本地开发可用 `PLUGIN_LOCAL_REPO_PATHS` 指向本仓库目录，配合 `PLUGIN_AUTO_RELOAD=true` 改完源码不用重启容器。

---

## 规则语法

每行一条，`#` 开头为注释。规则**按顺序**执行，命中即生效（可叠加）。

### 1. 专家条件修正（最常用）

```
@?{[条件列表]} => {[动作列表]}
```

条件：`tmdbid` / `type` / `year` / `s` / `e`（支持 `13-25` 区间）/ `includes`（文件名包含，支持 `&` `|` `()`）。
多个条件之间是 **AND**。动作：`tmdbid` / `type` / `year` / `s` / `e`（支持 `EP±N` 公式）。

```
# 连播番自动分季：鬼灭之刃
@?{[tmdbid=85937;type=tv;e=27-33]} => {[s=2;e=EP-26]}
@?{[tmdbid=85937;type=tv;e=34-44]} => {[s=3;e=EP-33]}

# 海贼王按集数段映射季
@?{[tmdbid=37854;type=tv;e=523-580]} => {[s=14]}

# 按字幕组 + 集数修正季
@?{[includes=SumiSora; e=25]} => {[s=5]}

# includes 支持逻辑运算
@?{[includes=(Netflix&1080p)|(Amazon&4K)]} => {[s=2;e=EP+0]}
```

### 2. 正则翻译（改标题）

```
Regex => Replacement
```

正则替换（忽略大小写）的字段可在配置页「正则翻译作用的字段」里自由勾选（留空 = 全部）：

- meta 层：`meta.title`（识别标题）、`meta.name`、`meta.cn_name`、`meta.en_name`、`meta.resource_team`（字幕组/发布组）
- media 层：`media.title`、`media.en_title`、`media.original_title`（识别到的媒体标题，会进媒体库缓存，谨慎勾选）

识别失败（无 `media_info`）时替换结果落在 `meta.title` 上，API 返回仍能看到效果。

```
Unlimited Blade Works => UBW
(\d{4})p => \1P
```

### 3. 元数据提取（强制覆盖）

```
Regex => {[k=v;k=v]}
```

右侧支持正则反向引用（`\1`）与公式。可写 `e` / `s` / `tmdbid`。

```
25v2 => {[e=25]}
[Ss](\d+)E(\d+) => {[s=\1;e=\2]}
```

### 4. 偏移定位器

```
前词 <> 后词 >> 公式
```

在文件名里匹配 `前词(\d+)后词`，把捕获到的数字代入公式。

```
[ <> v2] >> EP+10
```

---

## 配置项

| 配置 | 说明 |
| --- | --- |
| 启用插件 | 关闭时钩子直接返回 `None`，完全不影响原生流程 |
| 仅处理剧集 | 默认开，电影识别不进入规则引擎 |
| 跳过显式ID查询 | 默认开，详情页/刮削按 tmdbid 回查时不套规则，避免污染缓存 |
| 调试日志 | 命中时打 `logger.info`，可在日志里搜「渲染词」 |
| 渲染词规则 | 上面的四类语法，一行一条 |
| 远程规则地址 | 一行一个 URL，启动时异步拉取 + 每天 04:10 刷新，拉到的内容缓存到插件数据目录 |

可以直接填番剧管家的规则库（纯文本、`#` 注释、语法一致）：

```
https://raw.githubusercontent.com/pipi20xx/555999/refs/heads/main/anime/animeifre.txt
https://raw.githubusercontent.com/pipi20xx/555999/refs/heads/main/anime/episodegroupRE.txt
```

---

## 实现原理

MoviePilot V3 的识别收口在 `ChainBase.recognize_media / async_recognize_media`，
内部走 `run_module("recognize_media", **kwargs)`；`_PluginBase.get_module()` 的 key
是「方法名」，插件先执行、返回非 `None` 即短路系统模块。

本插件的做法：

1. 钩子被调用 → 置重入标记 → `self.chain.run_module("recognize_media", ...)`
   （此时钩子自己因标记直接返回 `None`，于是原生识别器正常执行）；
2. 拿到原生 `MediaInfo` 后，按渲染词规则改写 `meta`（季/集/类型/年份/tmdbid）与 `mediainfo`（标题/季/年份/类型/ID）；
3. 返回改写后的 `MediaInfo` → 外层 `run_module` 直接采用，内置识别器不再重复执行。

同步、异步两条链路各有一个钩子，分别用 `threading.local()` 和 `contextvars.ContextVar` 做重入保护。

导入统一走 V3 稳定 SDK：

```python
from app.schemas.types import MediaType, ModuleType
from app.sdk.logging import logger
from app.sdk.plugin import ModuleManager, _PluginBase
```

---

## 生效范围与已知限制

**会生效**：整理重命名（季集来自 `meta`）、媒体库刮削（`MediaInfo`）、订阅匹配、API 识别、仪表盘识别测试。

**需要注意**：

1. **只改「识别结果」，不改「重命名模板」**。MoviePilot 的文件名由重命名模板决定，
   渲染词的「翻译」类规则改的是 `title`，模板里若用 `{{ title }}` 就会体现；若模板写死了别的字段则不会。
2. **集数改写在批量/合集场景**：`e=EP-12` 会同时平移 `begin_episode` 与 `end_episode`。
3. **tmdbid 重定向**会丢弃当前结果、用新 ID 重新向原生识别器取一次数据（等价于番剧管家的 `[Mod] TMDB重定向`）。
   若内置识别器不可用会退回原结果，不会报错中断。
4. **剧集组（episode_group）** 在刮削阶段可能再次覆盖集号，属于 MoviePilot 自身逻辑，插件不介入。

---

## 调试

- 日志关键字：`渲染词`、`渲染词：命中`、`渲染词：TMDB 重定向`。
- 插件详情页显示：本地/远程规则条数、累计命中次数、最近 20 条命中记录（时间 / 文件名 / 结果标题 / 季 / 命中的规则）。
- 规则写错不会中断识别：单条规则异常只 `logger.warning`，继续跑下一条。

## 自测

不依赖 MoviePilot，用桩模块模拟 `app.sdk.*`：

```bash
python tests/v3/renderwords/test_renderwords.py   # 直接运行
python -m pytest tests/v3/renderwords -q          # pytest 收集运行
```

覆盖：专家条件分季、集号平移、合集 begin/end 同步、正则翻译（含无 media_info 场景与字段选择）、正则提取、
偏移定位器、`includes` 的 `& | ()` 逻辑、公式求值（含非法表达式拦截）、
配置解析、未启用时放行、`get_module` 双钩子注册。
