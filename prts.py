"""PRTS Wiki 的 MediaWiki API 客户端。

插件需要的 PRTS 数据都能通过 api.php 结构化获取，不必解析渲染后的 HTML：
干员列表（分类成员）、语音页模板参数（资源路径、每条语音的文件名、台词）、
重定向（PRTS 维护的干员别称）以及文件页的图片地址。
"""

import asyncio
import hashlib
import re
from typing import Any, Dict, List, Optional

import aiohttp

from . import constants


class PRTSLookupError(Exception):
    """PRTS 请求或解析失败，并携带可直接展示的原因。"""


class PRTSNotFoundError(PRTSLookupError):
    """PRTS 上不存在对应页面，通常是名称写错或使用了别称。"""


VOICE_PAGE_SUFFIX = "/语音记录"

_TEMPLATE_PARAM_RE = re.compile(r"^\|(?P<key>[^=\n|]+)=(?P<value>.*)$", re.MULTILINE)
_NUMBERED_KEY_RE = re.compile(r"^(?P<field>标题|语音|台词)(?P<index>\d+)$")
_WORD_TEMPLATE = "{{VoiceData/word|"
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)

# 台词模板里的语言标签 -> 插件语言代码；繁体等插件不支持的标签直接忽略。
TEXT_LANGUAGE_LABELS = {
    "中文": "cn",
    "中文-方言": "fy",
    "日文": "jp",
    "英文": "us",
    "韩文": "kr",
    "意大利文": "it",
    "俄文": "ru", "俄语": "ru",
    "德文": "de", "德语": "de",
    "西班牙文": "es", "西班牙语": "es",
    "法文": "fr", "法语": "fr",
}


async def api_query(
    session: aiohttp.ClientSession,
    params: Dict[str, str],
) -> Dict[str, Any]:
    """调用 api.php 并返回 JSON，对限流和临时故障按插件统一的策略重试。"""
    query = {"format": "json", "formatversion": "2", **params}
    retries = constants.CHARACTER_PAGE_RETRIES

    for attempt in range(retries):
        try:
            async with session.get(constants.PRTS_API_URL, params=query) as response:
                status = response.status

                if status == 200:
                    data = await response.json(content_type=None)

                    if not isinstance(data, dict):
                        raise PRTSLookupError("PRTS API 返回了无法识别的数据")

                    if "error" in data:
                        info = data["error"].get("info") or data["error"].get("code")
                        raise PRTSLookupError(f"PRTS API 报错: {info}")

                    return data

                if status == 403:
                    raise PRTSLookupError(
                        "PRTS 拒绝访问（HTTP 403），请稍后重试或检查网络出口"
                    )

                if status not in constants.RETRYABLE_PAGE_STATUSES:
                    raise PRTSLookupError(f"PRTS 请求失败（HTTP {status}）")

                if attempt + 1 >= retries:
                    if status == 429:
                        raise PRTSLookupError("PRTS 请求过于频繁（HTTP 429），请稍后重试")

                    raise PRTSLookupError(f"PRTS 服务暂时异常（HTTP {status}），请稍后重试")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            if attempt + 1 >= retries:
                raise PRTSLookupError(f"访问 PRTS 时网络异常: {exc}") from exc

        await asyncio.sleep(0.4 * (2**attempt))

    raise PRTSLookupError("PRTS 请求未返回内容")


async def fetch_operator_catalog(session: aiohttp.ClientSession) -> List[Dict[str, str]]:
    """返回 PRTS 上所有带语音记录页的干员：[{"name", "addedAt"}]，按收录时间从新到旧。

    “干员语音”分类只包含真正有语音页的干员，卫戍协议、集成战略等玩法里的
    临时干员不在其中，不需要额外过滤。
    """
    params = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": constants.PRTS_OPERATOR_VOICE_CATEGORY,
        "cmnamespace": "0",
        "cmlimit": "500",
        "cmprop": "title|timestamp",
        "cmsort": "timestamp",
        "cmdir": "desc",
    }
    result = []
    seen = set()

    for _ in range(constants.PRTS_MAX_CONTINUE_PAGES):
        data = await api_query(session, params)

        for member in data.get("query", {}).get("categorymembers", []):
            title = str(member.get("title", ""))

            if not title.endswith(VOICE_PAGE_SUFFIX):
                continue

            name = title[: -len(VOICE_PAGE_SUFFIX)].strip()

            if name and name not in seen:
                seen.add(name)
                result.append({"name": name, "addedAt": str(member.get("timestamp", ""))})

        continuation = data.get("continue")

        if not continuation:
            return result

        params = {**params, **{key: str(value) for key, value in continuation.items()}}

    return result


async def resolve_title(session: aiohttp.ClientSession, name: str) -> Optional[str]:
    """沿 PRTS 重定向解析页面名，例如“小羊”->“艾雅法拉”；页面不存在返回 None。"""
    data = await api_query(
        session,
        {"action": "query", "titles": name, "redirects": "1"},
    )
    pages = data.get("query", {}).get("pages", [])

    if not pages or pages[0].get("missing") or pages[0].get("invalid"):
        return None

    return str(pages[0].get("title", "")) or None


async def fetch_voice_record(
    session: aiohttp.ClientSession,
    character: str,
) -> Dict[str, Any]:
    """读取“干员/语音记录”页的模板参数并解析，见 parse_voice_record。"""
    records = await fetch_voice_records(session, [character])
    if character not in records:
        raise PRTSNotFoundError(f"PRTS 未找到角色 {character} 的语音记录")
    return records[character]


async def fetch_voice_records(
    session: aiohttp.ClientSession,
    characters: List[str],
) -> Dict[str, Dict[str, Any]]:
    """批量读取资源记录和头像地址；不存在的页面省略，解析失败抛错。"""
    result: Dict[str, Dict[str, Any]] = {}
    names = list(dict.fromkeys(characters))
    batch_size = constants.PRTS_BATCH_TITLES

    for start in range(0, len(names), batch_size):
        batch = names[start : start + batch_size]
        data = await api_query(
            session,
            {
                "action": "query",
                "prop": "revisions|imageinfo",
                "rvprop": "content",
                "rvslots": "main",
                "iiprop": "url",
                "redirects": "1",
                "titles": "|".join(
                    title for name in batch for title in (
                        f"{name}{VOICE_PAGE_SUFFIX}",
                        constants.PRTS_AVATAR_FILE_TITLE.format(character=name),
                    )
                ),
            },
        )
        query = data.get("query", {})
        # MediaWiki 会规范化页名（如首字母大写），按原名对回去。
        normalized = {
            str(item.get("from", "")): str(item.get("to", ""))
            for item in query.get("normalized", [])
        }
        redirects = {str(item["from"]): str(item["to"])
                     for item in query.get("redirects", [])}
        pages = {str(page.get("title", "")): page
                 for page in query.get("pages", [])}

        def find_page(title):
            current = normalized.get(title, title)
            seen = set()
            while current in redirects and current not in seen:
                seen.add(current)
                current = redirects[current]
            return pages.get(current)

        for name in batch:
            page = find_page(f"{name}{VOICE_PAGE_SUFFIX}")
            if page is None or page.get("missing") or page.get("invalid"):
                continue

            try:
                content = page["revisions"][0]["slots"]["main"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise PRTSLookupError(f"{name} 的语音记录页内容为空") from exc

            record = parse_voice_record(str(content))
            record["character"] = str(page["title"])[: -len(VOICE_PAGE_SUFFIX)]

            if not record["paths"] or not record["files"]:
                raise PRTSLookupError(f"{name} 的语音记录缺少路径或文件名")
            avatar = find_page(constants.PRTS_AVATAR_FILE_TITLE.format(character=name)) or {}
            info = avatar.get("imageinfo") or [{}]
            record["avatar_url"] = str(info[0].get("url", "")) or None
            result[name] = record

    return result


async def fetch_file_url(session: aiohttp.ClientSession, file_title: str) -> Optional[str]:
    """返回文件页（如“文件:头像_阿米娅.png”）对应的原图地址。"""
    data = await api_query(
        session,
        {
            "action": "query",
            "prop": "imageinfo",
            "iiprop": "url",
            "titles": file_title,
        },
    )
    pages = data.get("query", {}).get("pages", [])

    if not pages or pages[0].get("missing"):
        return None

    info = pages[0].get("imageinfo") or []

    if not info:
        return None

    return str(info[0].get("url", "")) or None


def language_from_label(label: str, voice_key: str = "") -> Optional[str]:
    base, _ = _split_skin_label(label)
    language = constants.PRTS_LANGUAGE_LABELS.get(base)
    if language is None and base == "联动":
        language = constants.PRTS_VOICE_FOLDERS.get(voice_key.strip("/").split("/", 1)[0])
    return language


def skin_name_from_label(label: str, voice_key: str) -> str:
    match = re.search(r"[（(]([^()（）]+)[）)]", label)
    name = match.group(1).strip() if match else ""
    return _safe_resource_name(name, voice_key, "skin_", 10)


def skin_resource_id(voice_key: str) -> str:
    key = voice_key.strip().strip("/")
    return _safe_resource_name(key.rsplit("/", 1)[-1], key, "skin_", 12)


def _safe_resource_name(raw: str, fallback: str, prefix: str, digest_length: int) -> str:
    raw = re.sub(r"[^\w\- .·()（）]", "_", raw, flags=re.UNICODE).strip(" ._")
    if not raw:
        return prefix + hashlib.sha256(fallback.encode("utf-8")).hexdigest()[:digest_length]
    if len(raw) > 80:
        raw = raw[:71] + "_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return raw


def voice_sources(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    if "sources" in record:
        return record["sources"]
    return [{
        "label": label, "voice_key": key,
        "language": language_from_label(label, key),
        "is_skin": bool(_split_skin_label(label)[1]),
        "skin_name": skin_name_from_label(label, key),
        "resource_id": skin_resource_id(key),
    } for label, key in record["paths"].items()]


def parse_voice_record(wikitext: str) -> Dict[str, Any]:
    """解析 VoiceTable 模板。

    返回：
    - paths：{语言标签: 资源路径}，与旧版页面 data-voice-base 属性的内容一致；
    - files：{语音标题: 文件名}，例如 {"交谈1": "CN_002.wav"}，只含该干员实际拥有的语音；
    - texts：{皮肤名或 "": {语音标题: {语言代码: 台词}}}，基础台词的键为空字符串。
    """
    params = {
        match.group("key").strip(): match.group("value").strip()
        for match in _TEMPLATE_PARAM_RE.finditer(wikitext)
    }
    paths = {}

    for item in params.get("路径", "").split(","):
        label, _, path = item.partition(":")
        label = label.strip()
        path = path.strip()

        if label and path:
            paths[label] = path

    numbered: Dict[str, Dict[str, str]] = {}

    for key, value in params.items():
        match = _NUMBERED_KEY_RE.match(key)

        if match:
            numbered.setdefault(match.group("index"), {})[match.group("field")] = value

    files = {}
    texts: Dict[str, Dict[str, Dict[str, str]]] = {}

    for fields in numbered.values():
        title = fields.get("标题", "").strip()

        if not title:
            continue

        file_name = fields.get("语音", "").strip()

        if file_name:
            if title in constants.VOICE_RESOURCE_IDS:
                if not re.fullmatch(r"CN_\d{3}\.wav", file_name, re.IGNORECASE):
                    raise PRTSLookupError(f"语音文件名无效: {title}")
                files[title] = file_name.lower()

        for label, text in _iter_word_templates(fields.get("台词", "")):
            base_label, skin = _split_skin_label(label)
            language = TEXT_LANGUAGE_LABELS.get(base_label)

            if language and text:
                texts.setdefault(skin, {}).setdefault(title, {})[language] = text

    record = {"paths": paths, "files": files, "texts": texts}
    record["sources"] = voice_sources(record)
    return record


def _iter_word_templates(value: str):
    """逐个取出 {{VoiceData/word|标签|台词}}，台词内允许嵌套模板。"""
    position = 0

    while True:
        start = value.find(_WORD_TEMPLATE, position)

        if start < 0:
            return

        depth = 1
        cursor = start + len(_WORD_TEMPLATE)
        body_start = cursor

        while cursor < len(value) and depth:
            if value.startswith("{{", cursor):
                depth += 1
                cursor += 2
            elif value.startswith("}}", cursor):
                depth -= 1
                cursor += 2
            else:
                cursor += 1

        body = value[body_start : cursor - 2] if depth == 0 else value[body_start:]
        label, _, text = body.partition("|")
        yield label.strip(), _clean_text(text)
        position = cursor


def _split_skin_label(label: str) -> tuple:
    """“中文(超新星)” -> ("中文", "超新星")；没有括号时皮肤名为空字符串。"""
    match = re.fullmatch(r"(?P<base>[^()（）]+)[（(](?P<skin>[^()（）]+)[）)]", label)

    if match:
        return match.group("base").strip(), match.group("skin").strip()

    return label.strip(), ""


def _clean_text(text: str) -> str:
    text = _BR_RE.sub("\n", text)
    text = _HTML_TAG_RE.sub("", text)
    return text.replace("&nbsp;", " ").strip()
