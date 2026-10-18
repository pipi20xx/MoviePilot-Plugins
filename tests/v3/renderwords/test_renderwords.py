"""
渲染词规则引擎自测 —— 不依赖 MoviePilot，用桩模块模拟 V3 的 app.sdk.* 入口。

用法：
    python tests/v3/renderwords/test_renderwords.py   # 直接运行
    python -m pytest tests/v3/renderwords -q          # pytest 收集运行

设计说明：
    插件从 app.sdk.plugin / app.sdk.logging 导入宿主入口，这里先往 sys.modules
    里注入假的 app.* 桩包，再 importlib 加载插件本体，从而在没有 MoviePilot
    的环境里也能验证导入和规则引擎。
"""

import importlib.util
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[3]
PLUGIN_FILE = ROOT / "plugins.v3" / "renderwords" / "__init__.py"


# ---------------------------------------------------------------------------
# 桩：app.* 包
# ---------------------------------------------------------------------------
def _pkg(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = []  # 让它表现得像个包
    return module


def _build_types_module() -> types.ModuleType:
    module = types.ModuleType("app.schemas.types")

    class MediaType:
        MOVIE = "电影"
        TV = "电视剧"
        UNKNOWN = "未知"

        def __init__(self, value):
            self.value = value

        def to_agent(self) -> str:
            return {"电影": "movie", "电视剧": "tv"}.get(self.value, self.value)

        def __eq__(self, other):
            return isinstance(other, MediaType) and other.value == self.value

        def __hash__(self):
            return hash(self.value)

        def __repr__(self):
            return f"MediaType({self.value})"

    class ModuleType:
        MediaRecognize = "mediarecognize"

    module.MediaType = MediaType
    module.ModuleType = ModuleType
    return module


def _build_chain_stub() -> type:
    class PluginChain:
        def run_module(self, method, *args, **kwargs):
            return None

        async def async_run_module(self, method, *args, **kwargs):
            return None

    return PluginChain


def _build_base_stub(chain_cls) -> type:
    from abc import ABCMeta, abstractmethod

    class _PluginBase(metaclass=ABCMeta):
        plugin_name = ""
        plugin_desc = ""
        plugin_order = 9999
        is_clone = False

        def __init__(self):
            self.chain = chain_cls()
            self._store = {}

        @abstractmethod
        def init_plugin(self, config: dict = None):
            ...

        @abstractmethod
        def get_state(self):
            ...

        @abstractmethod
        def get_api(self):
            ...

        @abstractmethod
        def get_form(self):
            ...

        @abstractmethod
        def get_page(self):
            ...

        @abstractmethod
        def stop_service(self):
            ...

        def get_data(self, key=None, plugin_id=None):
            return self._store.get(key)

        def save_data(self, key, value, plugin_id=None):
            self._store[key] = value

    return _PluginBase


def _build_logger():
    import logging

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    return logging.getLogger("renderwords-test")


def install_stubs() -> None:
    """注入 MoviePilot V3 的 app.* 桩模块（app.sdk.plugin / app.sdk.logging）"""
    for key in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
        del sys.modules[key]

    sys.modules["app"] = _pkg("app")

    schemas = _pkg("app.schemas")
    schemas.types = _build_types_module()
    sys.modules["app.schemas"] = schemas
    sys.modules["app.schemas.types"] = schemas.types

    chain_cls = _build_chain_stub()
    base_cls = _build_base_stub(chain_cls)
    logger = _build_logger()

    sdk = _pkg("app.sdk")
    sdk_plugin = types.ModuleType("app.sdk.plugin")
    sdk_plugin._PluginBase = base_cls
    sdk_plugin.PluginChain = chain_cls

    class ModuleManager:
        def get_running_type_modules(self, module_type):
            return iter(())

    sdk_plugin.ModuleManager = ModuleManager
    sdk_logging = types.ModuleType("app.sdk.logging")
    sdk_logging.logger = logger
    sdk_network = types.ModuleType("app.sdk.network")

    class RequestUtils:
        def __init__(self, *args, **kwargs):
            pass

        def get_res(self, url):
            raise RuntimeError("测试桩不发起网络请求")

    sdk_network.RequestUtils = RequestUtils
    sdk.logging = sdk_logging
    sdk.network = sdk_network
    sdk.plugin = sdk_plugin
    sys.modules["app.sdk"] = sdk
    sys.modules["app.sdk.plugin"] = sdk_plugin
    sys.modules["app.sdk.logging"] = sdk_logging
    sys.modules["app.sdk.network"] = sdk_network


def load_plugin():
    """按文件路径加载插件模块（绕过包名限制）"""
    spec = importlib.util.spec_from_file_location(
        "renderwords_under_test", PLUGIN_FILE
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["renderwords_under_test"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 测试数据
# ---------------------------------------------------------------------------
@dataclass
class Meta:
    title: str = ""
    org_string: Optional[str] = None
    type: object = None
    cn_name: Optional[str] = None
    en_name: Optional[str] = None
    year: Optional[str] = None
    begin_season: Optional[int] = None
    end_season: Optional[int] = None
    begin_episode: Optional[int] = None
    end_episode: Optional[int] = None
    total_season: int = 0
    total_episode: int = 0
    tmdbid: Optional[int] = None


@dataclass
class MediaInfo:
    title: str = ""
    en_title: str = ""
    original_title: str = ""
    tmdb_id: Optional[int] = None
    season: int = 1
    year: str = "2024"
    type: object = None


def make_plugin():
    module = load_plugin()
    plugin = module.RenderWords()
    plugin.init_plugin({"enabled": True, "only_tv": True, "rules": ""})
    return plugin


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------
def test_expert_rule_split_season():
    """专家规则：按 tmdbid + 集数区间自动分季，并把集号平移"""
    plugin = make_plugin()
    media_type = sys.modules["app.schemas.types"].MediaType
    meta = Meta(title="鬼灭之刃 30v2", org_string="鬼灭之刃 30v2 [1080p]",
                type=media_type.TV, begin_season=1, begin_episode=30)
    info = MediaInfo(title="鬼灭之刃", tmdb_id=85937, type=media_type.TV)
    info, hits = plugin._run_rules(
        meta, info, meta.org_string,
        ["@?{[tmdbid=85937;type=tv;e=27-33]} => {[s=2;e=EP-26]}"],
    )
    assert hits, "规则应当命中"
    assert (meta.begin_season, meta.begin_episode) == (2, 4), (meta.begin_season, meta.begin_episode)
    assert info.season == 2


def test_expert_rule_not_matched():
    """条件不满足时不改动"""
    plugin = make_plugin()
    media_type = sys.modules["app.schemas.types"].MediaType
    meta = Meta(title="x", org_string="x 100", type=media_type.TV, begin_season=1, begin_episode=100)
    info = MediaInfo(title="鬼灭之刃", tmdb_id=85937, type=media_type.TV)
    info, hits = plugin._run_rules(
        meta, info, meta.org_string,
        ["@?{[tmdbid=85937;type=tv;e=27-33]} => {[s=2;e=EP-26]}"],
    )
    assert hits == []
    assert (meta.begin_season, meta.begin_episode) == (1, 100)


def test_expert_rule_season_only():
    """只改季、不动集（海贼王长番场景）"""
    plugin = make_plugin()
    media_type = sys.modules["app.schemas.types"].MediaType
    meta = Meta(title="海贼王", org_string="海贼王 560", type=media_type.TV, begin_season=1, begin_episode=560)
    info = MediaInfo(title="海贼王", tmdb_id=37854, type=media_type.TV)
    info, hits = plugin._run_rules(meta, info, meta.org_string,
                                   ["@?{[tmdbid=37854;type=tv;e=523-580]} => {[s=14]}"])
    assert hits and meta.begin_season == 14 and meta.begin_episode == 560


def test_regex_translate():
    """正则翻译：改标题、不动季集"""
    plugin = make_plugin()
    media_type = sys.modules["app.schemas.types"].MediaType
    meta = Meta(title="Fate [Unlimited Blade Works] 01",
                org_string="Fate [Unlimited Blade Works] 01", type=media_type.TV, begin_episode=1)
    info = MediaInfo(title="Fate/stay night [Unlimited Blade Works]", en_title="Fate UBW", type=media_type.TV)
    info, hits = plugin._run_rules(meta, info, meta.org_string, ["Unlimited Blade Works => UBW"])
    assert hits
    assert "UBW" in info.title and "Unlimited" not in info.title


def test_regex_extract_episode():
    """正则提取：强制覆盖集数"""
    plugin = make_plugin()
    media_type = sys.modules["app.schemas.types"].MediaType
    meta = Meta(title="某番 25v2", org_string="某番 25v2 [1080p]", type=media_type.TV)
    info = MediaInfo(title="某番", tmdb_id=1, type=media_type.TV)
    info, hits = plugin._run_rules(meta, info, meta.org_string, [r"25v2 => {[e=25]}"])
    assert hits and meta.begin_episode == 25


def test_offset_locator():
    """偏移定位器：前词 <> 后词 >> EP+10"""
    plugin = make_plugin()
    media_type = sys.modules["app.schemas.types"].MediaType
    meta = Meta(title="某番 [25v2]", org_string="某番 [25v2]", type=media_type.TV)
    info = MediaInfo(title="某番", tmdb_id=1, type=media_type.TV)
    info, hits = plugin._run_rules(meta, info, meta.org_string, ["[ <> v2] >> EP+10"])
    assert hits and meta.begin_episode == 35


def test_includes_with_episode():
    """includes + 集数条件组合"""
    plugin = make_plugin()
    media_type = sys.modules["app.schemas.types"].MediaType
    meta = Meta(title="x", org_string="[SumiSora] 某番 - 25 [1080p]",
                type=media_type.TV, begin_season=1, begin_episode=25)
    info = MediaInfo(title="某番", tmdb_id=2, type=media_type.TV)
    info, hits = plugin._run_rules(meta, info, meta.org_string,
                                   ["@?{[includes=SumiSora; e=25]} => {[s=5]}"])
    assert hits and meta.begin_season == 5


def test_includes_boolean_expression():
    """includes 的 & | () 逻辑运算"""
    plugin = make_plugin()
    media_type = sys.modules["app.schemas.types"].MediaType
    meta = Meta(title="x", org_string="[SumiSora] 某番 - 26",
                type=media_type.TV, begin_season=1, begin_episode=26)
    info = MediaInfo(title="某番", tmdb_id=2, type=media_type.TV)
    rule = "@?{[includes=(Netflix&1080p)|(SumiSora&26)]} => {[s=3]}"
    info, hits = plugin._run_rules(meta, info, meta.org_string, [rule])
    assert hits and meta.begin_season == 3

    # 括号优先级：不满足的分支不能命中
    meta2 = Meta(title="x", org_string="[SumiSora] 某番 - 27",
                 type=media_type.TV, begin_season=1, begin_episode=27)
    info2 = MediaInfo(title="某番", tmdb_id=2, type=media_type.TV)
    info2, hits2 = plugin._run_rules(meta2, info2, meta2.org_string, [rule])
    assert hits2 == []


def test_batch_episode_shift():
    """合集场景：begin/end 一起平移"""
    plugin = make_plugin()
    media_type = sys.modules["app.schemas.types"].MediaType
    meta = Meta(title="x", org_string="某番 13-24", type=media_type.TV,
                begin_season=1, begin_episode=13, end_episode=24)
    info = MediaInfo(title="某番", tmdb_id=127532, type=media_type.TV)
    info, hits = plugin._run_rules(
        meta, info, meta.org_string,
        ["@?{[tmdbid=127532;type=tv;e=13-25]} => {[s=2;e=EP-12]}"],
    )
    assert hits
    assert (meta.begin_season, meta.begin_episode, meta.end_episode) == (2, 1, 12), (
        meta.begin_season, meta.begin_episode, meta.end_episode
    )


def test_rule_stacking_and_comments():
    """注释跳过 + 多条规则叠加"""
    plugin = make_plugin()
    media_type = sys.modules["app.schemas.types"].MediaType
    meta = Meta(title="鬼灭之刃 30", org_string="鬼灭之刃 30",
                type=media_type.TV, begin_season=1, begin_episode=30)
    info = MediaInfo(title="鬼灭之刃", tmdb_id=85937, type=media_type.TV)
    rules = [
        "# 注释行",
        "@?{[tmdbid=999;type=tv]} => {[s=9]}",
        "@?{[tmdbid=85937;type=tv;e=27-33]} => {[s=2;e=EP-26]}",
        "鬼灭之刃 => 鬼灭",
    ]
    info, hits = plugin._run_rules(meta, info, meta.org_string, rules)
    assert len(hits) == 2
    assert (meta.begin_season, meta.begin_episode, info.title) == (2, 4, "鬼灭")


def test_math_expressions():
    """公式求值与非法表达式"""
    plugin = make_plugin()
    assert plugin._eval_math("EP-26", {"EP": 30}) == 4
    assert plugin._eval_math("EP*2", {"EP": 21}) == 42
    assert plugin._eval_math("(EP-1)/2", {"EP": 21}) == 10
    assert plugin._eval_math("EP++", {"EP": 30}) is None
    assert plugin._eval_math("__import__('os')", {"EP": 1}) is None
    assert plugin._eval_math("EP", {"EP": 0}) == 0


def test_module_hooks_registered():
    """get_module 必须同时挂同步与异步两个方法名"""
    plugin = make_plugin()
    hooks = plugin.get_module()
    assert set(hooks) == {"recognize_media", "async_recognize_media"}
    assert callable(hooks["recognize_media"])
    assert callable(hooks["async_recognize_media"])


def test_config_parsing():
    """配置解析：注释/空行过滤、远程地址拆分"""
    module = load_plugin()
    plugin = module.RenderWords()
    plugin.init_plugin({
        # 保持 enabled=False：启用 + 有远程地址会起拉取线程，测试里不要碰网络
        "enabled": False,
        "only_tv": False,
        "rules": "# 注释\n@?{[tmdbid=1]} => {[s=2]}\n\n某番 => 某番2\n",
        "remote_urls": "https://a.txt\n\nhttps://b.txt\n",
    })
    assert plugin._enabled is False
    assert plugin.get_state() is False
    assert plugin._only_tv is False
    assert len(plugin._rules) == 2
    assert plugin._remote_urls == ["https://a.txt", "https://b.txt"]

    # 单独验证 get_state 跟随 enabled
    enabled_plugin = module.RenderWords()
    enabled_plugin.init_plugin({"enabled": True, "rules": ""})
    assert enabled_plugin.get_state() is True


def test_disabled_returns_none():
    """未启用时钩子必须放行（返回 None）"""
    module = load_plugin()
    plugin = module.RenderWords()
    plugin.init_plugin({"enabled": False})
    assert plugin.recognize_media(meta=None) is None


# ---------------------------------------------------------------------------
# 运行器
# ---------------------------------------------------------------------------
def run() -> int:
    install_stubs()
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failed = 0
    for name, func in tests:
        try:
            func()
            print(f"  PASS  {name}")
        except AssertionError as err:
            failed += 1
            print(f"  FAIL  {name}: {err}")
        except Exception as err:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {name}: {type(err).__name__}: {err}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(0 if run() == 0 else 1)
else:
    # 被 pytest 导入时先装好 V3 桩，保证用例可以直接被收集执行
    install_stubs()
