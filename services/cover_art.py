"""Единая визуальная система обложек классов Chatra: палитра, иконки, промпт
и локальный рендер (фолбэк + наложение иконки).

Обложка собирается из ДВУХ слоёв, и это принципиально:

  1. фон — абстрактная композиция (генерирует OpenAI, см. services/
     cover_generator.py; при любом сбое — локальный градиент из render_background);
  2. предметная иконка — рисуется ЗДЕСЬ примитивами Pillow поверх фона.

Иконку намеренно не рисует модель: символы вроде Σ, спирали ДНК или колбы
генеративные модели воспроизводят нестабильно (лишние штрихи, зеркальные
буквы, псевдотекст), а обложка предмета должна быть узнаваемой всегда. Слой
кода даёт одинаково чёткий глиф в каждой обложке — и в AI-, и в фолбэк-версии,
поэтому обе выглядят одной коллекцией.

Композитинг сделан на бэкенде, а не оверлеем в UI, сознательно: обложка
показывается больше чем в десяти местах веба и мобильного приложения
(карточки списка, архив, админка, шапка класса, диалог вступления по коду),
и все они уже умеют показывать одну картинку по cover_image/cover_thumbnail.
Единая готовая картинка не требует править ни одно из этих мест и остаётся
корректной везде, куда обложку добавят потом.
"""
import colorsys
import logging
import math
import random

logger = logging.getLogger(__name__)

# Ландшафт 3:2, а не квадрат: все существующие места показа — широкие баннеры
# (карточка класса 200px высотой на всю ширину, SliverAppBar 220px в
# приложении). Квадрат в них жёстко обрезался бы сверху и снизу.
COVER_WIDTH = 1536
COVER_HEIGHT = 1024

# Иконка ставится по центру кадра: сверху-слева на обложке лежит кнопка
# «назад», сверху-справа — код класса и «редактировать», снизу — название и
# описание (см. class_cover_sliver.dart, pages/index.vue). Центр — единственная
# зона, свободная во всех местах показа и переживающая любой кроп.
_ICON_CENTER_X = 0.5
_ICON_CENTER_Y = 0.44
_ICON_HEIGHT_RATIO = 0.34   # доля высоты кадра под глиф
_GLYPH_SUPERSAMPLE = 3      # ImageDraw без сглаживания — рисуем крупнее и ужимаем


# ── Палитра ─────────────────────────────────────────────────────────────────
# hex — акцент (он же цвет превью в UI и подсказка модели), deep — тёмный
# конец градиента фолбэка. prompt — как назвать цвет модели словами: hex-код
# в промпте image-модели работает плохо, название цвета — надёжно.
PALETTE: dict[str, dict[str, str]] = {
    "blue":   {"hex": "#0A84FF", "deep": "#0A2A5E", "prompt": "vivid azure blue"},
    "purple": {"hex": "#8B5CF6", "deep": "#2B1857", "prompt": "rich violet purple"},
    "green":  {"hex": "#22C55E", "deep": "#0B3722", "prompt": "fresh emerald green"},
    "orange": {"hex": "#F97316", "deep": "#54220A", "prompt": "warm amber orange"},
    "red":    {"hex": "#EF4444", "deep": "#511616", "prompt": "deep coral red"},
    "pink":   {"hex": "#EC4899", "deep": "#4F1030", "prompt": "soft magenta pink"},
    # Фирменный бирюзовый Chatra (C.teal в lib/theme/app_theme.dart).
    "teal":   {"hex": "#00B1C9", "deep": "#00303A", "prompt": "bright cyan teal"},
    "indigo": {"hex": "#6366F1", "deep": "#1C1D52", "prompt": "deep indigo"},
}

DEFAULT_COLOR = "teal"

# subject — предмет, для которого иконка предлагается по умолчанию (клиенты
# показывают подсказку рядом с иконкой). motif — как описать её модели, чтобы
# фон перекликался с предметом, оставаясь абстрактным.
ICONS: dict[str, dict[str, str]] = {
    "sigma":   {"subject": "Mathematics",      "motif": "summation and mathematical notation"},
    "atom":    {"subject": "Physics",          "motif": "atomic orbits and particle trajectories"},
    "flask":   {"subject": "Chemistry",        "motif": "laboratory glassware and molecular bonds"},
    "dna":     {"subject": "Biology",          "motif": "a double helix and organic cell structures"},
    "code":    {"subject": "Computer Science", "motif": "angle brackets, grids and data flow"},
    "column":  {"subject": "History",          "motif": "classical columns and architectural pediments"},
    "globe":   {"subject": "Geography",        "motif": "a globe, meridians and contour lines"},
    "letter":  {"subject": "English",          "motif": "letterforms and language structure"},
    "book":    {"subject": "Literature",       "motif": "open pages and flowing paper"},
    "chart":   {"subject": "Economics",        "motif": "ascending bars and trend lines"},
    "palette": {"subject": "Art",              "motif": "a painter palette and brush strokes"},
    "note":    {"subject": "Music",            "motif": "musical notes and sound waves"},
}

DEFAULT_ICON = "book"


def normalize_color(value: str | None) -> str:
    """Слаг цвета из палитры; неизвестное/пустое → DEFAULT_COLOR."""
    key = (value or "").strip().lower()
    return key if key in PALETTE else DEFAULT_COLOR


def normalize_icon(value: str | None) -> str:
    """Слаг иконки из набора; неизвестное/пустое → DEFAULT_ICON."""
    key = (value or "").strip().lower()
    return key if key in ICONS else DEFAULT_ICON


def catalog() -> dict:
    """Палитра и набор иконок для клиентов — веб и приложение строят пикеры
    из этого ответа, чтобы набор цветов/иконок нигде не разъезжался."""
    return {
        "colors": [
            {"id": slug, "hex": v["hex"], "deep": v["deep"]}
            for slug, v in PALETTE.items()
        ],
        "icons": [
            {"id": slug, "subject": v["subject"]} for slug, v in ICONS.items()
        ],
        "default_color": DEFAULT_COLOR,
        "default_icon": DEFAULT_ICON,
    }


# ── Промпт ──────────────────────────────────────────────────────────────────
# Общий каркас стиля — один на все обложки. Меняются только цвет и мотив,
# поэтому предметы выглядят как одна коллекция, а не как случайные картинки.
_BASE_STYLE = (
    "Premium minimal artwork for an educational app cover. "
    "Clean modern abstract composition, sophisticated flat vector-inspired visual "
    "language, subtle depth, soft geometric shapes with smooth edges, restrained "
    "detail, elegant balanced composition, generous negative space. "
    "Exactly one dominant accent colour: {color}. Deep dark background in the same "
    "hue family, accent used sparingly against it. "
    "Abstract geometry loosely evoking {motif} — suggestion only, never a literal "
    "or diagrammatic illustration. "
    "Flat 2D, matte finish, even level of detail across the whole frame. "
    "Keep the centre of the frame calm, dark and uncluttered. "
    "Strictly no text, no letters, no numbers, no words, no symbols that read as "
    "writing, no logos, no watermarks, no signatures, no UI elements. "
    "No photography, no people, no hands, no faces, no realistic 3D renders, "
    "no glossy reflections, no clutter, no busy patterns. "
    "One consistent design system: this image must look like it was made by the "
    "same designer, in the same session, as every other cover in the set."
)

# Вариации для Regenerate. Стиль и цвет остаются теми же — меняется только
# раскладка, поэтому повторная генерация даёт другую картинку, а не другой стиль.
_COMPOSITIONS = (
    "Composition: large soft shapes drifting in from the lower left, empty space upper right.",
    "Composition: a slow diagonal band of shapes from the upper left to the lower right.",
    "Composition: concentric arcs opening from the right edge, quiet space on the left.",
    "Composition: a loose scatter of overlapping translucent shapes near the top edge.",
    "Composition: two overlapping planes meeting near the lower third, wide calm sky above.",
    "Composition: a soft radial glow behind sparse geometry at the outer edges.",
)


def build_prompt(color: str, icon: str, seed: int | None = None) -> str:
    """Единый промпт Chatra под выбранные цвет и иконку.

    seed меняет только раскладку (одна фраза из _COMPOSITIONS) — Images API не
    принимает seed как параметр, поэтому вариативность для Regenerate вносим
    текстом промпта.
    """
    color = normalize_color(color)
    icon = normalize_icon(icon)
    rng = random.Random(seed)
    return " ".join((
        _BASE_STYLE.format(color=PALETTE[color]["prompt"], motif=ICONS[icon]["motif"]),
        rng.choice(_COMPOSITIONS),
    ))


# ── Цвет ────────────────────────────────────────────────────────────────────
def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _shift(rgb: tuple[int, int, int], *, light: float = 1.0, sat: float = 1.0):
    """Осветлить/затемнить и подкрутить насыщенность в HLS — линейное
    умножение RGB уводит оттенок, а нам нужен тот же цвет другой яркости."""
    r, g, b = (c / 255 for c in rgb)
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    r, g, b = colorsys.hls_to_rgb(h, min(1.0, l * light), min(1.0, s * sat))
    return round(r * 255), round(g * 255), round(b * 255)


# ── Фолбэк-фон ──────────────────────────────────────────────────────────────
def render_background(color: str, seed: int | None = None):
    """Локальный фон в том же визуальном языке, что и AI-версия: диагональный
    градиент выбранного цвета + несколько крупных мягких геометрических пятен.

    Используется, когда генерация недоступна (нет ключа, ошибка/таймаут
    OpenAI, исчерпан бюджет, сбой хранилища), и при создании класса — чтобы
    обложка была готова мгновенно и класс никогда не оставался без неё.
    """
    from PIL import Image, ImageDraw, ImageFilter

    color = normalize_color(color)
    accent = _rgb(PALETTE[color]["hex"])
    deep = _rgb(PALETTE[color]["deep"])
    rng = random.Random(seed)

    # Градиент считается на маленьком холсте и растягивается бикубиком: попиксельный
    # проход по 1536x1024 занял бы секунды, здесь — доли миллисекунды.
    small = Image.new("RGB", (64, 64))
    px = small.load()
    for y in range(64):
        for x in range(64):
            t = (x / 63 * 0.62 + (1 - y / 63) * 0.38)
            px[x, y] = tuple(
                round(deep[i] + (accent[i] - deep[i]) * (t ** 1.7) * 0.85)
                for i in range(3)
            )
    img = small.resize((COVER_WIDTH, COVER_HEIGHT), Image.BICUBIC)

    # Мягкие пятна рисуются на отдельном слое и размываются целиком — иначе
    # жёсткие края эллипсов ImageDraw выдали бы «клипарт».
    shapes = Image.new("RGBA", (COVER_WIDTH, COVER_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(shapes)
    glow = _shift(accent, light=1.25, sat=0.9)
    for _ in range(4):
        r = rng.uniform(0.22, 0.46) * COVER_HEIGHT
        cx = rng.uniform(-0.1, 1.1) * COVER_WIDTH
        cy = rng.uniform(-0.1, 1.1) * COVER_HEIGHT
        draw.ellipse((cx - r, cy - r, cx + r, cy + r),
                     fill=(*glow, rng.randint(26, 52)))
    shapes = shapes.filter(ImageFilter.GaussianBlur(COVER_HEIGHT * 0.045))
    img = Image.alpha_composite(img.convert("RGBA"), shapes)

    # Тонкие дуги — отдельным, почти не размытым слоём: под тем же 46-пиксельным
    # блюром, что у пятен, шестипиксельная линия исчезала бы полностью.
    arcs = Image.new("RGBA", (COVER_WIDTH, COVER_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(arcs)
    for _ in range(3):
        r = rng.uniform(0.45, 0.95) * COVER_HEIGHT
        cx = rng.uniform(0.0, 1.0) * COVER_WIDTH
        cy = rng.uniform(0.0, 1.0) * COVER_HEIGHT
        start = rng.uniform(0, 360)
        draw.arc((cx - r, cy - r, cx + r, cy + r), start, start + rng.uniform(60, 140),
                 fill=(*glow, 110), width=max(2, round(COVER_HEIGHT * 0.005)))
    arcs = arcs.filter(ImageFilter.GaussianBlur(COVER_HEIGHT * 0.004))

    return Image.alpha_composite(img, arcs).convert("RGB")


# ── Глифы иконок ────────────────────────────────────────────────────────────
# Координаты нормированы в [0,1] по стороне квадрата глифа: одна и та же
# запись рисуется в любом масштабе, и все иконки одинаковой «весовой» толщины.
def _line(draw, pts, n, w, fill):
    draw.line([(x * n, y * n) for x, y in pts], fill=fill, width=w, joint="curve")


def _ellipse(draw, box, n, w, fill):
    x0, y0, x1, y1 = box
    draw.ellipse((x0 * n, y0 * n, x1 * n, y1 * n), outline=fill, width=w)


def _disc(draw, cx, cy, r, n, fill):
    draw.ellipse(((cx - r) * n, (cy - r) * n, (cx + r) * n, (cy + r) * n), fill=fill)


def _rotated(n, angle, painter):
    """Слой с фигурой, повёрнутый на angle. ImageDraw умеет только оси-
    параллельные эллипсы — орбиты атома иначе не нарисовать."""
    from PIL import Image, ImageDraw

    layer = Image.new("L", (n, n), 0)
    painter(ImageDraw.Draw(layer))
    return layer.rotate(angle, resample=Image.BICUBIC, fillcolor=0)


def _glyph_sigma(draw, n, w, fill):
    _line(draw, [(.78, .13), (.22, .13), (.52, .5), (.22, .87), (.78, .87)], n, w, fill)


def _glyph_atom(draw, n, w, fill):
    _disc(draw, .5, .5, .075, n, fill)


def _glyph_atom_layers(n, w):
    box = (.5 - .48, .5 - .20, .5 + .48, .5 + .20)
    return [
        _rotated(n, angle, lambda d: _ellipse(d, box, n, w, 255))
        for angle in (0, 60, 120)
    ]


def _glyph_flask(draw, n, w, fill):
    _line(draw, [(.37, .12), (.63, .12)], n, w, fill)                 # горлышко сверху
    _line(draw, [(.43, .12), (.43, .40), (.15, .87), (.85, .87), (.57, .40), (.57, .12)],
          n, w, fill)
    _disc(draw, .40, .70, .045, n, fill)                              # пузырьки
    _disc(draw, .56, .77, .033, n, fill)


def _glyph_dna(draw, n, w, fill):
    # Ровно один период на всю высоту: нити расходятся дважды и пересекаются
    # сверху, по центру и снизу — та самая читаемая «двойная спираль».
    # Полтора периода складывались в стопку линз и переставали читаться.
    def dx(t):
        return .28 * math.sin(2 * math.pi * t)

    strand = [(i / 48, .10 + .80 * (i / 48)) for i in range(49)]
    _line(draw, [(.5 + dx(t), y) for t, y in strand], n, w, fill)
    _line(draw, [(.5 - dx(t), y) for t, y in strand], n, w, fill)
    # Перекладины только там, где нити заметно разошлись, и чуть тоньше нитей —
    # иначе сливаются с контуром в одно пятно.
    for t in (.14, .25, .36, .64, .75, .86):
        y, d = .10 + .80 * t, dx(t)
        _line(draw, [(.5 + d, y), (.5 - d, y)], n, max(2, round(w * 0.55)), fill)


def _glyph_code(draw, n, w, fill):
    _line(draw, [(.34, .26), (.13, .5), (.34, .74)], n, w, fill)
    _line(draw, [(.66, .26), (.87, .5), (.66, .74)], n, w, fill)
    _line(draw, [(.585, .18), (.415, .82)], n, max(2, round(w * 0.72)), fill)


def _glyph_column(draw, n, w, fill):
    _line(draw, [(.09, .33), (.5, .10), (.91, .33), (.09, .33)], n, w, fill)   # фронтон
    _line(draw, [(.15, .40), (.85, .40)], n, w, fill)                          # капитель
    for x in (.28, .5, .72):
        _line(draw, [(x, .40), (x, .82)], n, w, fill)                          # колонны
    _line(draw, [(.12, .88), (.88, .88)], n, w, fill)                          # стилобат


def _glyph_globe(draw, n, w, fill):
    _ellipse(draw, (.10, .10, .90, .90), n, w, fill)
    _ellipse(draw, (.30, .10, .70, .90), n, w, fill)   # меридиан
    _line(draw, [(.10, .5), (.90, .5)], n, w, fill)    # экватор
    _line(draw, [(.205, .285), (.795, .285)], n, w, fill)
    _line(draw, [(.205, .715), (.795, .715)], n, w, fill)


def _glyph_letter(draw, n, w, fill):
    _line(draw, [(.15, .88), (.5, .12), (.85, .88)], n, w, fill)
    _line(draw, [(.295, .62), (.705, .62)], n, w, fill)


def _glyph_book(draw, n, w, fill):
    _line(draw, [(.5, .26), (.5, .88)], n, w, fill)                                  # корешок
    _line(draw, [(.5, .26), (.30, .18), (.12, .22), (.12, .80), (.30, .78), (.5, .88)], n, w, fill)
    _line(draw, [(.5, .26), (.70, .18), (.88, .22), (.88, .80), (.70, .78), (.5, .88)], n, w, fill)


def _glyph_chart(draw, n, w, fill):
    _line(draw, [(.16, .12), (.16, .86), (.90, .86)], n, w, fill)                    # оси
    bar = max(3, round(w * 1.5))
    for x, top in ((.34, .62), (.53, .42), (.72, .24)):
        _line(draw, [(x, .86), (x, top)], n, bar, fill)


def _glyph_palette(draw, n, w, fill):
    _ellipse(draw, (.08, .14, .92, .86), n, w, fill)
    _disc(draw, .62, .60, .105, n, fill)                                             # отверстие
    for cx, cy in ((.28, .38), (.46, .28), (.66, .34), (.26, .60)):
        _disc(draw, cx, cy, .062, n, fill)                                           # краски


def _glyph_note(draw, n, w, fill):
    draw.ellipse((.20 * n, .64 * n, .56 * n, .88 * n), fill=fill)                    # головка
    _line(draw, [(.545, .76), (.545, .14)], n, w, fill)                              # штиль
    _line(draw, [(.545, .14), (.74, .24), (.80, .38), (.76, .48)], n, w, fill)       # флажок


_GLYPHS = {
    "sigma": _glyph_sigma,
    "atom": _glyph_atom,
    "flask": _glyph_flask,
    "dna": _glyph_dna,
    "code": _glyph_code,
    "column": _glyph_column,
    "globe": _glyph_globe,
    "letter": _glyph_letter,
    "book": _glyph_book,
    "chart": _glyph_chart,
    "palette": _glyph_palette,
    "note": _glyph_note,
}


def render_glyph(icon: str, size: int):
    """Маска глифа (режим "L", size×size) со сглаженными краями.

    ImageDraw не сглаживает — рисуем в _GLYPH_SUPERSAMPLE раз крупнее и
    уменьшаем LANCZOS, иначе на обложке видна лесенка по диагоналям (Σ, A, ДНК).
    """
    from PIL import Image, ImageDraw

    icon = normalize_icon(icon)
    n = size * _GLYPH_SUPERSAMPLE
    mask = Image.new("L", (n, n), 0)
    draw = ImageDraw.Draw(mask)
    width = max(2, round(n * 0.072))

    _GLYPHS[icon](draw, n, width, 255)
    if icon == "atom":
        for layer in _glyph_atom_layers(n, width):
            mask.paste(255, (0, 0), layer)

    return mask.resize((size, size), Image.LANCZOS)


def apply_icon(base, icon: str):
    """Накладывает белый глиф предмета на готовый фон (AI или фолбэк).

    Под глифом — размытая тёмная копия его же маски: без неё светлая
    AI-картинка «съедала» бы белый символ. Тень строится из самой маски, а не
    рисуется отдельной фигурой, поэтому обводит глиф точно по форме.
    """
    from PIL import Image, ImageFilter

    icon = normalize_icon(icon)
    img = base.convert("RGBA")
    size = round(img.height * _ICON_HEIGHT_RATIO)
    mask = render_glyph(icon, size)

    left = round(img.width * _ICON_CENTER_X - size / 2)
    top = round(img.height * _ICON_CENTER_Y - size / 2)

    pad = round(size * 0.22)
    shadow_mask = Image.new("L", (size + pad * 2, size + pad * 2), 0)
    shadow_mask.paste(mask, (pad, pad))
    shadow_mask = shadow_mask.filter(ImageFilter.GaussianBlur(size * 0.055))
    shadow_mask = shadow_mask.point(lambda v: min(255, round(v * 1.35)))
    shadow = Image.new("RGBA", shadow_mask.size, (5, 8, 12, 255))
    shadow.putalpha(shadow_mask)
    img.alpha_composite(shadow, (left - pad, top - pad + round(size * 0.02)))

    glyph = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    glyph.putalpha(mask)
    img.alpha_composite(glyph, (left, top))
    return img.convert("RGB")


def render_fallback_cover(color: str, icon: str, seed: int | None = None):
    """Полноценная обложка без единого обращения к внешним сервисам."""
    return apply_icon(render_background(color, seed), icon)


def encode_png(img) -> bytes:
    """PNG-байты. Дальше картинка всё равно пересжимается в WebP + миниатюру
    существующим services/image_processing.process_cover_image."""
    import io

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=False)
    return buf.getvalue()
