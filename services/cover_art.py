"""Единая визуальная система обложек классов Chatra: палитра, промпт и
локальный рендер фона.

Обложка — это ДВА независимых слоя, и разделение принципиально:

  1. фон — плоский графический баннер (генерирует OpenAI, см. services/
     cover_generator.py; при любом сбое — локальный render_background);
  2. предметная иконка — НЕ часть картинки. Её рисуют клиенты поверх фона
     нативным компонентом (components/classes/SubjectCover.vue на вебе,
     widgets/subject_cover.dart в приложении).

Модель отвечает только за фон и композицию. Символы вроде Σ, спирали ДНК или
колбы генеративные модели воспроизводят нестабильно (лишние штрихи, зеркальные
буквы, псевдотекст), поэтому иконка живёт в UI: там она всегда одного размера,
одной толщины линии и одного стиля во всех предметах, её можно перекрасить или
заменить без перегенерации всех обложек.

Визуальный ориентир — обложка курса, нарисованная графическим дизайнером для
premium-образовательного продукта: цельная абстрактная композиция из немногих
крупных форм, перетекающие кривые, наложения и прозрачность, богатый цвет одной
семьи. НЕ дашборд, не инфографика, не паттерн из точек и прямоугольников.

Специальную пустую зону под иконку модель больше НЕ оставляет: сетки и «чистые
полосы» ровно и превращали картинку в SaaS-фон. Иконка кладётся UI-слоем поверх
готовой композиции, а её читаемость обеспечивает сам слой (см. ICON_ON_ARTWORK).
"""
import colorsys
import logging
import random

logger = logging.getLogger(__name__)

# Ландшафт 3:2. Квадрат здесь уже был и провалился: обложку показывают широкой
# полосой (карточка каталога ~2.1:1, шапка класса ~3.3:1), поэтому от квадрата
# в кадр попадала только средняя полоса — 48% и 30% высоты соответственно.
COVER_WIDTH = 1536
COVER_HEIGHT = 1024

# Иконка ложится поверх произвольной точки композиции, а не в зарезервированную
# пустоту. Клиенты рисуют её белой с мягкой тенью — единственный вариант,
# который читается на любом кадре: сама композиция теперь насыщенная и её
# яркость в центре заранее неизвестна. Константа держит это решение в одном
# месте вместе с промптом, который его и породил.
ICON_ON_ARTWORK = "white-with-shadow"


# ── Палитра ─────────────────────────────────────────────────────────────────
# hex   — акцент бренда: свотч в пикере и подсветка выбора.
# base  — основной тон композиции: насыщенный средний цвет, из которого строится
#         вся обложка. Здесь уже побывали почти чёрные значения (вышло «ночно»)
#         и очень светлая пастель (вышло блёкло и по-дашбордному) — держим
#         выразительную середину.
# ink   — цвет иконки, когда она рисуется В ТОН. Сейчас клиенты рисуют её белой
#         (см. ICON_ON_ARTWORK), но значение оставлено: пикер подсвечивает им
#         выбранную иконку, и оно же понадобится, если решим вернуть иконку в тон.
# prompt — как описать цвет модели словами. Не один тон, а СЕМЬЯ оттенков:
#         промпт просит строить композицию на нескольких оттенках одного цвета,
#         и семья должна быть названа явно — иначе модель делает однотонную
#         заливку. Hex-коды image-модель понимает плохо, названия — надёжно.
PALETTE: dict[str, dict[str, str]] = {
    "blue": {
        "hex": "#0A84FF", "base": "#3B82F6", "ink": "#1D4ED8",
        "prompt": "blue — luminous cornflower, sky blue and soft cobalt, with lighter azure tints and pale translucent highlights",
    },
    "purple": {
        "hex": "#8B5CF6", "base": "#7C5CE6", "ink": "#6D28D9",
        "prompt": "purple — luminous violet, lavender and periwinkle, with lighter lilac tints and pale translucent highlights",
    },
    "green": {
        "hex": "#22C55E", "base": "#12A970", "ink": "#047857",
        "prompt": "green — luminous emerald, mint and spring green, with lighter sage tints and pale translucent highlights",
    },
    "orange": {
        "hex": "#F97316", "base": "#F4842B", "ink": "#C2410C",
        "prompt": "orange — luminous warm orange, peach and amber, with lighter apricot tints and pale translucent highlights",
    },
    "red": {
        "hex": "#EF4444", "base": "#E4534F", "ink": "#B91C1C",
        "prompt": "red — luminous coral, rose and warm crimson, with lighter blush tints and pale translucent highlights",
    },
    "pink": {
        "hex": "#EC4899", "base": "#E8559C", "ink": "#BE185D",
        "prompt": "pink — luminous pink, magenta and soft rose, with lighter petal tints and pale translucent highlights",
    },
    # Фирменный бирюзовый Chatra (C.teal в lib/theme/app_theme.dart).
    "teal": {
        "hex": "#00B1C9", "base": "#12A2B5", "ink": "#0E7490",
        "prompt": "teal — luminous turquoise, aqua and teal, with lighter seafoam tints and pale translucent highlights",
    },
    "indigo": {
        "hex": "#6366F1", "base": "#5A5FE0", "ink": "#4338CA",
        "prompt": "indigo — luminous periwinkle, blue-violet and indigo, with lighter cornflower tints and pale translucent highlights",
    },
}

DEFAULT_COLOR = "teal"

# subject — предмет, для которого иконка предлагается по умолчанию (клиенты
# показывают подсказку рядом с иконкой).
# motif — АБСТРАКТНЫЙ визуальный язык предмета. Не «нарисуй колбу» и не список
# фигур («сетка, точки, прямоугольники») — второе как раз и превращало обложку
# в дашборд. Здесь описывается характер форм и движения, а конкретную
# композицию модель придумывает сама.
ICONS: dict[str, dict[str, str]] = {
    "sigma": {
        "subject": "Mathematics",
        "motif": "abstract mathematical visual language: elegant geometric "
                 "relationships, arcs, curves, coordinate-like lines, circles and "
                 "subtle mathematical structures",
    },
    "atom": {
        "subject": "Physics",
        "motif": "abstract physics visual language: flowing waves, orbital curves, "
                 "particles, energy fields and subtle motion",
    },
    "flask": {
        "subject": "Chemistry",
        "motif": "abstract chemistry visual language: molecular relationships, "
                 "connected structures, fluid shapes and subtle "
                 "laboratory-inspired geometry",
    },
    "dna": {
        "subject": "Biology",
        "motif": "abstract biological visual language: organic cellular forms, "
                 "branching structures, flowing membranes and soft circular forms",
    },
    "code": {
        "subject": "Computer Science",
        "motif": "abstract computational visual language: connected nodes, flowing "
                 "paths, layered structures and network-like geometry",
    },
    "column": {
        "subject": "History",
        "motif": "abstract historical visual language: architectural geometry, "
                 "classical proportions, arches and columns reduced to simple "
                 "graphic forms",
    },
    "globe": {
        "subject": "Geography",
        "motif": "abstract geographic visual language: flowing map-like curves, "
                 "contour lines, globe-inspired arcs and terrain-like organic forms",
    },
    "letter": {
        "subject": "English",
        "motif": "abstract language visual language: flowing ribbons of script-like "
                 "movement, soft speech-shaped forms and elegant connecting curves",
    },
    "book": {
        "subject": "Literature",
        "motif": "abstract literary visual language: flowing pages, layered forms, "
                 "elegant lines and abstract book-like geometry",
    },
    "chart": {
        "subject": "Economics",
        "motif": "abstract economic visual language: rising curves, balanced "
                 "geometric forms and subtle charts transformed into elegant "
                 "abstract shapes",
    },
    "palette": {
        "subject": "Art",
        "motif": "abstract artistic visual language: expressive curves, layered "
                 "shapes, creative colour blocks and organic composition",
    },
    "note": {
        "subject": "Music",
        "motif": "abstract musical visual language: flowing rhythm lines, waves, "
                 "circular motion and elegant repeating curves",
    },
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
            {"id": slug, "hex": v["hex"], "base": v["base"], "ink": v["ink"]}
            for slug, v in PALETTE.items()
        ],
        "icons": [
            {"id": slug, "subject": v["subject"]} for slug, v in ICONS.items()
        ],
        "default_color": DEFAULT_COLOR,
        "default_icon": DEFAULT_ICON,
    }


# ── Промпт ──────────────────────────────────────────────────────────────────
# Общий каркас стиля — один на все обложки. Меняются только цвет и набор
# декоративных элементов, поэтому предметы выглядят одной коллекцией.
#
# Правки формулировок здесь меняют вид ВСЕХ будущих обложек: этот текст и есть
# дизайн-система. Прежние обложки при этом не трогаются — каждая живёт своей
# картинкой в хранилище, пока её не перегенерируют.
_BASE_STYLE = (
    "Create a premium illustrated course banner for a modern educational "
    "application. "
    "The artwork should feel like a professionally designed editorial graphic "
    "illustration — not an AI-generated diagram, dashboard background, "
    "infographic, SaaS interface or generic stock illustration. "
    "Create one cohesive abstract composition using a small number of carefully "
    "designed large and medium visual forms. The composition should feel "
    "intentional, elegant and slightly playful. "
    "Use flowing curves, large rounded geometric shapes, arcs, circles, organic "
    "forms, subtle lines and a few small decorative details. Allow shapes to "
    "overlap naturally and create visual depth through scale, layering, "
    "transparency and contrast. "
    "Do not arrange the elements into a grid. Do not create columns. Do not "
    "create repetitive patterns. Do not force symmetry. Do not divide the "
    "artwork into left and right sections. "
    "The composition should have a natural visual rhythm with a strong focal "
    "area, supporting decorative elements and generous breathing room. "
    "Colour: {color}. Use this colour family as the dominant colour of the entire "
    "artwork, built from several carefully chosen shades and tints of that same "
    "family. The palette should feel rich and sophisticated rather than washed "
    "out. Avoid pure black backgrounds and avoid overly pale backgrounds. Keep "
    "enough contrast for the artwork to stay visually interesting when displayed "
    "as a small course card. "
    "Subject: {motif}. Represent the subject through this abstract visual "
    "language rather than a literal illustration. "
    "The artwork should be flat and graphic, with subtle layering and "
    "transparency, but not 3D. Clean contemporary graphic design, soft rounded "
    "geometry, elegant curves, subtle depth, sophisticated composition. It should "
    "feel premium, modern, youthful and suitable for an educational technology "
    "product. "
    "Do not place any text inside the artwork. Do not draw the subject icon "
    "itself. No letters, no symbols, no logos, no UI elements, no buttons, no "
    "cards, no dashboards. No photographs, no people, no realistic objects, no "
    "photorealism, no 3D rendering. Not an infographic, not a diagram, no "
    "repetitive dot grids, no rows of rectangles, nothing that looks like a SaaS "
    "dashboard. "
    "The final result should look like a polished course cover illustration "
    "created by a professional graphic designer for a premium education "
    "application. Maintain the same overall visual language across every subject "
    "cover."
)

# Варианты композиции для Regenerate — не координаты, а художественное
# направление. Точные раскладки («левая треть», «чистая середина») ровно и
# превращали обложку в дашборд, поэтому модель строит композицию сама.
_COMPOSITIONS = (
    "Composition direction: large overlapping organic forms.",
    "Composition direction: flowing curves with layered circles.",
    "Composition direction: asymmetric geometric composition.",
    "Composition direction: large soft shapes with subtle linear elements.",
    "Composition direction: layered arcs and abstract structures.",
    "Composition direction: balanced geometric-organic composition.",
)


def build_prompt(color: str, icon: str, seed: int | None = None) -> str:
    """Единый промпт Chatra под выбранные цвет и предмет.

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
    r, g, b = colorsys.hls_to_rgb(h, max(0.0, min(1.0, l * light)), max(0.0, min(1.0, s * sat)))
    return round(r * 255), round(g * 255), round(b * 255)


# ── Локальный фон (фолбэк) ──────────────────────────────────────────────────
def render_background(color: str, seed: int | None = None):
    """Локальный фон в том же визуальном языке, что и AI-версия: насыщенный
    цвет одной семьи, несколько крупных перетекающих форм с наложением и
    прозрачностью, пара тонких дуг.

    Никаких сеток, точечных матриц и «чистой середины» — ровно они и делали
    обложку похожей на дашборд. Иконку клиенты кладут поверх готовой
    композиции, отдельного места под неё не резервируется.

    Используется, когда генерация недоступна (нет ключа, ошибка/таймаут
    OpenAI, исчерпан бюджет), и при создании класса — чтобы обложка была
    готова мгновенно и класс никогда не оставался без неё.
    """
    from PIL import Image, ImageDraw, ImageFilter

    color = normalize_color(color)
    base = _rgb(PALETTE[color]["base"])
    rng = random.Random(seed)
    side = COVER_HEIGHT

    # Основа — мягкий переход внутри одной цветовой семьи, по диагонали.
    deep = _shift(base, light=0.72, sat=1.05)
    lift = _shift(base, light=1.16, sat=0.94)
    flip = rng.random() < 0.5
    small = Image.new("RGB", (64, 64))
    px = small.load()
    for yy in range(64):
        for xx in range(64):
            u = (xx / 63) if flip else (1 - xx / 63)
            t = u * 0.62 + (yy / 63) * 0.38
            px[xx, yy] = tuple(round(deep[i] + (lift[i] - deep[i]) * t) for i in range(3))
    img = small.resize((COVER_WIDTH, COVER_HEIGHT), Image.BICUBIC).convert("RGBA")

    # Оттенки той же семьи для форм: светлее, темнее и почти белый акцент.
    tints = [
        _shift(base, light=1.34, sat=0.86),
        _shift(base, light=0.66, sat=1.10),
        _shift(base, light=1.55, sat=0.55),
    ]

    shapes = Image.new("RGBA", (COVER_WIDTH, COVER_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(shapes)

    def blob(cx: float, cy: float, r: float, fill):
        draw.ellipse((cx * COVER_WIDTH - r, cy * COVER_HEIGHT - r,
                      cx * COVER_WIDTH + r, cy * COVER_HEIGHT + r), fill=fill)

    # Три-четыре крупные формы, свободно расставленные и намеренно
    # перекрывающиеся — глубина берётся из наложения и прозрачности.
    for i in range(rng.randint(3, 4)):
        r = rng.uniform(0.30, 0.62) * side
        cx = rng.uniform(-0.05, 1.05)
        cy = rng.uniform(-0.10, 1.10)
        blob(cx, cy, r, (*tints[i % len(tints)], rng.randint(46, 84)))

    # Пара тонких дуг — «subtle lines» из промпта, без всякой регулярности.
    hairline = max(3, round(side * 0.005))
    for _ in range(rng.randint(1, 2)):
        r = rng.uniform(0.45, 0.85) * side
        cx = rng.uniform(0.0, 1.0) * COVER_WIDTH
        cy = rng.uniform(0.0, 1.0) * COVER_HEIGHT
        start = rng.uniform(0, 360)
        draw.arc((cx - r, cy - r, cx + r, cy + r), start, start + rng.uniform(60, 150),
                 fill=(*tints[2], 110), width=hairline)

    # Размытие — только чтобы снять лесенку ImageDraw (он не сглаживает).
    shapes = shapes.filter(ImageFilter.GaussianBlur(side * 0.003))
    return Image.alpha_composite(img, shapes).convert("RGB")


def render_fallback_cover(color: str, icon: str = "", seed: int | None = None):
    """Полноценный фон обложки без единого обращения к внешним сервисам.

    icon в рендер не входит и принимается только для симметрии вызова:
    предметную иконку рисуют клиенты поверх этой картинки (см. докстринг
    модуля), поэтому фолбэк и AI-версия проходят один и тот же путь.
    """
    return render_background(color, seed)


def encode_png(img) -> bytes:
    """PNG-байты. Дальше картинка всё равно пересжимается в WebP + миниатюру
    существующим services/image_processing.process_cover_image."""
    import io

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=False)
    return buf.getvalue()
