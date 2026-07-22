import asyncio
import math
import tempfile
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageEnhance, ImageFont


class VoiceRenderer:
    """Render the Rhodes-Island-style voice archive and help panels."""

    # Neutral graphite, paper white and a restrained industrial warning palette.
    COLOR_BG = (12, 15, 17)
    COLOR_PANEL = (20, 24, 27)
    COLOR_CARD = (27, 32, 35)
    COLOR_CARD_ALT = (31, 36, 38)
    COLOR_LINE = (58, 65, 68)
    COLOR_GRID = (28, 33, 36)
    COLOR_TEXT = (238, 240, 236)
    COLOR_SUB = (155, 163, 164)
    COLOR_MUTED = (89, 98, 100)
    COLOR_YELLOW = (245, 199, 0)
    COLOR_CYAN = (18, 184, 196)
    COLOR_RED = (218, 66, 58)
    COLOR_BLACK = (8, 10, 11)

    CANVAS_WIDTH = 1080
    PAGE_MARGIN = 56
    GRID_GAP = 14
    GRID_COLS = 3

    def __init__(self, font_path: str = None, output_dir: str = None):
        self.font_path = font_path
        self._font_cache = {}
        self.output_dir = Path(
            output_dir or Path(tempfile.gettempdir()) / "astrbot_plugin_mrfz"
        )
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _font_candidates(self, *, bold: bool, mono: bool) -> Sequence[Path]:
        base = Path(__file__).parent
        candidates = []
        if self.font_path:
            candidates.append(Path(self.font_path))

        if mono:
            candidates.extend(
                [
                    base / "SarasaMonoSC-Regular.ttf",
                    base / "NotoSansHans-Regular.otf",
                    base / "SourceHanSansSC-Regular.otf",
                    Path("C:/Windows/Fonts/consola.ttf"),
                    Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
                ]
            )
        elif bold:
            candidates.extend(
                [
                    base / "SourceHanSansSC-Bold.otf",
                    base / "SourceHanSansCN-Bold.otf",
                    base / "NotoSansHans-Bold.otf",
                    base / "SourceHanSerifCN-Medium-6.otf",
                    Path("C:/Windows/Fonts/msyhbd.ttc"),
                    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
                    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
                ]
            )
        else:
            candidates.extend(
                [
                    base / "SourceHanSansSC-Regular.otf",
                    base / "SourceHanSansCN-Regular.otf",
                    base / "NotoSansHans-Regular.otf",
                    base / "SourceHanSerifCN-Medium-6.otf",
                    Path("C:/Windows/Fonts/msyh.ttc"),
                    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
                    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                ]
            )
        return candidates

    def _load_font(self, size: int, *, bold: bool = False, mono: bool = False):
        cache_key = (size, bold, mono)
        if cache_key in self._font_cache:
            return self._font_cache[cache_key]

        font = None
        for candidate in self._font_candidates(bold=bold, mono=mono):
            try:
                if candidate.exists() or str(candidate).lower().endswith(".ttc"):
                    font = ImageFont.truetype(str(candidate), size)
                    break
            except (OSError, IOError):
                continue

        if font is None:
            font = ImageFont.load_default()
        self._font_cache[cache_key] = font
        return font

    @staticmethod
    def _new_rgba(size: Tuple[int, int], color=(0, 0, 0, 0)) -> Image.Image:
        return Image.new("RGBA", size, color)

    def _new_output_path(self, prefix: str) -> Path:
        return self.output_dir / f"{prefix}_{uuid.uuid4().hex}.png"

    @staticmethod
    def _save_atomic(image: Image.Image, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            image.save(temp_path, format="PNG")
            temp_path.replace(path)
        finally:
            temp_path.unlink(missing_ok=True)

    @staticmethod
    def _cut_box(x: int, y: int, width: int, height: int, cut: int = 12):
        return [
            (x, y),
            (x + width - cut, y),
            (x + width, y + cut),
            (x + width, y + height),
            (x + cut, y + height),
            (x, y + height - cut),
        ]

    def _draw_cut_panel(
        self,
        draw: ImageDraw.ImageDraw,
        box: Tuple[int, int, int, int],
        *,
        fill,
        outline=None,
        cut: int = 12,
        width: int = 1,
    ) -> None:
        x, y, w, h = box
        points = self._cut_box(x, y, w, h, cut)
        draw.polygon(points, fill=fill)
        if outline:
            draw.line(points + [points[0]], fill=outline, width=width, joint="curve")

    def _fit_text(self, draw, text: str, font, max_width: float) -> str:
        text = str(text)
        if draw.textlength(text, font=font) <= max_width:
            return text
        ellipsis = "…"
        while text and draw.textlength(text + ellipsis, font=font) > max_width:
            text = text[:-1]
        return text + ellipsis

    def _draw_background(self, image: Image.Image, draw: ImageDraw.ImageDraw) -> None:
        width, height = image.size
        draw.rectangle((0, 0, width, height), fill=self.COLOR_BG)

        # Fine terminal grid; intentionally subtle so dense data remains readable.
        for x in range(0, width, 48):
            draw.line((x, 0, x, height), fill=self.COLOR_GRID, width=1)
        for y in range(0, height, 48):
            draw.line((0, y, width, y), fill=self.COLOR_GRID, width=1)

        draw.rectangle((20, 20, width - 20, height - 20), outline=(45, 51, 54), width=1)
        draw.rectangle((20, 20, 27, height - 20), fill=self.COLOR_YELLOW)

        # Registration marks.
        mark = 18
        for x, y, sx, sy in (
            (35, 35, 1, 1),
            (width - 35, 35, -1, 1),
            (35, height - 35, 1, -1),
            (width - 35, height - 35, -1, -1),
        ):
            draw.line((x, y, x + sx * mark, y), fill=self.COLOR_MUTED, width=2)
            draw.line((x, y, x, y + sy * mark), fill=self.COLOR_MUTED, width=2)

    def _draw_hazard(self, draw, x: int, y: int, width: int, height: int) -> None:
        draw.rectangle((x, y, x + width, y + height), fill=self.COLOR_YELLOW)
        stripe = 18
        for start in range(x - height, x + width + height, stripe * 2):
            draw.polygon(
                [
                    (start, y + height),
                    (start + height, y),
                    (start + height + stripe, y),
                    (start + stripe, y + height),
                ],
                fill=self.COLOR_BLACK,
            )

    def _draw_header(self, draw, width: int, *, page_code: str, title: str) -> int:
        left = self.PAGE_MARGIN
        right = width - self.PAGE_MARGIN

        draw.text(
            (left, 42),
            "RHODES ISLAND // PRTS LINK",
            font=self._load_font(14, mono=True),
            fill=self.COLOR_SUB,
        )
        draw.text(
            (left, 64),
            title,
            font=self._load_font(42, bold=True),
            fill=self.COLOR_TEXT,
        )
        draw.rectangle((left, 119, left + 192, 125), fill=self.COLOR_YELLOW)
        draw.rectangle((left + 198, 119, right - 245, 125), fill=self.COLOR_TEXT)

        meta_x = right - 225
        draw.text(
            (meta_x, 42),
            f"FILE / {page_code}",
            font=self._load_font(14, mono=True),
            fill=self.COLOR_YELLOW,
        )
        draw.text(
            (meta_x, 66),
            "VOICE ARCHIVE",
            font=self._load_font(22, bold=True),
            fill=self.COLOR_TEXT,
        )
        draw.text(
            (meta_x, 95),
            "STATUS  ONLINE  ●",
            font=self._load_font(13, mono=True),
            fill=self.COLOR_CYAN,
        )
        self._draw_hazard(draw, right - 36, 42, 36, 83)

        draw.text(
            (left, 139),
            "TERMINAL DATABASE / LOCAL VOICE ASSET INDEX",
            font=self._load_font(12, mono=True),
            fill=self.COLOR_MUTED,
        )
        return 176

    def _draw_section_header(
        self,
        draw,
        y: int,
        number: int,
        title_cn: str,
        title_en: str,
        count: int,
    ) -> int:
        left = self.PAGE_MARGIN
        width = self.CANVAS_WIDTH - self.PAGE_MARGIN * 2
        height = 58

        draw.rectangle((left, y, left + width, y + height), fill=self.COLOR_PANEL)
        draw.rectangle((left, y, left + 6, y + height), fill=self.COLOR_YELLOW)
        draw.text(
            (left + 18, y + 5),
            f"{number:02d}",
            font=self._load_font(38, bold=True, mono=True),
            fill=(66, 73, 75),
        )
        draw.text(
            (left + 84, y + 7),
            title_cn,
            font=self._load_font(22, bold=True),
            fill=self.COLOR_TEXT,
        )
        draw.text(
            (left + 84, y + 34),
            title_en.upper(),
            font=self._load_font(11, mono=True),
            fill=self.COLOR_SUB,
        )
        count_text = f"{count:02d} RECORDS"
        count_font = self._load_font(12, mono=True)
        count_width = draw.textlength(count_text, font=count_font)
        draw.text(
            (left + width - count_width - 18, y + 23),
            count_text,
            font=count_font,
            fill=self.COLOR_YELLOW,
        )
        draw.line(
            (left + 84, y + height - 1, left + width, y + height - 1),
            fill=self.COLOR_LINE,
            width=1,
        )
        return y + height + 14

    def _open_avatar(self, avatar_path: Optional[str], size: int) -> Image.Image:
        if avatar_path and Path(avatar_path).is_file():
            try:
                with Image.open(avatar_path) as source:
                    avatar = source.convert("RGB")
                    side = min(avatar.size)
                    left = (avatar.width - side) // 2
                    top = (avatar.height - side) // 2
                    avatar = avatar.crop((left, top, left + side, top + side))
                    avatar = avatar.resize((size, size), Image.Resampling.LANCZOS)
                    return ImageEnhance.Contrast(avatar).enhance(1.08)
            except (OSError, IOError, ValueError):
                pass

        placeholder = Image.new("RGB", (size, size), self.COLOR_BLACK)
        pdraw = ImageDraw.Draw(placeholder)
        pdraw.line((0, 0, size, size), fill=self.COLOR_LINE, width=2)
        pdraw.line((size, 0, 0, size), fill=self.COLOR_LINE, width=2)
        pdraw.rectangle((8, 8, size - 8, size - 8), outline=self.COLOR_MUTED, width=1)
        return placeholder

    def _paste_cut_avatar(
        self, image: Image.Image, avatar_path: Optional[str], x: int, y: int, size: int
    ) -> None:
        avatar = self._open_avatar(avatar_path, size).convert("RGBA")
        mask = Image.new("L", (size, size), 0)
        mdraw = ImageDraw.Draw(mask)
        cut = 10
        mdraw.polygon(
            [
                (0, 0),
                (size - cut, 0),
                (size, cut),
                (size, size),
                (cut, size),
                (0, size - cut),
            ],
            fill=255,
        )
        avatar.putalpha(mask)
        image.alpha_composite(avatar, (x, y))

    def _draw_language_tags(self, draw, x: int, y: int, languages: List[Dict]) -> None:
        for index, language in enumerate(languages[:6]):
            label = str(language.get("display", "--"))[:2]
            raw_color = language.get("color", self.COLOR_MUTED)
            try:
                color = tuple(raw_color)[:3]
                if len(color) != 3:
                    raise ValueError
            except (TypeError, ValueError):
                color = self.COLOR_MUTED

            tag_w = 29
            tag_x = x + index * (tag_w + 5)
            draw.rectangle((tag_x, y, tag_x + tag_w, y + 19), fill=(42, 48, 51))
            draw.rectangle((tag_x, y + 16, tag_x + tag_w, y + 19), fill=color)
            label_font = self._load_font(11, bold=True)
            label_w = draw.textlength(label, font=label_font)
            draw.text(
                (tag_x + (tag_w - label_w) / 2, y + 1),
                label,
                font=label_font,
                fill=self.COLOR_TEXT,
            )

    def _draw_operator_card(
        self,
        image: Image.Image,
        draw,
        item: Dict,
        x: int,
        y: int,
        width: int,
        height: int,
        index: int,
        *,
        is_skin: bool,
    ) -> None:
        accent = self.COLOR_CYAN if is_skin else self.COLOR_YELLOW
        fill = self.COLOR_CARD_ALT if is_skin else self.COLOR_CARD
        self._draw_cut_panel(
            draw, (x, y, width, height), fill=fill, outline=self.COLOR_LINE
        )
        draw.rectangle((x, y, x + 4, y + height - 10), fill=accent)

        avatar_size = height - 24
        avatar_x, avatar_y = x + 12, y + 12
        self._paste_cut_avatar(
            image, item.get("avatar_path"), avatar_x, avatar_y, avatar_size
        )
        draw.line(
            (
                avatar_x + avatar_size + 7,
                y + 12,
                avatar_x + avatar_size + 7,
                y + height - 12,
            ),
            fill=self.COLOR_LINE,
            width=1,
        )

        text_x = avatar_x + avatar_size + 18
        name_font = self._load_font(21, bold=True)
        name = self._fit_text(
            draw, str(item.get("name", "UNKNOWN")), name_font, width - (text_x - x) - 16
        )
        draw.text((text_x, y + 16), name, font=name_font, fill=self.COLOR_TEXT)

        file_label = "OUTFIT DATA" if is_skin else "OPERATOR FILE"
        draw.text(
            (text_x, y + 47),
            f"{file_label}  /  {index:03d}",
            font=self._load_font(10, mono=True),
            fill=accent,
        )
        self._draw_language_tags(
            draw, text_x, y + height - 34, item.get("languages", [])
        )

        draw.polygon(
            [(x + width - 20, y), (x + width, y + 20), (x + width, y)],
            fill=accent,
        )

    def _draw_custom_card(
        self,
        image: Image.Image,
        draw,
        item: Dict,
        x: int,
        y: int,
        width: int,
        height: int,
        index: int,
    ) -> None:
        self._draw_cut_panel(
            draw, (x, y, width, height), fill=self.COLOR_CARD, outline=self.COLOR_LINE
        )
        avatar_size = height - 24
        self._paste_cut_avatar(
            image, item.get("avatar_path"), x + 12, y + 12, avatar_size
        )

        text_x = x + avatar_size + 25
        trigger = str(item.get("trigger", "UNBOUND"))
        target = str(item.get("target", "--"))
        lang = str(item.get("lang_display", "AUTO"))

        label_font = self._load_font(13, bold=True)
        trigger = self._fit_text(draw, trigger, label_font, width - (text_x - x) - 20)
        label_w = min(
            draw.textlength(trigger, font=label_font) + 16, width - (text_x - x) - 14
        )
        draw.polygon(
            [
                (text_x, y + 14),
                (text_x + label_w - 7, y + 14),
                (text_x + label_w, y + 21),
                (text_x + label_w, y + 38),
                (text_x, y + 38),
            ],
            fill=self.COLOR_YELLOW,
        )
        draw.text((text_x + 7, y + 18), trigger, font=label_font, fill=self.COLOR_BLACK)

        target_font = self._load_font(14)
        target = self._fit_text(draw, target, target_font, width - (text_x - x) - 18)
        draw.text(
            (text_x, y + 51), f"> {target}", font=target_font, fill=self.COLOR_TEXT
        )
        draw.text(
            (text_x, y + height - 23),
            f"AUTO ROUTE / {lang}",
            font=self._load_font(9, mono=True),
            fill=self.COLOR_CYAN,
        )
        draw.text(
            (x + width - 36, y + height - 24),
            f"{index:02d}",
            font=self._load_font(12, bold=True, mono=True),
            fill=self.COLOR_MUTED,
        )

    def _grid_dimensions(self) -> Tuple[int, int]:
        available = self.CANVAS_WIDTH - self.PAGE_MARGIN * 2
        card_width = (
            available - self.GRID_GAP * (self.GRID_COLS - 1)
        ) // self.GRID_COLS
        return available, card_width

    def _section_height(self, count: int, card_height: int) -> int:
        if not count:
            return 0
        rows = math.ceil(count / self.GRID_COLS)
        return 58 + 14 + rows * card_height + max(0, rows - 1) * self.GRID_GAP + 28

    async def render_help(self) -> str:
        """Render the help panel outside the event loop."""
        return await asyncio.to_thread(self._render_help_logic)

    def _render_help_logic(self) -> str:
        width = self.CANVAS_WIDTH
        commands = [
            (
                "/mrfz [角色] [语音] [语言]",
                "查询并播放干员语音；语音和语言均可省略",
                "/mrfz 凯尔希 问候 中文",
            ),
            ("/mrfz_list", "查看本地已登记干员、时装和语言索引", "/mrfz_list"),
            ("/mrfz_fetch [角色名]", "从 PRTS Wiki 获取干员语音资料", "/mrfz_fetch 陈"),
            (
                "/mrfz_bind [触发] [角色] [语音] [语言]",
                "建立一条自定义快捷触发指令",
                "/mrfz_bind 早安 阿米娅 问候 中文",
            ),
            ("/mrfz_unbind [触发词]", "移除已经登记的快捷触发词", "/mrfz_unbind 早安"),
        ]
        row_height = 118
        start_y = 190
        total_height = start_y + len(commands) * (row_height + 14) + 82

        image = self._new_rgba((width, total_height), self.COLOR_BG + (255,))
        draw = ImageDraw.Draw(image)
        self._draw_background(image, draw)
        self._draw_header(draw, width, page_code="MAN-01", title="终端操作手册")

        y = start_y
        for index, (command, description, example) in enumerate(commands, 1):
            x = self.PAGE_MARGIN
            panel_w = width - self.PAGE_MARGIN * 2
            self._draw_cut_panel(
                draw,
                (x, y, panel_w, row_height),
                fill=self.COLOR_CARD,
                outline=self.COLOR_LINE,
            )
            draw.rectangle((x, y, x + 8, y + row_height - 12), fill=self.COLOR_YELLOW)
            draw.text(
                (x + 24, y + 16),
                f"{index:02d}",
                font=self._load_font(38, bold=True, mono=True),
                fill=(72, 79, 81),
            )
            draw.text(
                (x + 92, y + 14),
                command,
                font=self._load_font(22, bold=True, mono=True),
                fill=self.COLOR_YELLOW,
            )
            draw.text(
                (x + 92, y + 53),
                description,
                font=self._load_font(17),
                fill=self.COLOR_TEXT,
            )
            draw.text(
                (x + 92, y + 83),
                f"EXAMPLE  //  {example}",
                font=self._load_font(12, mono=True),
                fill=self.COLOR_SUB,
            )
            draw.text(
                (x + panel_w - 138, y + 87),
                "ACCESS / OK",
                font=self._load_font(10, mono=True),
                fill=self.COLOR_CYAN,
            )
            y += row_height + 14

        self._draw_footer(draw, width, total_height)
        output_path = self._new_output_path("help")
        self._save_atomic(image.convert("RGB"), output_path)
        return str(output_path.absolute())

    def render_image(self, data: Dict, voice_descriptions: List[str]) -> str:
        """Render the detailed archive list using the existing renderer API."""
        custom_commands = list(data.get("custom_commands") or [])
        operators = list(data.get("operators") or [])
        skin_operators = list(data.get("skin_operators") or [])

        custom_card_h = 108
        operator_card_h = 112
        modules_cols = 5
        modules_rows = (
            math.ceil(len(voice_descriptions) / modules_cols)
            if voice_descriptions
            else 0
        )
        modules_h = 58 + 14 + modules_rows * 42 + max(0, modules_rows - 1) * 8 + 30

        total_height = 190
        total_height += self._section_height(len(custom_commands), custom_card_h)
        total_height += self._section_height(len(operators), operator_card_h)
        total_height += self._section_height(len(skin_operators), operator_card_h)
        total_height += modules_h + 72

        image = self._new_rgba(
            (self.CANVAS_WIDTH, total_height), self.COLOR_BG + (255,)
        )
        draw = ImageDraw.Draw(image)
        self._draw_background(image, draw)
        self._draw_header(
            draw, self.CANVAS_WIDTH, page_code="DB-03", title="干员语音档案"
        )

        _, card_width = self._grid_dimensions()
        current_y = 190
        section_no = 1

        def draw_grid_section(items, cn, en, card_height, *, custom=False, skin=False):
            nonlocal current_y, section_no
            if not items:
                return
            current_y = self._draw_section_header(
                draw, current_y, section_no, cn, en, len(items)
            )
            section_no += 1
            for index, item in enumerate(items):
                row, col = divmod(index, self.GRID_COLS)
                x = self.PAGE_MARGIN + col * (card_width + self.GRID_GAP)
                y = current_y + row * (card_height + self.GRID_GAP)
                if custom:
                    self._draw_custom_card(
                        image, draw, item, x, y, card_width, card_height, index + 1
                    )
                else:
                    self._draw_operator_card(
                        image,
                        draw,
                        item,
                        x,
                        y,
                        card_width,
                        card_height,
                        index + 1,
                        is_skin=skin,
                    )
            rows = math.ceil(len(items) / self.GRID_COLS)
            current_y += rows * card_height + max(0, rows - 1) * self.GRID_GAP + 28

        draw_grid_section(
            custom_commands,
            "自定义快捷指令",
            "CUSTOM SHORTCUT ROUTES",
            custom_card_h,
            custom=True,
        )
        draw_grid_section(
            operators,
            "已登记干员",
            "REGISTERED OPERATORS",
            operator_card_h,
        )
        draw_grid_section(
            skin_operators,
            "时装语音记录",
            "OUTFIT VOICE RECORDS",
            operator_card_h,
            skin=True,
        )

        current_y = self._draw_section_header(
            draw,
            current_y,
            section_no,
            "系统语音模块",
            "SYSTEM VOICE MODULES",
            len(voice_descriptions),
        )
        module_gap = 10
        module_width = (
            self.CANVAS_WIDTH - self.PAGE_MARGIN * 2 - module_gap * (modules_cols - 1)
        ) // modules_cols
        module_height = 42

        for index, description in enumerate(voice_descriptions):
            row, col = divmod(index, modules_cols)
            x = self.PAGE_MARGIN + col * (module_width + module_gap)
            y = current_y + row * (module_height + 8)
            self._draw_cut_panel(
                draw,
                (x, y, module_width, module_height),
                fill=(24, 29, 31),
                outline=self.COLOR_LINE,
                cut=7,
            )
            draw.rectangle((x, y, x + 4, y + module_height - 7), fill=self.COLOR_YELLOW)
            draw.text(
                (x + 12, y + 6),
                f"M-{index + 1:02d}",
                font=self._load_font(9, bold=True, mono=True),
                fill=self.COLOR_CYAN,
            )
            text_font = self._load_font(13)
            fitted = self._fit_text(
                draw, str(description), text_font, module_width - 60
            )
            draw.text((x + 54, y + 12), fitted, font=text_font, fill=self.COLOR_TEXT)

        self._draw_footer(draw, self.CANVAS_WIDTH, total_height)
        output_path = self._new_output_path("list")
        self._save_atomic(image.convert("RGB"), output_path)
        return str(output_path.absolute())

    def _draw_footer(self, draw, width: int, height: int) -> None:
        y = height - 58
        left = self.PAGE_MARGIN
        right = width - self.PAGE_MARGIN
        draw.line((left, y, right, y), fill=self.COLOR_LINE, width=1)
        draw.rectangle((left, y - 2, left + 90, y + 2), fill=self.COLOR_YELLOW)
        draw.text(
            (left, y + 12),
            "RHODES ISLAND / VOICE ARCHIVE TERMINAL",
            font=self._load_font(10, mono=True),
            fill=self.COLOR_MUTED,
        )
        footer = "GENERATED BY astrbot_plugin_mrfz  //  by bushikq"
        font = self._load_font(10, mono=True)
        footer_w = draw.textlength(footer, font=font)
        draw.text((right - footer_w, y + 12), footer, font=font, fill=self.COLOR_MUTED)
