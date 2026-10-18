# MoviePilot-Plugins（个人插件仓库）

[MoviePilot](https://github.com/jxxghp/MoviePilot) 的个人插件仓库，**只维护 V3 专用插件**。

本仓库 fork 自 [jxxghp/MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins)
官方插件市场，已移除全部官方插件与多代索引，仅保留本仓库自己的插件。
`docs/` 下的开发指南为上游参考文档。

## 插件列表

| 插件 ID | 名称 | 说明 | 版本 |
| --- | --- | --- | --- |
| `RenderWords` | [渲染词](./plugins.v3/renderwords/README.md) | 识别完成后按「渲染词」规则修正最终元数据：标题替换、季集重映射、年份/类型/TMDB 重定向 | 1.0.6 |

## 在 MoviePilot 中使用

1. MoviePilot → 设置 → 插件 → 插件市场 → 添加仓库：
   `https://github.com/pipi20xx/MoviePilot-Plugins`
2. 刷新插件市场，搜索「渲染词」→ 安装 → 启用。

市场索引为 `package.v3.json`，仅对 MoviePilot V3（`>=3.0.0`）生效；
`package.json` 保留为空对象，兼容旧版客户端的市场请求。

## 仓库结构

```text
MoviePilot-Plugins/
├── plugins.v3/              # V3 专用插件源码（一个插件一个目录，目录名 = 插件类名小写）
│   └── renderwords/
├── tests/v3/                # 插件自测（桩模块方式，不依赖 MoviePilot 后端）
│   └── renderwords/
├── icons/                   # 插件图标
├── docs/                    # 上游 V3 开发指南（参考文档）
├── package.v3.json          # V3 插件市场索引
├── package.json             # 空索引（兼容旧版客户端请求）
└── .github/workflows/       # release：自动打包 Release zip；check：编译 + 版本门禁 + 自测
```

## 维护约定

- 插件目录名必须是插件主类名的小写，主类定义在目录的 `__init__.py`。
- 版本三处同步：`package.v3.json` 的 `version`、插件类 `plugin_version`、`history` 首条。
- `history` 当前版本置顶，按语义版本降序排列。
- 新增插件同时补 `tests/v3/<plugin_id>/` 自测。
- 提交前：`python3 -m compileall plugins.v3`、`python3 tests/v3/renderwords/test_renderwords.py`、
  `python3 .github/scripts/check_plugin_versions.py package.v3.json`。

## 发布

`package.v3.json` 中声明 `"release": true` 的插件，在推送到 `main` 后由
[release workflow](./.github/workflows/release.yml) 自动打包成
`<插件目录>_v<版本>.zip` 并创建 GitHub Release，Market 安装即走该资产。
