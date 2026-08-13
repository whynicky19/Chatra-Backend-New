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

Отсюда же требование к самому фону: центральная ПОЛОСА кадра должна оставаться
чистой — туда ляжет иконка. Это записано и в промпт, и в локальный рендер.

Визуальный ориентир — светлый баннер курса, а не «AI-картинка про математику»:
пастельный фон выбранного цвета с мягким градиентом, аккуратная раскладка
простых плоских форм в ЛЕВОЙ и ПРАВОЙ третях, чистая середина и цветная иконка
в тон поверх неё.

Про раскладку по бокам, а не по углам: обложку показывают широкой полосой, и
по высоте её режут очень сильно (в шапке класса видно ~30%). Углы при этом
уезжают за кадр, а в кадре остаётся пустая середина — ровно на это и жаловались.
По ширине же кадр виден целиком всегда, поэтому декор живёт по бокам.
"""
import colorsys
import logging
import math
import random

logger = logging.getLogger(__name__)

# Ландшафт 3:2. Квадрат здесь уже был и провалился: обложку показывают широкой
# полосой (карточка каталога ~2.1:1, шапка класса ~3.3:1), поэтому от квадрата
# в кадр попадала только средняя полоса — 48% и 30% высоты соответственно.
# А это ровно та зона, которую композиция оставляет чистой под иконку: снаружи
# обложка выглядела пустой, а декор по углам обрезался краями кадра.
COVER_WIDTH = 1536
COVER_HEIGHT = 1024

# Чистая зона под иконку — вертикальная полоса ПО ЦЕНТРУ, а не круг у центра.
# Кроп по высоте может быть сколь угодно жёстким, но по горизонтали кадр всегда
# виден целиком, поэтому декор живёт в левой и правой третях (там он переживает
# любой кроп), а середина остаётся свободной.
_CLEAR_COLUMN_HALF_WIDTH = 0.17   # доля ширины в каждую сторону от центра


# ── Палитра ─────────────────────────────────────────────────────────────────
# hex   — акцент бренда: свотч в пикере и подсветка выбора.
# base  — заливка фона: СВЕТЛЫЙ пастельный тон. Обложка — воздушная карточка
#         курса, а не насыщенный плакат: сначала здесь были почти чёрные
#         значения (тёмно и «ночно»), потом насыщенные средние (плотно и
#         пусто). Пастель даёт лёгкость и место для тонкой текстуры.
# ink   — цвет предметной иконки, которую клиенты рисуют поверх фона. Насыщенный
#         тон того же оттенка: иконка должна быть В ЦВЕТ обложки, а не белой —
#         белая на светлой пастели просто пропадает.
# prompt — как назвать цвет модели словами: hex-код в промпте image-модели
#         работает плохо, название цвета — надёжно.
PALETTE: dict[str, dict[str, str]] = {
    "blue":   {"hex": "#0A84FF", "base": "#E3EDFF", "ink": "#1D4ED8", "prompt": "soft pastel blue"},
    "purple": {"hex": "#8B5CF6", "base": "#EDE9FE", "ink": "#7C3AED", "prompt": "soft pastel lavender purple"},
    "green":  {"hex": "#22C55E", "base": "#DEF7E9", "ink": "#047857", "prompt": "soft pastel mint green"},
    "orange": {"hex": "#F97316", "base": "#FFEBD9", "ink": "#C2410C", "prompt": "soft pastel peach orange"},
    "red":    {"hex": "#EF4444", "base": "#FEE4E2", "ink": "#B91C1C", "prompt": "soft pastel rose red"},
    "pink":   {"hex": "#EC4899", "base": "#FCE7F3", "ink": "#BE185D", "prompt": "soft pastel pink"},
    # Фирменный бирюзовый Chatra (C.teal в lib/theme/app_theme.dart).
    "teal":   {"hex": "#00B1C9", "base": "#D6F1F7", "ink": "#0E7490", "prompt": "soft pastel aqua teal"},
    "indigo": {"hex": "#6366F1", "base": "#E6E7FD", "ink": "#4F46E5", "prompt": "soft pastel indigo"},
}

DEFAULT_COLOR = "teal"

# subject — предмет, для которого иконка предлагается по умолчанию (клиенты
# показывают подсказку рядом с иконкой).
# motif — декоративный язык предмета: НЕ «нарисуй колбу», а перечень простых
# абстрактных элементов. Модель хорошо делает сетки, дуги, точки и линии и
# плохо — узнаваемые предметы, поэтому просим ровно первое.
ICONS: dict[str, dict[str, str]] = {
    "sigma": {
        "subject": "Mathematics",
        "motif": "a sparse square grid, a few long diagonal lines, two concentric "
                 "arcs and a small cluster of dots",
    },
    "atom": {
        "subject": "Physics",
        "motif": "wide elliptical orbit curves, a few small particle dots and one "
                 "smooth wave line",
    },
    "flask": {
        "subject": "Chemistry",
        "motif": "small circles joined by straight bond lines and one soft rounded "
                 "vessel-like shape",
    },
    "dna": {
        "subject": "Biology",
        "motif": "soft organic curves, overlapping cell-like circles and one gently "
                 "branching line",
    },
    "code": {
        "subject": "Computer Science",
        "motif": "a regular dot grid, a few connected nodes with straight link lines "
                 "and offset rectangular blocks",
    },
    "column": {
        "subject": "History",
        "motif": "evenly spaced vertical bars, one wide flat triangle and stepped "
                 "rectangular forms",
    },
    "globe": {
        "subject": "Geography",
        "motif": "concentric circles, curved meridian lines and soft contour bands",
    },
    "letter": {
        "subject": "English",
        "motif": "flowing ribbon curves, sparse horizontal baseline rules and small "
                 "rounded speech-bubble forms",
    },
    "book": {
        "subject": "Literature",
        "motif": "layered rounded rectangles like stacked pages, soft folded curves "
                 "and thin ruled lines",
    },
    "chart": {
        "subject": "Economics",
        "motif": "ascending rectangular bars, one rising straight line and small "
                 "marker dots",
    },
    "palette": {
        "subject": "Art",
        "motif": "overlapping soft circles of different sizes, one sweeping "
                 "brush-like curve and small dots",
    },
    "note": {
        "subject": "Music",
        "motif": "parallel horizontal staff lines, small round dots and two smooth "
                 "wave curves",
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
    "A wide flat illustrated banner for an educational course card, in the style "
    "of a clean modern classroom app theme. Part of one consistent design system. "
    "Background: a very light {color} field with a smooth gentle gradient across "
    "the width. Pale, soft and luminous throughout. Never saturated, never "
    "mid-tone, never dark, never near-black. "
    "On this field draw a tidy decorative arrangement of simple flat shapes, all "
    "of them in slightly deeper tints of the SAME pastel hue, all low contrast "
    "and calm. No other hues anywhere in the image. "
    "LAYOUT, this matters most: group the decoration into the LEFT THIRD and the "
    "RIGHT THIRD of the banner, spread over the full height of each side, "
    "balanced left against right. Keep the central vertical band of the banner — "
    "the middle third of its width, top to bottom — completely clean and empty: "
    "plain pale background, nothing drawn in it at all, because an icon is "
    "placed there afterwards. "
    "Every shape must sit FULLY INSIDE the frame with a margin: nothing may run "
    "off or be cut by the edges of the image. "
    "Motifs for this subject: {motif}. Draw them as neat flat vector shapes, "
    "several per side, at varied sizes, arranged like a considered pattern rather "
    "than scattered at random. They are decorative suggestions, never a literal "
    "illustration, never a diagram, never a large central symbol. "
    "Crisp flat vector language, uniform thin line weight, no shading, no grain, "
    "no noise, no drop shadows. "
    "Strictly no text, no letters, no numbers, no words, no symbols that read as "
    "writing, no logos, no watermarks, no signatures, no icons, no UI elements. "
    "No photography, no people, no hands, no faces, no 3D rendering, no realistic "
    "objects, no scenes, no clutter, no dark areas, no multiple competing colours."
)

# Вариации для Regenerate. Стиль, цвет и набор элементов остаются теми же —
# меняется только их раскладка. Каждая вариация обязана оставлять центр
# свободным: туда клиент кладёт иконку.
# Вариации для Regenerate. Все обязаны держать декор в боковых третях и
# оставлять центральную полосу чистой — меняется только расстановка внутри
# этих третей.
_COMPOSITIONS = (
    "Arrangement: larger shapes low on the left side, smaller ones high on the "
    "right side.",
    "Arrangement: a vertical stack of shapes on the left side, a looser diagonal "
    "run of shapes on the right side.",
    "Arrangement: shapes clustered at mid-height on both sides, thinning towards "
    "the top and bottom.",
    "Arrangement: larger shapes high on the left side, smaller ones low on the "
    "right side.",
    "Arrangement: an even rhythm of shapes down the left side, two or three "
    "bigger ones on the right side.",
    "Arrangement: shapes gathered near the outer edges of both sides, leaving "
    "room between them and the clean central band.",
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
# Запас на размытие (см. конец render_background): размытая кромка фигуры
# заезжает чуть дальше её геометрической границы.
_BLUR_MARGIN = 0.02


def _side_spots(rng, count: int) -> list[tuple[float, float]]:
    """Точки для кластеров декора — только в левой и правой третях, во всю
    высоту кадра, поровну с обеих сторон.

    Раньше декор раскладывался по УГЛАМ, и это было прямой причиной жалоб
    «фон пустой» и «элементы уходят по краям»: обложку показывают широкой
    полосой (в шапке класса видно всего 30% высоты), поэтому углы уезжали за
    кадр, а в кадре оставалась пустая середина. По горизонтали же кадр виден
    целиком всегда — значит и декор должен жить по бокам.
    """
    left = [(x, y) for x in (0.11, 0.24) for y in (0.16, 0.40, 0.64, 0.88)]
    right = [(1 - x, y) for x, y in left]
    rng.shuffle(left)
    rng.shuffle(right)

    spots: list[tuple[float, float]] = []
    # Чередуем стороны, чтобы композиция не перекосилась на одну.
    for i in range(count):
        pool = left if i % 2 == 0 else right
        if pool:
            spots.append(pool.pop())
    return spots


def _clear_column(x: float, half: float = _CLEAR_COLUMN_HALF_WIDTH) -> bool:
    """True — точка попадает в центральную полосу под иконку."""
    return abs(x - 0.5) < half


def render_background(color: str, seed: int | None = None):
    """Локальный фон в том же визуальном языке, что и AI-версия: светлый
    пастельный градиент + деликатная текстура (точечные сетки, штриховка,
    волны, дуги, мягкие пятна) в ЛЕВОЙ и ПРАВОЙ третях, с чистой центральной
    полосой под иконку.

    Используется, когда генерация недоступна (нет ключа, ошибка/таймаут
    OpenAI, исчерпан бюджет), и при создании класса — чтобы обложка была
    готова мгновенно и класс никогда не оставался без неё.
    """
    from PIL import Image, ImageDraw, ImageFilter

    color = normalize_color(color)
    base = _rgb(PALETTE[color]["base"])
    rng = random.Random(seed)
    side = COVER_HEIGHT

    # Переход в основном ПО ГОРИЗОНТАЛИ: по ширине кадр виден целиком всегда,
    # а по высоте его режут, поэтому вертикальный градиент в показе почти не
    # читается. Небольшая вертикальная составляющая оставлена для живости.
    deep = _shift(base, light=0.94, sat=1.06)
    pale = _shift(base, light=1.045, sat=0.55)
    flip = rng.random() < 0.5
    small = Image.new("RGB", (64, 64))
    px = small.load()
    for y in range(64):
        for x in range(64):
            u = (x / 63) if flip else (1 - x / 63)
            t = u * 0.78 + (y / 63) * 0.22
            px[x, y] = tuple(round(deep[i] + (pale[i] - deep[i]) * t) for i in range(3))
    img = small.resize((COVER_WIDTH, COVER_HEIGHT), Image.BICUBIC).convert("RGBA")

    # Текстура — тем же оттенком, но заметно глубже фона. Альфа маленькая:
    # декор должен читаться «со второго взгляда», а не спорить с иконкой.
    tint = _shift(base, light=0.80, sat=1.15)
    blob = _shift(base, light=0.93, sat=1.10)

    shapes = Image.new("RGBA", (COVER_WIDTH, COVER_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(shapes)
    hairline = max(2, round(side * 0.0035))

    def _dot_grid(cx, cy):
        """Мелкая точечная сетка — самый узнаваемый элемент референса."""
        step = side * 0.030
        n = rng.randint(4, 6)
        r = side * 0.0045
        for row in range(n):
            for col in range(n):
                x = (cx * COVER_WIDTH) + (col - (n - 1) / 2) * step
                y = (cy * COVER_HEIGHT) + (row - (n - 1) / 2) * step
                draw.ellipse((x - r, y - r, x + r, y + r), fill=(*tint, 96))

    def _hatch(cx, cy):
        """Тонкая параллельная штриховка под 45°."""
        n = rng.randint(5, 8)
        step = side * 0.026
        length = side * rng.uniform(0.13, 0.19)
        for i in range(n):
            off = (i - (n - 1) / 2) * step
            x = cx * COVER_WIDTH + off
            y = cy * COVER_HEIGHT - off
            draw.line((x, y, x + length * 0.72, y + length * 0.72),
                      fill=(*tint, 74), width=hairline)

    def _waves(cx, cy):
        """Мягкие волновые ленты."""
        for k in range(rng.randint(2, 3)):
            pts = []
            span = side * 0.42
            amp = side * rng.uniform(0.020, 0.034)
            phase = rng.uniform(0, math.tau)
            y0 = cy * COVER_HEIGHT + k * side * 0.032
            for i in range(33):
                t = i / 32
                pts.append((cx * COVER_WIDTH - span / 2 + span * t,
                            y0 + math.sin(phase + t * math.tau) * amp))
            draw.line(pts, fill=(*tint, 70), width=hairline, joint="curve")

    def _arcs(cx, cy):
        """Концентрические дуги."""
        for k in range(rng.randint(2, 4)):
            r = side * (0.12 + k * 0.055)
            start = rng.uniform(0, 360)
            draw.arc((cx * COVER_WIDTH - r, cy * COVER_HEIGHT - r,
                      cx * COVER_WIDTH + r, cy * COVER_HEIGHT + r),
                     start, start + rng.uniform(70, 150),
                     fill=(*tint, 80), width=hairline)

    # Крупные бледные пятна — по одному с каждой стороны, прижаты к внешним
    # краям, чтобы не наползать на центральную полосу.
    for side_x in (rng.uniform(0.02, 0.14), rng.uniform(0.86, 0.98)):
        r_norm = rng.uniform(0.24, 0.34)
        cy = rng.uniform(0.15, 0.85)
        r = r_norm * side
        draw.ellipse((side_x * COVER_WIDTH - r, cy * COVER_HEIGHT - r,
                      side_x * COVER_WIDTH + r, cy * COVER_HEIGHT + r),
                     fill=(*blob, 90))

    # Четыре-шесть кластеров текстуры по бокам — фон перестаёт быть пустым при
    # любом кропе по высоте, оставаясь тихим.
    makers = [_dot_grid, _hatch, _waves, _arcs]
    rng.shuffle(makers)
    spots = _side_spots(rng, rng.randint(4, 6))
    for i, (cx, cy) in enumerate(spots):
        if _clear_column(cx):
            continue
        makers[i % len(makers)](cx, cy)

    # Лёгкое размытие снимает лесенку ImageDraw (он не сглаживает), но
    # оставляет линии читаемыми — это плоская графика, а не туман.
    shapes = shapes.filter(ImageFilter.GaussianBlur(side * 0.0022))

    # Центральная полоса вычищается маской, а не аккуратной геометрией каждого
    # элемента: раньше приходилось доказывать про каждую дугу и сетку, что она
    # не заедет под иконку, и одна дуга с большим радиусом это условие
    # нарушала. Маска даёт инвариант по построению — что бы ни нарисовали выше.
    shapes.putalpha(Image.composite(
        Image.new("L", shapes.size, 0), shapes.getchannel("A"), _centre_mask(shapes.size),
    ))
    return Image.alpha_composite(img, shapes).convert("RGB")


_centre_mask_cache: dict[tuple[int, int], object] = {}


def _centre_mask(size: tuple[int, int]):
    """Маска центральной ПОЛОСЫ под иконку: белое внутри, чёрное снаружи, с
    мягким краем — жёсткая граница резала бы текстуру видимой линией.

    Полоса, а не круг: кроп по высоте бывает очень жёстким (в шапке класса
    видно ~30% высоты), поэтому чистой должна быть вся центральная колонка на
    любой высоте, а не только пятно вокруг геометрического центра.
    """
    from PIL import Image, ImageDraw, ImageFilter

    cached = _centre_mask_cache.get(size)
    if cached is not None:
        return cached

    w, h = size
    half = w * _CLEAR_COLUMN_HALF_WIDTH
    # Полоса рисуется ШИРЕ защищаемой зоны, а размытие уводит спад наружу: если
    # растушевать ровно по границе, полупрозрачный край маски приходится на
    # саму зону и текстура просачивается под иконку у её кромки.
    blur = half * 0.30
    outer = half + blur * 2.6

    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rectangle((w / 2 - outer, -1, w / 2 + outer, h + 1), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(blur))
    _centre_mask_cache[size] = mask
    return mask


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
