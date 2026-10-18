"""
MoviePilot 插件：渲染词（RenderWords）

作用：在 MoviePilot「识别完成」之后，对最终元数据做后处理修正。
      即把番剧管家（Anime-Manager）的「自定义渲染词」规则搬到 MoviePilot 上。

介入点：_PluginBase.get_module() 胁持 ChainBase 的识别方法
        - recognize_media        （同步链路）
        - async_recognize_media  （异步链路）
        ChainBase.run_module() 会先跑插件、再跑系统模块；
        本插件先调用原生识别器拿到 MediaInfo，再按规则改写，然后返回。

支持的规则语法（与番剧管家 backend/recognition/render/engine.py 对齐）：
    1. 正则翻译      Regex => Replacement
    2. 元数据提取    Regex => {[e=25]} / {[s=2;e=EP-12]} / {[tmdbid=123]}
    3. 偏移定位器    [ <> v2] >> EP+10
    4. 专家条件修正  @?{[tmdbid=127532;type=tv;e=13-25]} => {[s=2;e=EP-12]}

版本：1.0.1（MoviePilot V3 专用）
"""

import ast
import contextvars
import operator
import re
import threading
import traceback
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# MoviePilot V3 稳定 SDK
# ---------------------------------------------------------------------------
from app.schemas.types import MediaType, ModuleType
from app.sdk.logging import logger
from app.sdk.network import RequestUtils
from app.sdk.plugin import ModuleManager, _PluginBase

__all__ = ["RenderWords"]

# ---------------------------------------------------------------------------
# 安全算术：只允许数字与 + - * / // % 与括号
# ---------------------------------------------------------------------------
_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_MATH_RE = re.compile(r"^[0-9\s\+\-\*\/%\(\)\.]+$")
_MODS_RE = re.compile(r"\{\[(.*?)\]\}", re.S)
_INCLUDES_SPLIT_RE = re.compile(r"([&|()])")


class RenderWords(_PluginBase):
    # ---- 插件元信息 ----
    plugin_name = "渲染词"
    plugin_desc = "识别完成后按「渲染词」规则修正最终元数据：标题替换、季集重映射、年份/类型/TMDB 重定向"
    plugin_icon = "renderwords.png"
    plugin_version = "1.0.4"
    plugin_author = "pipi20xx"
    author_url = "https://github.com/pipi20xx/555999"
    plugin_config_prefix = "renderwords_"
    plugin_order = 20
    auth_level = 1

    # ---- 运行态 ----
    _enabled: bool = False
    _only_tv: bool = True
    _skip_explicit_id: bool = True
    _debug: bool = False
    _rules: List[str] = []
    _remote_rules: List[str] = []
    _remote_urls: List[str] = []
    _remote_status: List[Dict[str, Any]] = []
    # 同步链路重入保护（线程级）
    _local = threading.local()
    # 异步链路重入保护（协程级）
    _async_busy: contextvars.ContextVar = contextvars.ContextVar("renderwords_busy", default=False)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._only_tv = bool(config.get("only_tv", True))
        self._skip_explicit_id = bool(config.get("skip_explicit_id", True))
        self._debug = bool(config.get("debug", False))

        rules_text = config.get("rules") or ""
        self._rules = [
            line.strip()
            for line in rules_text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

        self._remote_urls = [
            url.strip()
            for url in (config.get("remote_urls") or "").splitlines()
            if url.strip()
        ]

        # 先用上次缓存，保证启动即可用
        cached = self.get_data("remote_cache") or []
        if isinstance(cached, list):
            self._remote_rules = [str(x) for x in cached if str(x).strip()]

        if self._enabled and self._remote_urls:
            threading.Thread(target=self.refresh_remote_rules, daemon=True).start()

        logger.info(
            f"渲染词：已{'启用' if self._enabled else '停用'}，"
            f"本地规则 {len(self._rules)} 条，远程规则 {len(self._remote_rules)} 条"
        )

    def get_state(self) -> bool:
        return self._enabled

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def stop_service(self):
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        """每天 04:10 拉一次远程规则"""
        if self._enabled and self._remote_urls:
            return [{
                "id": "RenderWordsRefresh",
                "name": "渲染词远程规则刷新",
                "trigger": "cron",
                "func": self.refresh_remote_rules,
                "kwargs": {"hour": 4, "minute": 10},
            }]
        return []

    # ------------------------------------------------------------------
    # 核心：胁持识别方法
    # ------------------------------------------------------------------
    def get_module(self) -> Dict[str, Any]:
        """
        key 是「方法名」，不是模块 id。
        ChainBase.run_module() 先执行插件方法；返回非 None 即短路系统模块。
        """
        return {
            "recognize_media": self.recognize_media,
            "async_recognize_media": self.async_recognize_media,
        }

    def recognize_media(self, *args, **kwargs):
        """同步链路：先调原生识别，再后处理"""
        if not self._enabled:
            return None
        if getattr(self._local, "busy", False):
            # 重入：把执行权交回系统模块
            return None
        self._local.busy = True
        try:
            mediainfo = self.chain.run_module("recognize_media", *args, **kwargs)
        except Exception as err:
            logger.error(f"渲染词：调用原生识别失败 - {err}\n{traceback.format_exc()}")
            return None
        finally:
            self._local.busy = False
        return self._post_process(mediainfo, kwargs)

    async def async_recognize_media(self, *args, **kwargs):
        """异步链路：同上"""
        if not self._enabled:
            return None
        if self._async_busy.get():
            return None
        token = self._async_busy.set(True)
        try:
            mediainfo = await self.chain.async_run_module(
                "async_recognize_media", *args, **kwargs
            )
        except Exception as err:
            logger.error(f"渲染词：调用原生识别失败 - {err}\n{traceback.format_exc()}")
            return None
        finally:
            self._async_busy.reset(token)
        return self._post_process(mediainfo, kwargs)

    # ------------------------------------------------------------------
    # 后处理
    # ------------------------------------------------------------------
    def _post_process(self, mediainfo, kwargs: dict):
        if not mediainfo:
            return mediainfo
        meta: Any = kwargs.get("meta")

        # 显式 ID 查询（详情页、刮削回查）默认不动，避免污染缓存
        if self._skip_explicit_id and kwargs.get("tmdbid"):
            return mediainfo
        # 只处理剧集
        if self._only_tv and getattr(mediainfo, "type", None) != MediaType.TV:
            return mediainfo

        rules = list(self._rules) + list(self._remote_rules)
        if not rules:
            return mediainfo

        filename = ""
        if meta is not None:
            filename = getattr(meta, "org_string", None) or getattr(meta, "title", None) or ""

        try:
            mediainfo, hits = self._run_rules(meta, mediainfo, filename, rules)
        except Exception as err:
            logger.error(f"渲染词：规则执行异常 - {err}\n{traceback.format_exc()}")
            return mediainfo

        if hits:
            try:
                self._record_hits(hits, mediainfo, filename)
            except Exception as err:
                logger.debug(f"渲染词：记录命中失败 - {err}")
        return mediainfo

    def _run_rules(
            self,
            meta: Any,
            mediainfo,
            filename: str,
            rules: List[str],
    ) -> Tuple[Any, List[str]]:
        hits: List[str] = []
        ctx = self._context(meta, mediainfo)
        for idx, raw in enumerate(rules):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                applied, meta, mediainfo, ctx = self._apply_line(
                    line, meta, mediainfo, filename, ctx
                )
            except Exception as err:
                logger.warning(f"渲染词：规则 #{idx + 1} 解析失败 [{line}] - {err}")
                continue
            if applied:
                hits.append(line)
        return mediainfo, hits

    # ------------------------------------------------------------------
    # 单条规则
    # ------------------------------------------------------------------
    def _apply_line(self, line: str, meta, mediainfo, filename: str, ctx: dict,
                    _chain_depth: int = 0):
        # 1) 偏移定位器：前词 <> 后词 >> 公式
        if " <> " in line and " >> " in line:
            left, rest = line.split(" <> ", 1)
            right, formula = rest.split(" >> ", 1)
            pattern = r"(?i)%s\s*(\d+)\s*%s" % (
                re.escape(left.strip()), re.escape(right.strip())
            )
            match = re.search(pattern, filename)
            if not match:
                return False, meta, mediainfo, ctx
            new_ep = self._eval_math(
                formula,
                {"EP": int(match.group(1)), "S": ctx.get("S", 1), "YEAR": ctx.get("YEAR", "")},
            )
            if new_ep is None:
                return False, meta, mediainfo, ctx
            meta, mediainfo = self._set_episode(meta, mediainfo, new_ep, None)
            return True, meta, mediainfo, self._context(meta, mediainfo)

        # 2) 专家条件：@?{[条件]} => {[动作]}
        if line.startswith("@?{"):
            if " => " not in line:
                return False, meta, mediainfo, ctx
            src, tgt = line.split(" => ", 1)
            cond_match = _MODS_RE.search(src)
            mods_match = _MODS_RE.search(tgt)
            if not cond_match or not mods_match:
                return False, meta, mediainfo, ctx
            conds = self._parse_kv(cond_match.group(1))
            if not self._match_conds(conds, meta, mediainfo, filename, ctx):
                return False, meta, mediainfo, ctx
            mods = self._parse_kv(mods_match.group(1))
            meta, mediainfo = self._apply_mods(mods, meta, mediainfo, ctx)
            return True, meta, mediainfo, self._context(meta, mediainfo)

        # 3) 正则：翻译 或 提取
        if " => " in line:
            pattern, replacement = line.split(" => ", 1)
            pattern, replacement = pattern.strip(), replacement.strip()
            if not pattern:
                return False, meta, mediainfo, ctx

            # 3a) 提取：Regex => {[k=v]}
            if replacement.startswith("{[") and replacement.endswith("]}"):
                match = re.search(pattern, filename, re.I)
                if not match:
                    return False, meta, mediainfo, ctx
                body = replacement[2:-2]
                try:
                    body = match.expand(body)
                except Exception:
                    pass
                mods = self._parse_kv(body)
                meta, mediainfo = self._apply_mods(mods, meta, mediainfo, ctx)
                return True, meta, mediainfo, self._context(meta, mediainfo)

            # 3b) 翻译：Regex => Replacement（&&开头可串联下一条规则）
            chain_rule = None
            if replacement.startswith("&&"):
                chain_rule = replacement[2:].strip()
                replacement = ""
            if not re.search(pattern, filename, re.I):
                return False, meta, mediainfo, ctx
            meta, mediainfo = self._translate(pattern, replacement, meta, mediainfo)
            if chain_rule and _chain_depth < 5:
                _, meta, mediainfo, ctx = self._apply_line(
                    chain_rule, meta, mediainfo, filename, ctx,
                    _chain_depth=_chain_depth + 1,
                )
            return True, meta, mediainfo, ctx

        return False, meta, mediainfo, ctx

    # ------------------------------------------------------------------
    # 条件匹配
    # ------------------------------------------------------------------
    def _match_conds(self, conds: dict, meta, mediainfo, filename: str, ctx: dict) -> bool:
        """所有条件取 AND；e 支持区间 `13-25`；includes 支持 & | () 逻辑"""
        tmdb_id = str(getattr(mediainfo, "tmdb_id", "") or "")
        mtype = self._type_str(getattr(mediainfo, "type", None) or (getattr(meta, "type", None) if meta else None))
        year = str(getattr(mediainfo, "year", None) or (getattr(meta, "year", None) if meta else "") or "")
        season = str(ctx.get("S") or "")
        episode = int(ctx.get("EP") or 0)

        for key, value in conds.items():
            if key == "tmdbid":
                if str(value) != tmdb_id:
                    return False
            elif key == "type":
                if self._to_media_type(value) is None or self._to_media_type(value) != self._to_media_type(mtype):
                    return False
            elif key == "year":
                if str(value) != year:
                    return False
            elif key == "s":
                if str(value) != season:
                    return False
            elif key == "e":
                if "-" in str(value):
                    try:
                        low, high = (int(x) for x in str(value).split("-", 1))
                    except Exception:
                        return False
                    if not low <= episode <= high:
                        return False
                elif str(episode) != str(value):
                    return False
            elif key == "includes":
                if not self._eval_includes(str(value), filename):
                    return False
            else:
                # 未知条件：保守跳过
                return False
        return True

    def _eval_includes(self, expression: str, filename: str) -> bool:
        expr = expression.replace("||", "|")
        parts: List[str] = []
        for token in _INCLUDES_SPLIT_RE.split(expr):
            token = token.strip()
            if not token:
                continue
            if token == "&":
                parts.append(" and ")
            elif token == "|":
                parts.append(" or ")
            elif token in ("(", ")"):
                parts.append(token)
            else:
                parts.append("True" if token.lower() in filename.lower() else "False")
        try:
            return bool(eval("".join(parts), {"__builtins__": {}}, {}))
        except Exception:
            return expression.lower() in filename.lower()

    # ------------------------------------------------------------------
    # 动作应用
    # ------------------------------------------------------------------
    def _apply_mods(self, mods: dict, meta, mediainfo, ctx: dict):
        for key, value in mods.items():
            try:
                if key == "tmdbid":
                    mediainfo = self._redirect(meta, mediainfo, value, mods.get("type"))
                elif key == "type":
                    meta, mediainfo = self._set_type(meta, mediainfo, value)
                elif key == "year":
                    if meta is not None:
                        meta.year = str(value)
                    if mediainfo is not None:
                        mediainfo.year = str(value)
                elif key == "s":
                    meta, mediainfo = self._set_season(meta, mediainfo, int(value))
                elif key == "e":
                    new_ep = self._eval_math(value, ctx)
                    if new_ep is not None:
                        old_end = getattr(meta, "end_episode", None) if meta is not None else None
                        new_end = None
                        if old_end:
                            new_end = self._eval_math(value, {**ctx, "EP": old_end})
                        meta, mediainfo = self._set_episode(meta, mediainfo, new_ep, new_end)
            except Exception as err:
                logger.warning(f"渲染词：动作 [{key}={value}] 执行失败 - {err}")
        return meta, mediainfo

    def _translate(self, pattern: str, replacement: str, meta, mediainfo):
        if meta is not None:
            for attr in ("cn_name", "en_name"):
                value = getattr(meta, attr, None)
                if value:
                    try:
                        setattr(meta, attr, re.sub(pattern, replacement, value, flags=re.I))
                    except Exception:
                        pass
        if mediainfo is not None:
            for attr in ("title", "en_title", "original_title"):
                value = getattr(mediainfo, attr, None)
                if value:
                    try:
                        setattr(mediainfo, attr, re.sub(pattern, replacement, value, flags=re.I))
                    except Exception:
                        pass
        return meta, mediainfo

    def _set_season(self, meta, mediainfo, season: int):
        if meta is not None:
            meta.begin_season = season
            meta.end_season = None
            meta.total_season = 1
        if mediainfo is not None:
            mediainfo.season = season
        return meta, mediainfo

    def _set_episode(self, meta, mediainfo, begin: int, end: Optional[int]):
        if meta is not None:
            meta.begin_episode = begin
            if end is not None:
                meta.end_episode = end
                meta.total_episode = end - begin + 1 if end >= begin else 1
            else:
                meta.end_episode = None
        return meta, mediainfo

    def _set_type(self, meta, mediainfo, value: str):
        mtype = self._to_media_type(value)
        if not mtype:
            return meta, mediainfo
        if meta is not None:
            meta.type = mtype
        if mediainfo is not None:
            mediainfo.type = mtype
        return meta, mediainfo

    def _redirect(self, meta, mediainfo, new_id, new_type=None):
        """tmdbid 重定向：直接找原生识别器重新取一次"""
        try:
            new_id = int(str(new_id).strip())
        except Exception:
            return mediainfo
        mtype = self._to_media_type(new_type) or getattr(mediainfo, "type", None)
        if meta is not None:
            meta.tmdbid = new_id
            if mtype:
                meta.type = mtype
        try:
            modules = sorted(
                ModuleManager().get_running_type_modules(ModuleType.MediaRecognize),
                key=lambda m: getattr(m, "get_priority", lambda: 100)(),
            )
        except Exception as err:
            logger.warning(f"渲染词：TMDB 重定向取模块失败 - {err}")
            return mediainfo
        for module in modules:
            func = getattr(module, "recognize_media", None)
            if not callable(func):
                continue
            try:
                info = func(meta=meta, tmdbid=new_id, mtype=mtype, cache=True)
            except Exception as err:
                logger.debug(f"渲染词：{getattr(module, 'get_name', lambda: '?')()} 重定向失败 - {err}")
                continue
            if info:
                logger.info(f"渲染词：TMDB 重定向 -> {new_id} ({getattr(info, 'title', '')})")
                return info
        return mediainfo

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_kv(text: str) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for item in str(text).split(";"):
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            key = key.strip().lower()
            if key:
                result[key] = value.strip()
        return result

    @staticmethod
    def _context(meta, mediainfo) -> dict:
        episode = getattr(meta, "begin_episode", None) if meta is not None else None
        end = getattr(meta, "end_episode", None) if meta is not None else None
        season = getattr(meta, "begin_season", None) if meta is not None else None
        if season is None:
            season = getattr(mediainfo, "season", None) or 1
        year = getattr(mediainfo, "year", None) or (
            getattr(meta, "year", None) if meta is not None else None
        ) or ""
        return {
            "EP": episode if episode is not None else 0,
            "END": end if end is not None else 0,
            "S": season,
            "YEAR": year,
            "TMDBID": getattr(mediainfo, "tmdb_id", None) or "",
        }

    @staticmethod
    def _eval_math(expression, context: dict) -> Optional[int]:
        """把 EP/S/YEAR 等占位符替换成数值后安全求值"""
        try:
            text = str(expression).upper()
        except Exception:
            return None
        for key in sorted(context.keys(), key=len, reverse=True):
            value = context.get(key)
            if value is None or value == "":
                continue
            text = re.sub(
                r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(key),
                str(value),
                text,
            )
        text = text.strip()
        if not text or not _MATH_RE.fullmatch(text):
            return None
        try:
            tree = ast.parse(text, mode="eval")
            return int(RenderWords._eval_node(tree.body))
        except Exception:
            return None

    @staticmethod
    def _eval_node(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
            return _BINOPS[type(node.op)](
                RenderWords._eval_node(node.left), RenderWords._eval_node(node.right)
            )
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](RenderWords._eval_node(node.operand))
        raise ValueError("unsupported expression")

    @staticmethod
    def _to_media_type(value) -> Optional[MediaType]:
        if not value:
            return None
        text = str(value).strip().lower()
        if text in ("tv", "电视剧", "剧集", "series"):
            return MediaType.TV
        if text in ("movie", "movies", "电影"):
            return MediaType.MOVIE
        return None

    @staticmethod
    def _type_str(value) -> str:
        if isinstance(value, MediaType):
            return value.to_agent()
        if not value:
            return ""
        return str(getattr(value, "value", value)).lower()

    # ------------------------------------------------------------------
    # 远程规则
    # ------------------------------------------------------------------
    def refresh_remote_rules(self):
        """拉取全部远程规则；成功即更新内存与缓存，失败保留上次缓存并在详情页标注"""
        if not self._remote_urls:
            return
        lines: List[str] = []
        statuses: List[Dict[str, Any]] = []
        for url in self._remote_urls:
            try:
                response = RequestUtils(
                    ua="MoviePilot-RenderWords/1.0", timeout=15
                ).get_res(url)
                if response is None:
                    raise RuntimeError("请求失败（无响应，检查容器网络/代理）")
                response.raise_for_status()
                text = response.text or ""
                count_before = len(lines)
                lines.extend([
                    line.strip()
                    for line in text.splitlines()
                    if line.strip() and not line.strip().startswith("#")
                ])
                statuses.append({
                    "url": url, "ok": True,
                    "count": len(lines) - count_before,
                })
                logger.info(f"渲染词：远程规则拉取成功 {url}（{len(lines) - count_before} 条）")
            except Exception as err:
                statuses.append({"url": url, "ok": False, "error": str(err)[:120]})
                logger.warning(f"渲染词：拉取远程规则失败 {url} - {err}")
        self._remote_status = statuses
        if lines:
            self._remote_rules = lines
            try:
                self.save_data("remote_cache", lines)
            except Exception as err:
                logger.debug(f"渲染词：缓存远程规则失败 - {err}")
            logger.info(f"渲染词：远程规则已更新，共 {len(lines)} 条")

    # ------------------------------------------------------------------
    # 命中记录
    # ------------------------------------------------------------------
    def _record_hits(self, hits: List[str], mediainfo, filename: str):
        record = {
            "time": datetime.now().strftime("%m-%d %H:%M:%S"),
            "file": (filename or "")[:100],
            "title": getattr(mediainfo, "title", "") or "",
            "season": getattr(mediainfo, "season", None),
            "rules": hits[:3],
        }
        data = self.get_data("hits") or []
        if not isinstance(data, list):
            data = []
        data.insert(0, record)
        del data[30:]
        self.save_data("hits", data)

        stats = self.get_data("stats") or {}
        if not isinstance(stats, dict):
            stats = {}
        stats["total"] = int(stats.get("total", 0)) + 1
        self.save_data("stats", stats)

        if self._debug:
            logger.info(f"渲染词：命中 {len(hits)} 条 -> {record['file']} | {hits}")

    # ------------------------------------------------------------------
    # 配置页 / 详情页
    # ------------------------------------------------------------------
    @staticmethod
    def _rule_help_panels() -> List[dict]:
        """配置页的规则语法说明（折叠面板）"""
        def panel(title: str, lines: List[str]) -> dict:
            return {
                "component": "VExpansionPanel",
                "content": [
                    {
                        "component": "VExpansionPanelTitle",
                        "text": title,
                    },
                    {
                        "component": "VExpansionPanelText",
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {"type": "info", "variant": "tonal", "density": "compact"},
                                "content": [
                                    {
                                        "component": "VRow",
                                        "content": [
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12},
                                                "content": [{
                                                    "component": "VCardText",
                                                    "text": line,
                                                }],
                                            } for line in lines
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }

        return [
            panel("① 专家条件修正（最常用）", [
                "格式：@?{[条件列表]} => {[动作列表]}，条件之间是 AND，全部满足才生效。",
                "条件：tmdbid / type（tv或movie）/ year / s / e（支持区间 13-25）/ "
                "includes（文件名包含，支持 & 与 | 及括号）。",
                "动作：tmdbid / type / year / s / e（支持 EP±N、EP*2 等公式，EP=当前集号）。",
                "示例：@?{[tmdbid=85937;type=tv;e=27-33]} => {[s=2;e=EP-26]}",
                "　　　（鬼灭之刃 27-33 集映射为 S2E1-7，合集 begin/end 一起平移）",
                "示例：@?{[includes=(Netflix&1080p)|(Amazon&4K)]} => {[s=2]}",
            ]),
            panel("② 正则翻译（改标题）", [
                "格式：Regex => Replacement，对识别结果的中/英/原标题做忽略大小写的正则替换。",
                "只改标题展示，不动季集；Replacement 为空（&& 串联）时只标记命中，见第 ④ 条。",
                "示例：Unlimited Blade Works => UBW",
                "示例：(\d{4})p => \1P（\1 为反向引用）",
            ]),
            panel("③ 正则提取（强制覆盖季集/ID）", [
                "格式：Regex => {[k=v;k=v]}，在文件名里匹配并提取，右侧支持 \1 反向引用。",
                "可写键：e / s / tmdbid（tmdbid 会触发重定向，用新 ID 重新识别）。",
                "示例：25v2 => {[e=25]}",
                "示例：[Ss](\d+)E(\d+) => {[s=\1;e=\2]}",
            ]),
            panel("④ 偏移定位器 与 && 串联", [
                "偏移定位：前词 <> 后词 >> 公式，在文件名里匹配『前词(数字)后词』代入公式。",
                "示例：[ <> v2] >> EP+10（文件名里 25v2 识别为 E35）。",
                "串联：翻译规则命中后，可用『Regex => &&下一条规则』继续执行下一条规则，",
                "常用于『先改标题、再按新标题套专家规则』的链式场景，最多串联 5 层。",
                "示例：剧场版 => &&@?{[type=tv]} => {[type=movie]}",
            ]),
            panel("⑤ 通用说明", [
                "每行一条规则，# 开头为注释；规则按顺序执行，命中可叠加。",
                "公式占位符：EP=当前集号、END=合集末集、S=季号、YEAR=年份、TMDBID=当前ID。",
                "远程规则地址每行一个，与本地规则合并生效，每天 04:10 自动刷新，"
                "拉取结果见插件详情页的状态列表。",
                "插件工作在『识别之后』：不影响识别命中率，只修正最终季集/标题/年份/TMDB ID；"
                "文件名由重命名模板决定，模板用 {{ title }} 时翻译结果才会体现。",
            ]),
        ]

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "enabled", "label": "启用插件"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "only_tv", "label": "仅处理剧集"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "skip_explicit_id", "label": "跳过显式ID查询"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "debug", "label": "调试日志"},
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VTextarea",
                                "props": {
                                    "model": "rules",
                                    "label": "渲染词规则（每行一条，# 开头为注释）",
                                    "rows": 14,
                                    "placeholder": "@?{[tmdbid=85937;type=tv;e=27-33]} => {[s=2;e=EP-26]}",
                                },
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VExpansionPanels",
                                "props": {"multiple": True, "density": "compact"},
                                "content": self._rule_help_panels(),
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VTextarea",
                                "props": {
                                    "model": "remote_urls",
                                    "label": "远程规则地址（每行一个，每天 04:10 自动刷新）",
                                    "rows": 4,
                                    "placeholder": "https://raw.githubusercontent.com/pipi20xx/555999/refs/heads/main/anime/animeifre.txt",
                                },
                            }],
                        }],
                    },
                ],
            }
        ], {
            "enabled": False,
            "only_tv": True,
            "skip_explicit_id": True,
            "debug": False,
            "rules": "",
            "remote_urls": "",
        }

    def get_page(self) -> Optional[List[dict]]:
        stats = self.get_data("stats") or {}
        hits = self.get_data("hits") or []
        if not isinstance(stats, dict):
            stats = {}
        if not isinstance(hits, list):
            hits = []

        total = stats.get("total", 0)
        rows = []
        for hit in hits[:20]:
            rows.append({
                "component": "VListItem",
                "props": {"subtitle": f"{hit.get('time', '')} · {hit.get('file', '')}"},
                "content": [{
                    "component": "VListItemTitle",
                    "text": f"{hit.get('title', '')}  S{hit.get('season', '')}  ←  "
                            + " / ".join(hit.get("rules") or []),
                }],
            })

        # 远程规则拉取状态（本次运行期内）
        remote_rows = []
        for st in self._remote_status:
            if st.get("ok"):
                title = f"OK · {st.get('count', 0)} 条"
            else:
                title = f"失败 · {st.get('error', '')}"
            remote_rows.append({
                "component": "VListItem",
                "props": {"subtitle": st.get("url", "")},
                "content": [{"component": "VListItemTitle", "text": title}],
            })

        page_content = [
                {
                    "component": "VCardText",
                    "text": f"本地规则 {len(self._rules)} 条 · 远程规则 {len(self._remote_rules)} 条 · 累计命中 {total} 次",
                },
                {"component": "VDivider"},
        ]
        if remote_rows:
            page_content.append({
                "component": "VList",
                "props": {"density": "compact", "subtitle": "远程规则拉取状态"},
                "content": remote_rows,
            })
            page_content.append({"component": "VDivider"})
        page_content.append(
            {
                "component": "VList",
                "props": {"density": "compact"},
                "content": rows or [{
                    "component": "VListItem",
                    "props": {"title": "暂无命中记录"},
                }],
            }
        )

        return [{
            "component": "VCard",
            "props": {"variant": "tonal"},
            "content": page_content,
        }]
