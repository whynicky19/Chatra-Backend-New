"""Единая визуальная система обложек классов Chatra: палитра, промпт и
локальный рендер фона.

Обложка — это ДВА независимых слоя, и разделение принципиально:

  1. фон — тёмный premium-баннер 16:9 (генерирует OpenAI, см. services/
     cover_generator.py; при любом сбое — локальный render_background);
  2. главный символ предмета — НЕ часть картинки. Его рисуют клиенты поверх
     фона нативным компонентом (components/classes/SubjectCover.vue на вебе,
     widgets/subject_cover.dart в приложении).

Модель отвечает только за фон и атмосферу. Символы вроде Σ, спирали ДНК или
колбы генеративные модели воспроизводят нестабильно (лишние штрихи, зеркальные
буквы, псевдотекст), поэтому символ живёт в UI: там он всегда одного размера,
одной толщины линии и одного стиля во всех предметах, его можно перекрасить или
заменить без перегенерации всех обложек.

Визуальный ориентир — тёмный premium / Apple-like: глубокий выбранный цвет с
мягким градиентом, много свободного пространства, мягкое свечение и глубина.
Тематика предмета живёт в ФОНЕ: тонкие полупрозрачные линии, сетки, волны,
частицы и другая геометрия, встроенные в градиент и уходящие в него, а не набор
отдельных картинок. Главный акцент обложки — символ, который кладёт UI, поэтому
середина кадра остаётся спокойной: фон вторичен и создаёт атмосферу.

Что здесь уже пробовали и почему не вернулись:
  • яркая «редакторская» иллюстрация из крупных цветных форм — обложки
    получались нарядными, но каждая жила своей жизнью, коллекции не было;
  • светлая пастель и чистые полосы под иконку — выглядело как SaaS-дашборд;
  • композиция без спокойной середины — белый символ терялся в декоре.
Отсюда нынешний набор правил: темно, тонко, тематично, с пустой серединой.
"""
import colorsys
import logging
import random
import re

logger = logging.getLogger(__name__)

# Кадр 16:9 — широкая горизонтальная композиция. Обложку показывают полосой
# (карточка каталога ~2.1:1, шапка класса ~3.3:1), и чем шире исходник, тем
# меньше композиции теряется при кропе. Картинку от модели приводим к этому
# кадру в fit_cover_frame(): Images API даёт 3:2, лишнее срезается сверху и
# снизу — поэтому промпт и просит держать верхний и нижний край спокойными.
COVER_WIDTH = 1536
COVER_HEIGHT = 864

# Символ ложится в центр композиции. Клиенты рисуют его белым с мягкой тенью —
# на тёмном фоне это самый читаемый вариант, а спокойная середина (её просит
# промпт и оставляет локальный рендер) гарантирует, что он не потеряется.
ICON_ON_ARTWORK = "white-with-shadow"

# Название предмета уезжает в промпт как подсказка темы, поэтому его нужно
# обеззаразить: это пользовательский текст, и в нём могут оказаться кавычки,
# переводы строк или прямые указания модели. Оставляем только буквы, цифры и
# безобидную пунктуацию — «инструкции» без служебных символов модель читает
# как часть названия курса, а не как команду.
_SUBJECT_ALLOWED = re.compile(r"[^\w \-&+/.,:()]", re.UNICODE)
SUBJECT_MAX_LEN = 60


# ── Палитра ─────────────────────────────────────────────────────────────────
# hex   — акцент бренда: свотч в пикере и подсветка выбора.
# base  — опорный тон цвета. Клиенты рисуют им градиент предмета без картинки,
#         а локальный рендер берёт его за основу и уводит в темноту сам
#         (см. render_background): в палитре держим выразительный средний тон,
#         чтобы свотчи в пикере не превратились в восемь чёрных квадратов.
# ink   — цвет символа, когда он рисуется В ТОН. Сейчас клиенты рисуют его
#         белым (см. ICON_ON_ARTWORK), но значение оставлено: пикер подсвечивает
#         им выбранный символ.
# prompt — как описать цвет модели словами. Не один тон, а ГЛУБИНА: тёмная
#         основа плюс более светлые оттенки той же семьи для свечения. Слова
#         deep/dark здесь намеренно: обложка теперь тёмная premium, и без них
#         модель уводит фон в яркую заливку. Hex-коды image-модель понимает
#         плохо, названия — надёжно.
PALETTE: dict[str, dict[str, str]] = {
    "blue": {
        "hex": "#0A84FF", "base": "#3B82F6", "ink": "#1D4ED8",
        "prompt": "deep midnight blue — near-black navy in the corners, rich cobalt "
                  "in the field, with soft luminous sky-blue and pale azure light",
    },
    "purple": {
        "hex": "#8B5CF6", "base": "#7C5CE6", "ink": "#6D28D9",
        "prompt": "deep midnight violet — near-black plum in the corners, rich purple "
                  "in the field, with soft luminous lavender and pale lilac light",
    },
    "green": {
        "hex": "#22C55E", "base": "#12A970", "ink": "#047857",
        "prompt": "deep forest emerald — near-black pine in the corners, rich emerald "
                  "in the field, with soft luminous mint and pale seafoam light",
    },
    "orange": {
        "hex": "#F97316", "base": "#F4842B", "ink": "#C2410C",
        "prompt": "deep ember orange — near-black burnt umber in the corners, rich amber "
                  "in the field, with soft luminous apricot and pale golden light",
    },
    "red": {
        "hex": "#EF4444", "base": "#E4534F", "ink": "#B91C1C",
        "prompt": "deep crimson — near-black maroon in the corners, rich warm red "
                  "in the field, with soft luminous coral and pale rose light",
    },
    "pink": {
        "hex": "#EC4899", "base": "#E8559C", "ink": "#BE185D",
        "prompt": "deep magenta — near-black wine in the corners, rich fuchsia "
                  "in the field, with soft luminous pink and pale petal light",
    },
    # Фирменный бирюзовый Chatra (C.teal в lib/theme/app_theme.dart).
    "teal": {
        "hex": "#00B1C9", "base": "#12A2B5", "ink": "#0E7490",
        "prompt": "deep ocean teal — near-black petrol in the corners, rich teal "
                  "in the field, with soft luminous turquoise and pale aqua light",
    },
    "indigo": {
        "hex": "#6366F1", "base": "#5A5FE0", "ink": "#4338CA",
        "prompt": "deep indigo — near-black ink blue in the corners, rich indigo "
                  "in the field, with soft luminous periwinkle and pale cornflower light",
    },
}

DEFAULT_COLOR = "teal"

# subject — предмет, для которого символ предлагается по умолчанию. Клиенты
#           показывают подсказку рядом с символом, а промпт берёт это значение
#           темой, если название класса ничего не даёт («11А», «Поток 2»).
# motif   — ПРИМЕР тематических элементов фона для этого символа. Именно
#           пример: тему модель выводит из названия предмета и подбирает формы
#           сама, а мотив задаёт планку конкретности («UI-каркасы, сетки»,
#           а не «что-нибудь про дизайн») и держит коллекцию в одном языке.
ICONS: dict[str, dict[str, str]] = {
    "sigma": {
        "subject": "Mathematics",
        "motif": "coordinate grids, plotted curves, geometric constructions, "
                 "circles with radii, faint graph axes",
    },
    "atom": {
        "subject": "Physics",
        "motif": "wave interference, field lines, particle trails, orbital paths, "
                 "faint scientific diagrams",
    },
    "flask": {
        "subject": "Chemistry",
        "motif": "molecular lattices, bonded hexagonal structures, laboratory "
                 "glassware outlines, dissolving particles",
    },
    "dna": {
        "subject": "Biology",
        "motif": "DNA helices, cell membranes, branching organic structures, "
                 "microscopic patterns",
    },
    "code": {
        "subject": "Computer Science",
        "motif": "wireframe layouts, node graphs, layered interface frames, "
                 "vector paths, data flow lines",
    },
    "column": {
        "subject": "History",
        "motif": "old map contours, classical architecture outlines, arches and "
                 "columns, artifact silhouettes, faint ornamental patterns",
    },
    "globe": {
        "subject": "Geography",
        "motif": "meridian grids, terrain contour lines, coastline outlines, "
                 "topographic patterns",
    },
    "letter": {
        "subject": "English",
        "motif": "flowing script-like strokes, ruled writing lines, abstract "
                 "phonetic marks, ribbons of language",
    },
    "book": {
        "subject": "Literature",
        "motif": "layered pages, open-book outlines, ruled text blocks reduced to "
                 "faint lines, quill strokes",
    },
    "chart": {
        "subject": "Economics",
        "motif": "trend lines, candlestick and bar silhouettes, plotted axes, "
                 "flowing market curves",
    },
    "palette": {
        "subject": "Art",
        "motif": "brush strokes, colour-wheel arcs, composition guides, golden-ratio "
                 "spirals, canvas grids",
    },
    "note": {
        "subject": "Music",
        "motif": "staff lines, sound waves, rhythmic pulses, equaliser bars reduced "
                 "to faint geometry",
    },
}

DEFAULT_ICON = "book"


def normalize_color(value: str | None) -> str:
    """Слаг цвета из палитры; неизвестное/пустое → DEFAULT_COLOR."""
    key = (value or "").strip().lower()
    return key if key in PALETTE else DEFAULT_COLOR


def normalize_icon(value: str | None) -> str:
    """Слаг символа из набора; неизвестное/пустое → DEFAULT_ICON."""
    key = (value or "").strip().lower()
    return key if key in ICONS else DEFAULT_ICON


def normalize_subject(value: str | None, icon: str = "") -> str:
    """Тема для промпта из названия класса.

    Название пишет преподаватель, а уезжает оно во внешнюю модель — поэтому
    вычищаем служебные символы (кавычки, переводы строк, скобки-фигурные) и
    режем длину: остаётся название курса, а не площадка для инструкций модели.
    Пусто или ничего не осталось → предмет по умолчанию для выбранного символа
    («11А» темы не несёт, а Mathematics — несёт).
    """
    cleaned = _SUBJECT_ALLOWED.sub(" ", value or "")
    cleaned = " ".join(cleaned.split())[:SUBJECT_MAX_LEN].strip(" -&+/.,:()")
    if not cleaned:
        return ICONS[normalize_icon(icon)]["subject"]
    return cleaned


def catalog() -> dict:
    """Палитра и набор символов для клиентов — веб и приложение строят пикеры
    из этого ответа, чтобы набор цветов/символов нигде не разъезжался."""
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
# Общий каркас стиля — один на все обложки. Меняются только цвет и тематика
# фона, поэтому предметы выглядят одной коллекцией.
#
# Правки формулировок здесь меняют вид ВСЕХ будущих обложек: этот текст и есть
# дизайн-система. Прежние обложки при этом не трогаются — каждая живёт своей
# картинкой в хранилище, пока её не перегенерируют.
_BASE_STYLE = (
    "Create a premium dark abstract cover background for a modern educational "
    "application. The visual language is high-end and Apple-like: calm, "
    "minimal, confident, with a lot of empty space. "
    "Wide cinematic 16:9 horizontal composition. "

    "Colour and light: {color}. Fill the entire frame with one smooth soft "
    "gradient built from this single colour family — darkest in the corners, "
    "slightly brighter and luminous towards the middle. Add gentle depth and a "
    "soft ambient glow, as if light came from behind the artwork. The glow must "
    "stay soft and diffused: no harsh highlights, no strong neon, no glare, no "
    "second unrelated colour. "

    "Subject of the course: \"{subject}\". Treat this only as the topic of the "
    "background — never render it, or any other word, as text. Across the "
    "outer part of the frame, place abstract elements "
    "that belong to this specific subject, for example: {motif}. Work out the "
    "elements yourself from the meaning of the subject, so the cover is "
    "instantly recognisable as this field of knowledge, and so that two "
    "different subjects never get the same background. "

    "How those elements must look: thin precise lines, faint outlines, "
    "minimal geometry, sparse particles, subtle grids, curves and waves. Very "
    "low contrast, semi-transparent, softly glowing and dissolving into the "
    "gradient — they are part of the background, not objects placed on top of "
    "it. Never a collage of separate pictures, never opaque solid shapes, never "
    "a row of icons, never a busy or crowded frame. Use only a few of them and "
    "leave large areas of the gradient completely empty. "

    "The centre of the frame stays calm: only the gradient and the soft glow "
    "there. A single large subject symbol is placed over the centre by the "
    "interface and it is the main visual accent of the cover — nothing in the "
    "background may compete with it, so keep the thematic elements away from "
    "the middle and let them fade out towards it. Keep the top and bottom edges "
    "quiet as well, the artwork is cropped there. "

    "Every cover in this collection shares one style: the same darkness, the "
    "same softness, the same thin-line treatment and the same amount of empty "
    "space. Only the colour and the thematic elements change. "

    "Do not draw: any text, letters, numbers, subject names, labels, captions, "
    "logos or watermarks; the subject symbol itself; photographs, people, "
    "characters, mascots or random unrelated objects; realistic 3D renders, "
    "glossy or metallic materials, cartoon or hand-drawn illustration; bright "
    "neon, rainbow or multi-colour palettes; cluttered compositions; anything "
    "that looks like a screenshot of a real application interface. "

    "The final result should look like one cover from a professionally designed "
    "premium collection — dark, quiet, atmospheric and unmistakably part of the "
    "same design system as every other cover in the app."
)

# Варианты раскладки для Regenerate — куда сместить свечение и где собрать
# тематику. Середина остаётся спокойной в каждом варианте: это не «тоже
# направление», а условие читаемости символа.
_COMPOSITIONS = (
    "Layout: the glow sits slightly right of centre, the thematic elements "
    "gather along the left edge and the lower corner.",
    "Layout: the glow sits slightly left of centre, the thematic elements "
    "gather along the right edge and the upper corner.",
    "Layout: a wide low glow across the middle, the thematic elements drift in "
    "from both side edges and fade before reaching the centre.",
    "Layout: the glow is centred and tight, the thematic elements form a very "
    "sparse ring far around it.",
    "Layout: the glow rises from the lower third, the thematic elements hang in "
    "the upper corners as faint distant structures.",
    "Layout: a diagonal band of light from the lower left to the upper right, "
    "the thematic elements scattered sparsely along its darker side.",
)


def build_prompt(color: str, icon: str, seed: int | None = None,
                 subject: str | None = None) -> str:
    """Единый промпт Chatra под выбранные цвет, символ и предмет.

    subject — название класса: модель по нему сама подбирает тематические
    элементы фона (преподаватель по-прежнему выбирает только цвет и символ).
    Пустое/бессодержательное название заменяется предметом по умолчанию для
    выбранного символа, см. normalize_subject.

    seed меняет только раскладку (одна фраза из _COMPOSITIONS) — Images API не
    принимает seed как параметр, поэтому вариативность для Regenerate вносим
    текстом промпта.
    """
    color = normalize_color(color)
    icon = normalize_icon(icon)
    rng = random.Random(seed)
    return " ".join((
        _BASE_STYLE.format(
            color=PALETTE[color]["prompt"],
            subject=normalize_subject(subject, icon),
            motif=ICONS[icon]["motif"],
        ),
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


# ── Кадр ────────────────────────────────────────────────────────────────────
def fit_cover_frame(img):
    """Приводит картинку к кадру обложки 16:9 (COVER_WIDTH×COVER_HEIGHT).

    Images API отдаёт 3:2 (16:9 в списке размеров нет), поэтому лишнее
    срезается симметрично сверху и снизу — промпт для этого и просит держать
    верхний и нижний край спокойными. Центр кадра, где лежит символ, кроп не
    трогает вообще.
    """
    from PIL import Image

    target = COVER_WIDTH / COVER_HEIGHT
    w, h = img.size
    if w / h > target:
        # Шире кадра — режем по бокам поровну.
        new_w = round(h * target)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    elif w / h < target:
        new_h = round(w / target)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    if img.size != (COVER_WIDTH, COVER_HEIGHT):
        img = img.resize((COVER_WIDTH, COVER_HEIGHT), Image.LANCZOS)
    return img


# ── Локальный фон (фолбэк) ──────────────────────────────────────────────────
def _radial_mask(cx: float, cy: float, radius: float, softness: float = 1.0):
    """Маска мягкого круглого свечения (L-канал) на весь кадр.

    Считается на маленькой сетке и растягивается: попиксельный проход по
    1536×864 занял бы секунды, а нам нужен именно плавный ореол без границ.
    """
    from PIL import Image

    grid = 96
    mask = Image.new("L", (grid, grid))
    px = mask.load()
    ratio = COVER_WIDTH / COVER_HEIGHT
    for yy in range(grid):
        for xx in range(grid):
            dx = (xx / (grid - 1) - cx) * ratio
            dy = yy / (grid - 1) - cy
            d = (dx * dx + dy * dy) ** 0.5 / radius
            # Косинусоподобное затухание: к краю ореола уходит в ноль без
            # видимого кольца.
            v = max(0.0, 1.0 - d) ** (2.0 * softness)
            px[xx, yy] = round(255 * v)
    return mask.resize((COVER_WIDTH, COVER_HEIGHT), Image.BICUBIC)


def render_background(color: str, seed: int | None = None):
    """Локальный фон в том же визуальном языке, что и AI-версия: тёмный
    premium-градиент выбранного цвета, мягкое свечение к середине, тонкие
    полупрозрачные линии, дуги и частицы по краям, спокойный центр.

    Тематики предмета здесь нет намеренно: её приносит модель. Фолбэк должен
    оставаться нейтральным членом той же коллекции — темнота, мягкость и
    пустая середина у него ровно те же, что у сгенерированной обложки.

    Используется, когда генерация недоступна (нет ключа, ошибка/таймаут
    OpenAI, исчерпан бюджет), и при создании класса — чтобы обложка была
    готова мгновенно и класс никогда не оставался без неё.
    """
    from PIL import Image, ImageDraw, ImageFilter

    color = normalize_color(color)
    base = _rgb(PALETTE[color]["base"])
    rng = random.Random(seed)

    # Глубина цвета: почти чёрный угол, насыщенное поле, светлый ореол и
    # почти белая линия — всё внутри одной семьи (HLS-сдвиг не уводит тон).
    night = _shift(base, light=0.17, sat=1.10)
    field = _shift(base, light=0.42, sat=1.05)
    glow = _shift(base, light=0.92, sat=0.80)
    line = _shift(base, light=1.30, sat=0.50)

    # Диагональный градиент night → field: считаем мелко и растягиваем.
    flip = rng.random() < 0.5
    small = Image.new("RGB", (64, 64))
    px = small.load()
    for yy in range(64):
        for xx in range(64):
            u = (xx / 63) if flip else (1 - xx / 63)
            t = u * 0.55 + (1 - yy / 63) * 0.45
            px[xx, yy] = tuple(round(night[i] + (field[i] - night[i]) * t) for i in range(3))
    img = small.resize((COVER_WIDTH, COVER_HEIGHT), Image.BICUBIC).convert("RGB")

    # Свечение у середины — главный источник глубины и то, на чём читается
    # белый символ. Смещаем его слабо: символ ложится по центру, и уводить
    # свет далеко от него нельзя.
    gx = 0.5 + rng.uniform(-0.10, 0.10)
    gy = 0.5 + rng.uniform(-0.08, 0.08)
    halo = Image.new("RGB", (COVER_WIDTH, COVER_HEIGHT), glow)
    img = Image.composite(halo, img, _radial_mask(gx, gy, rng.uniform(0.62, 0.78)).point(
        lambda v: round(v * 0.42)))

    # Виньетка — та самая premium-глубина: углы уходят в почти чёрный. Кладём
    # её ДО тонкой графики: наоборот она гасила линии до полной невидимости —
    # «тонкие и полупрозрачные» не значит «которых нет».
    vignette = _radial_mask(0.5, 0.5, 1.35, softness=0.75).point(lambda v: 255 - round(v * 0.80))
    dark = Image.new("RGB", (COVER_WIDTH, COVER_HEIGHT), _shift(base, light=0.08, sat=1.0))
    img = Image.composite(dark, img, vignette.point(lambda v: round(v * 0.55)))

    # Тонкая графика по краям: пара длинных дуг, несколько прямых и редкие
    # частицы. Всё полупрозрачное и подальше от центра — фон вторичен.
    shapes = Image.new("RGBA", (COVER_WIDTH, COVER_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(shapes)
    hairline = max(2, round(COVER_HEIGHT * 0.0026))

    def far_from_centre(x: float, y: float) -> bool:
        return abs(x - 0.5) > 0.23 or abs(y - 0.5) > 0.26

    for _ in range(rng.randint(2, 3)):
        r = rng.uniform(0.55, 1.15) * COVER_HEIGHT
        cx = rng.choice((rng.uniform(-0.25, 0.12), rng.uniform(0.88, 1.25))) * COVER_WIDTH
        cy = rng.uniform(-0.2, 1.2) * COVER_HEIGHT
        start = rng.uniform(0, 360)
        draw.arc((cx - r, cy - r, cx + r, cy + r), start, start + rng.uniform(50, 130),
                 fill=(*line, rng.randint(70, 110)), width=hairline)

    for _ in range(rng.randint(3, 5)):
        y = rng.uniform(0.04, 0.96)
        if not far_from_centre(0.5, y):
            continue
        x0 = rng.choice((rng.uniform(-0.12, 0.10), rng.uniform(0.55, 0.80)))
        draw.line((x0 * COVER_WIDTH, y * COVER_HEIGHT,
                   (x0 + rng.uniform(0.22, 0.45)) * COVER_WIDTH,
                   (y + rng.uniform(-0.05, 0.05)) * COVER_HEIGHT),
                  fill=(*line, rng.randint(45, 75)), width=hairline)

    for _ in range(rng.randint(16, 24)):
        x, y = rng.random(), rng.random()
        if not far_from_centre(x, y):
            continue
        r = rng.uniform(1.8, 4.5)
        draw.ellipse((x * COVER_WIDTH - r, y * COVER_HEIGHT - r,
                      x * COVER_WIDTH + r, y * COVER_HEIGHT + r),
                     fill=(*line, rng.randint(90, 160)))

    # Размытие — только чтобы снять лесенку ImageDraw (он не сглаживает).
    shapes = shapes.filter(ImageFilter.GaussianBlur(COVER_HEIGHT * 0.0016))
    return Image.alpha_composite(img.convert("RGBA"), shapes).convert("RGB")


def render_fallback_cover(color: str, icon: str = "", seed: int | None = None):
    """Полноценный фон обложки без единого обращения к внешним сервисам.

    icon в рендер не входит и принимается только для симметрии вызова: главный
    символ рисуют клиенты поверх этой картинки (см. докстринг модуля), поэтому
    фолбэк и AI-версия проходят один и тот же путь.
    """
    return render_background(color, seed)


def encode_png(img) -> bytes:
    """PNG-байты. Дальше картинка всё равно пересжимается в WebP + миниатюру
    существующим services/image_processing.process_cover_image."""
    import io

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=False)
    return buf.getvalue()
