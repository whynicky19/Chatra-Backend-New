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

Отсюда же требование к самому фону: ЦЕНТР кадра должен оставаться пустым полем
цвета — туда ляжет иконка. Это записано и в промпт, и в локальный рендер.

Визуальный ориентир — графический баннер курса, а не «AI-картинка про
математику»: один доминирующий цвет, 2-4 простых абстрактных элемента из
оттенков того же цвета, мягкая асимметрия, много воздуха.
"""
import colorsys
import logging
import math
import random

logger = logging.getLogger(__name__)

# Квадрат: обложка кропается под очень разные пропорции (широкая карточка
# каталога, шапка класса, мелкая плашка в диалоге вступления). Квадратный
# исходник переживает любой из этих кропов, а композиция промпта держит
# элементы у краёв и центр пустым, поэтому кроп ничего важного не срезает.
COVER_WIDTH = 1024
COVER_HEIGHT = 1024

# Радиус центральной зоны (доля от стороны), которую фон обязан оставить
# пустым под иконку. Локальный рендер в неё не рисует, промпт её требует.
_CLEAR_CENTRE_RADIUS = 0.22


# ── Палитра ─────────────────────────────────────────────────────────────────
# hex   — акцент бренда: цвет свотча в пикере и иконки в UI.
# base  — заливка фона: насыщенный средний тон того же оттенка. Именно он
#         определяет всю обложку. Раньше здесь были почти чёрные значения —
#         обложки выходили тёмными и «ночными» вместо графичных баннеров.
# prompt — как назвать цвет модели словами: hex-код в промпте image-модели
#         работает плохо, название цвета — надёжно.
PALETTE: dict[str, dict[str, str]] = {
    "blue":   {"hex": "#0A84FF", "base": "#1C5FC4", "prompt": "azure blue"},
    "purple": {"hex": "#8B5CF6", "base": "#6D45CE", "prompt": "violet purple"},
    "green":  {"hex": "#22C55E", "base": "#1E9B54", "prompt": "emerald green"},
    "orange": {"hex": "#F97316", "base": "#D2600F", "prompt": "amber orange"},
    "red":    {"hex": "#EF4444", "base": "#C93A3A", "prompt": "coral red"},
    "pink":   {"hex": "#EC4899", "base": "#C43B80", "prompt": "magenta pink"},
    # Фирменный бирюзовый Chatra (C.teal в lib/theme/app_theme.dart).
    "teal":   {"hex": "#00B1C9", "base": "#0891A6", "prompt": "cyan teal"},
    "indigo": {"hex": "#6366F1", "base": "#4B4ECC", "prompt": "indigo"},
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
            {"id": slug, "hex": v["hex"], "base": v["base"]}
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
    "Flat minimal graphic banner artwork for an educational course card, part of "
    "one consistent design system. "
    "Background: a single solid {color} field, evenly lit, mid-tone and clearly "
    "coloured — never near-black, never dark, never washed out to white. "
    "On that field place only 2 to 4 simple abstract decorative elements, drawn "
    "in lighter and darker tints of the SAME colour, plus at most one small soft "
    "off-white accent. No other hues anywhere in the image. "
    "The elements are large, calm and geometric: soft rounded shapes, thin clean "
    "lines, gentle arcs, small dots, sparse regular grids. Crisp flat vector "
    "language, uniform line weight, no shading, no texture, no noise, no outlines "
    "around shapes. "
    "Composition: soft asymmetric balance with the elements grouped towards the "
    "edges and corners; at least half the frame stays an empty field of plain "
    "colour. The CENTRE of the frame must remain completely empty — plain "
    "background colour with absolutely nothing drawn in it. "
    "Decorative language for this subject: {motif}. This is a suggestion "
    "expressed purely as abstract geometry — never a literal illustration, never "
    "a diagram, never a recognisable object, and never a large central symbol. "
    "Strictly no text, no letters, no numbers, no words, no symbols that read as "
    "writing, no logos, no watermarks, no signatures, no icons, no UI elements. "
    "No photography, no people, no hands, no faces, no 3D rendering, no realistic "
    "objects, no scenes, no busy patterns, no clutter, no multiple competing "
    "colours."
)

# Вариации для Regenerate. Стиль, цвет и набор элементов остаются теми же —
# меняется только их раскладка. Каждая вариация обязана оставлять центр
# свободным: туда клиент кладёт иконку.
_COMPOSITIONS = (
    "Layout: elements gathered in the lower-left corner, the rest of the frame empty.",
    "Layout: elements along the top edge, a wide empty field below them.",
    "Layout: elements sweeping down the right-hand edge, the left side left empty.",
    "Layout: elements clustered in the upper-left corner with one small element "
    "near the lower-right corner.",
    "Layout: a sparse band of elements across the lower third, wide empty field above.",
    "Layout: elements hugging the left edge, the right two thirds left empty.",
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


def _place_disc(rng, radius: float) -> tuple[float, float]:
    """Центр круга радиуса radius (в долях стороны) так, чтобы КРАЙ круга не
    заходил в центральную зону под иконку.

    Считать нужно именно по краю: проверка одного лишь центра фигуры пускала
    в кадр крупные пятна, которые сами лежали за границей зоны, а перекрывали
    её с запасом — иконка ложилась на пятно.
    """
    need = _CLEAR_CENTRE_RADIUS + radius + _BLUR_MARGIN
    for _ in range(64):
        x, y = rng.uniform(-0.15, 1.15), rng.uniform(-0.15, 1.15)
        if math.hypot(x - 0.5, y - 0.5) > need:
            return x, y
    # Гарантированный угол: по диагонали до угла ~0.71 от центра, что заведомо
    # больше любого допустимого need при radius ≤ 0.34.
    return rng.choice(((-0.1, -0.1), (1.1, -0.1), (-0.1, 1.1), (1.1, 1.1)))


def _place_ring(rng, radius: float) -> tuple[float, float] | None:
    """Центр окружности радиуса radius так, чтобы сама ЛИНИЯ окружности не
    пересекала центральную зону: либо кольцо целиком далеко, либо зона целиком
    внутри кольца. Дуга — это тонкая линия, и «центр далеко» её не спасает."""
    clear = _CLEAR_CENTRE_RADIUS + _BLUR_MARGIN
    for _ in range(64):
        x, y = rng.uniform(-0.15, 1.15), rng.uniform(-0.15, 1.15)
        d = math.hypot(x - 0.5, y - 0.5)
        if d - radius > clear or radius - d > clear:
            return x, y
    return None


def render_background(color: str, seed: int | None = None):
    """Локальный фон в том же визуальном языке, что и AI-версия: плоская
    заливка выбранного цвета + несколько простых форм из оттенков того же
    цвета, с пустым центром под иконку.

    Используется, когда генерация недоступна (нет ключа, ошибка/таймаут
    OpenAI, исчерпан бюджет), и при создании класса — чтобы обложка была
    готова мгновенно и класс никогда не оставался без неё.
    """
    from PIL import Image, ImageDraw, ImageFilter

    color = normalize_color(color)
    base = _rgb(PALETTE[color]["base"])
    rng = random.Random(seed)

    # Очень мягкий вертикальный переход внутри одного тона — плоско, но не
    # мертво. Считается на маленьком холсте и растягивается: попиксельный
    # проход по полному размеру занял бы секунды.
    top = _shift(base, light=1.10, sat=0.98)
    bottom = _shift(base, light=0.92)
    small = Image.new("RGB", (2, 64))
    px = small.load()
    for y in range(64):
        t = y / 63
        for x in range(2):
            px[x, y] = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    img = small.resize((COVER_WIDTH, COVER_HEIGHT), Image.BICUBIC).convert("RGBA")

    light = _shift(base, light=1.34, sat=0.86)
    dark = _shift(base, light=0.78)
    side = COVER_HEIGHT

    shapes = Image.new("RGBA", (COVER_WIDTH, COVER_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(shapes)

    def disc(cx, cy, r_norm, fill):
        r = r_norm * side
        draw.ellipse((cx * COVER_WIDTH - r, cy * COVER_HEIGHT - r,
                      cx * COVER_WIDTH + r, cy * COVER_HEIGHT + r), fill=fill)

    # 2-3 крупные мягкие формы по краям — те же «soft rounded shapes», что
    # просит промпт. Центр не трогаем: там будет иконка.
    for _ in range(rng.randint(2, 3)):
        r_norm = rng.uniform(0.20, 0.34)
        cx, cy = _place_disc(rng, r_norm)
        tint = light if rng.random() < 0.65 else dark
        disc(cx, cy, r_norm, (*tint, rng.randint(46, 74)))

    # Пара тонких дуг и горсть точек — «thin clean lines, gentle arcs, dots».
    stroke = max(2, round(side * 0.006))
    for _ in range(2):
        r_norm = rng.uniform(0.30, 0.52)
        placed = _place_ring(rng, r_norm)
        if placed is None:
            continue
        cx, cy = placed
        r = r_norm * side
        start = rng.uniform(0, 360)
        draw.arc((cx * COVER_WIDTH - r, cy * COVER_HEIGHT - r,
                  cx * COVER_WIDTH + r, cy * COVER_HEIGHT + r),
                 start, start + rng.uniform(55, 120),
                 fill=(*light, 120), width=stroke)
    for _ in range(rng.randint(3, 6)):
        r_norm = rng.uniform(0.008, 0.018)
        cx, cy = _place_disc(rng, r_norm)
        disc(cx, cy, r_norm, (*light, 150))

    # Лёгкое размытие снимает лесенку ImageDraw (он не сглаживает), но
    # оставляет формы читаемыми — это плоская графика, а не туман.
    shapes = shapes.filter(ImageFilter.GaussianBlur(side * 0.004))
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
