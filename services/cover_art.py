"""Единая визуальная система обложек классов Chatra: палитра, промпт и
локальный рендер фона.

Обложка — это ДВА независимых слоя, и разделение принципиально:

  1. фон — premium-баннер 16:9 с уникальной тематической сценой (генерирует
     OpenAI, см. services/cover_generator.py; при любом сбое — локальный
     render_background);
  2. главный символ предмета — НЕ часть картинки. Его рисуют клиенты поверх
     фона нативным компонентом (components/classes/SubjectCover.vue на вебе,
     widgets/subject_cover.dart в приложении).

Модель отвечает только за фон и атмосферу. Символы вроде Σ, спирали ДНК или
колбы генеративные модели воспроизводят нестабильно (лишние штрихи, зеркальные
буквы, псевдотекст), поэтому символ живёт в UI: там он всегда одного размера,
одной толщины линии и одного стиля во всех предметах, его можно перекрасить или
заменить без перегенерации всех обложек.

Визуальный ориентир — premium / Apple-like: насыщенный выбранный цвет с мягким
градиентом, контролируемая глубина, тематическая сцена в духе предмета.
Цвет обязан быть виден в каждой точке кадра; средняя яркость — примерно
четверть шкалы, без почти-чёрных углов и без «прожектора» в центре.

Главный сдвиг текущей версии — ОТ «у всех одинаковая тарелка + иконка» К
«у каждого предмета своя визуальная сцена». Раньше промпт в обязательном
порядке просил модель нарисовать «softly glowing stage» в центре под иконку:
выходила одна и та же светящаяся платформа на всех обложках, а тематика
предмета сводилась к двум-трём полупрозрачным линиям у краёв. Теперь:

  • иконка НЕ привязана к платформе — она может плавать, быть частью
    технической структуры или стоять в центре абстрактной композиции;
  • название предмета определяет СЦЕНУ, а не «несколько деталей у краёв»;
  • тематических элементов 3–6, они видимы, но не конкурируют с иконкой;
  • все обложки остаются в одной дизайн-системе: один градиент, одно
    освещение, одна толщина линий, один уровень деталировки.

Что здесь уже пробовали и почему не вернулись:
  • «softly glowing stage» под иконку у каждого предмета — одинаковая
    тарелка на всех обложках, тематика угадывалась только по цвету;
  • «two or three faint motifs near the edges» — тематика была почти
    невидимой, дизайн-система доминировала над предметом;
  • «no more than three motifs, must never compete with the centre» —
    мотивы гасились до полной прозрачности.
Отсюда нынешний набор правил: у каждого предмета СВОЯ сцена, в одном
визуальном языке коллекции.
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
# prompt — как описать цвет модели словами. Не один тон, а ГЛУБИНА: насыщенная
#         основа плюс более светлые оттенки той же семьи для свечения. Hex-коды
#         image-модель понимает плохо, названия — надёжно.
#         Формулировки специально держатся В СРЕДНЕЙ части шкалы. Здесь стояло
#         «near-black … in the corners» и «only a faint halo» — модель читала
#         это буквально и отдавала почти чёрный кадр, в котором выбранный цвет
#         угадывался разве что по краю свечения. Теперь угол — это тёмный, но
#         явно цветной тон, поле — светящийся насыщенный цвет, а ореол назван
#         отчётливым, а не еле заметным.
PALETTE: dict[str, dict[str, str]] = {
    "blue": {
        "hex": "#0A84FF", "base": "#3B82F6", "ink": "#1D4ED8",
        "prompt": "deep cobalt blue — dark but clearly blue navy in the corners, "
                  "luminous cobalt filling the field, lit by a distinct sky-blue halo "
                  "and highlights of pale azure",
    },
    "purple": {
        "hex": "#8B5CF6", "base": "#7C5CE6", "ink": "#6D28D9",
        "prompt": "deep violet — dark but clearly purple plum in the corners, "
                  "luminous violet filling the field, lit by a distinct lavender halo "
                  "and highlights of pale lilac",
    },
    "green": {
        "hex": "#22C55E", "base": "#12A970", "ink": "#047857",
        "prompt": "deep emerald green — dark but clearly green pine in the corners, "
                  "luminous emerald filling the field, lit by a distinct mint halo "
                  "and highlights of pale seafoam",
    },
    "orange": {
        "hex": "#F97316", "base": "#F4842B", "ink": "#C2410C",
        "prompt": "deep ember orange — dark but clearly orange burnt umber in the "
                  "corners, luminous amber filling the field, lit by a distinct apricot "
                  "halo and highlights of pale golden",
    },
    "red": {
        "hex": "#EF4444", "base": "#E4534F", "ink": "#B91C1C",
        "prompt": "deep crimson — dark but clearly red maroon in the corners, "
                  "luminous warm red filling the field, lit by a distinct coral halo "
                  "and highlights of pale rose",
    },
    "pink": {
        "hex": "#EC4899", "base": "#E8559C", "ink": "#BE185D",
        "prompt": "deep magenta — dark but clearly pink wine in the corners, "
                  "luminous fuchsia filling the field, lit by a distinct pink halo "
                  "and highlights of pale petal",
    },
    # Фирменный бирюзовый Chatra (C.teal в lib/theme/app_theme.dart).
    "teal": {
        "hex": "#00B1C9", "base": "#12A2B5", "ink": "#0E7490",
        "prompt": "deep ocean teal — dark but clearly teal petrol in the corners, "
                  "luminous teal filling the field, lit by a distinct turquoise halo "
                  "and highlights of pale aqua",
    },
    "indigo": {
        "hex": "#6366F1", "base": "#5A5FE0", "ink": "#4338CA",
        "prompt": "deep indigo — dark but clearly indigo ink blue in the corners, "
                  "luminous indigo filling the field, lit by a distinct periwinkle halo "
                  "and highlights of pale cornflower",
    },
    # Дальше — расширение набора: в вузе и школе направлений много, восьми
    # цветов на каталог не хватало. Цвета клиентам ничего не стоят (это
    # чистые данные из /cover/options), в отличие от символов, каждому из
    # которых нужен глиф в обоих клиентах.
    "gold": {
        "hex": "#EAB308", "base": "#B38C22", "ink": "#854D0E",
        "prompt": "deep antique gold — dark but clearly golden bistre in the corners, "
                  "luminous ochre filling the field, lit by a distinct amber halo "
                  "and highlights of pale straw",
    },
    "lime": {
        "hex": "#84CC16", "base": "#6FA81B", "ink": "#3F6212",
        "prompt": "deep olive lime — dark but clearly olive moss in the corners, "
                  "luminous olive green filling the field, lit by a distinct lime halo "
                  "and highlights of pale chartreuse",
    },
    "bronze": {
        "hex": "#C2763A", "base": "#B4703C", "ink": "#7C2D12",
        "prompt": "deep bronze — dark but clearly bronze sepia in the corners, "
                  "luminous warm bronze filling the field, lit by a distinct copper halo "
                  "and highlights of pale sand",
    },
    # Нейтральный вариант коллекции: не «серый по недосмотру», а осознанный
    # графитовый — для направлений, которым цветной акцент не идёт.
    "slate": {
        "hex": "#7C8BA5", "base": "#64748B", "ink": "#334155",
        "prompt": "deep graphite — dark but clearly grey charcoal in the corners, "
                  "luminous cool slate grey filling the field, lit by a distinct "
                  "steel-blue halo and highlights of pale silver",
    },
}

# Цвета без выраженного тона: их не проверяют на насыщенность (см. тесты) —
# графит в палитре ровно один и намеренно.
NEUTRAL_COLORS = frozenset({"slate"})

DEFAULT_COLOR = "teal"

# Группы символов для пикера. Набор перевалил за четыре десятка — плоским
# списком его листать невозможно, поэтому клиенты рисуют его секциями в этом
# порядке. Ключ уезжает в каждый символ как "group", подпись — как
# "group_label" (английская, как и subject: локализуют клиенты).
ICON_GROUPS: tuple[tuple[str, str], ...] = (
    ("exact", "Exact sciences"),
    ("natural", "Natural sciences"),
    ("tech", "Technology & engineering"),
    ("humanities", "Humanities"),
    ("language", "Languages"),
    ("business", "Business & law"),
    ("arts", "Arts & media"),
    ("health", "Health & medicine"),
    ("applied", "Applied & vocational"),
    ("sport", "Sport"),
)

# subject — предмет, для которого символ предлагается по умолчанию. Клиенты
#           показывают подсказку рядом с символом, а промпт берёт это значение
#           темой, если название класса ничего не даёт («11А», «Поток 2»).
# group   — секция пикера, ключ из ICON_GROUPS.
# motif   — ПРИМЕР тематических элементов фона для этого символа. Именно
#           пример: тему модель выводит из названия предмета и подбирает формы
#           сама (SUBJECT_MOTIFS), а мотив символа работает запасным вариантом
#           и держит коллекцию в одном языке.
#
# ВАЖНО про добавление символов: сам глиф рисуют клиенты (SubjectCover.vue,
# subject_cover.dart), поэтому новый id здесь — это задача и им тоже. Идентифи-
# каторы подобраны под имена Lucide (sigma, atom, flask-conical, dna, code,
# app-window, database, network, shield, cpu, cog, compass, building-2, scroll,
# brain, users, coins, briefcase, scale, camera, pen-tool, mic, heart-pulse,
# stethoscope, pill, wrench, chef-hat, scissors, car, plane, sprout, volleyball),
# чтобы глиф на обоих клиентах брался готовым, а не рисовался с нуля.
ICONS: dict[str, dict[str, str]] = {
    # ── Точные науки ────────────────────────────────────────────────────────
    "sigma": {
        "subject": "Mathematics", "group": "exact",
        "motif": "coordinate grids, plotted curves, geometric constructions, "
                 "circles with radii, faint graph axes",
    },
    "cube": {
        "subject": "Geometry", "group": "exact",
        "motif": "wireframe polyhedra, projection lines, angles and compass arcs, "
                 "construction grids",
    },
    "dice": {
        "subject": "Statistics", "group": "exact",
        "motif": "distribution curves, scatter grids, probability trees, tally "
                 "geometry, plotted axes",
    },

    # ── Естественные науки ──────────────────────────────────────────────────
    "atom": {
        "subject": "Physics", "group": "natural",
        "motif": "wave interference, field lines, particle trails, orbital paths, "
                 "faint scientific diagrams",
    },
    "flask": {
        "subject": "Chemistry", "group": "natural",
        "motif": "molecular lattices, bonded hexagonal structures, laboratory "
                 "glassware outlines, dissolving particles",
    },
    "dna": {
        "subject": "Biology", "group": "natural",
        "motif": "DNA helices, cell membranes, branching organic structures, "
                 "microscopic patterns",
    },
    "microscope": {
        "subject": "Microbiology", "group": "natural",
        "motif": "cell colonies under a lens, petri-dish circles, slide grids, "
                 "microscopic filaments",
    },
    "leaf": {
        "subject": "Ecology", "group": "natural",
        "motif": "leaf venation, growth rings, branching root systems, water cycle "
                 "arrows reduced to thin curves",
    },
    "telescope": {
        "subject": "Astronomy", "group": "natural",
        "motif": "orbital ellipses, constellation lines, planetary arcs, star fields, "
                 "faint celestial charts",
    },
    "globe": {
        "subject": "Geography", "group": "natural",
        "motif": "meridian grids, terrain contour lines, coastline outlines, "
                 "topographic patterns",
    },

    # ── Технологии и инженерия ──────────────────────────────────────────────
    "code": {
        "subject": "Programming", "group": "tech",
        "motif": "node graphs, flowing data paths, layered code blocks reduced to "
                 "faint lines, terminal-like geometry",
    },
    "browser": {
        "subject": "Web Development", "group": "tech",
        "motif": "wireframe layouts, browser window frames, layout grids, vector "
                 "paths, interface components reduced to thin outlines",
    },
    "database": {
        "subject": "Databases", "group": "tech",
        "motif": "stacked table grids, relation arrows, indexed rows reduced to thin "
                 "lines, query trees",
    },
    "network": {
        "subject": "Networks", "group": "tech",
        "motif": "node topologies, routing paths, signal rings, packet trails",
    },
    "shield": {
        "subject": "Cybersecurity", "group": "tech",
        "motif": "key and lock geometry, cipher grids, layered perimeters, binary "
                 "streams reduced to faint lines",
    },
    "chip": {
        "subject": "Electronics", "group": "tech",
        "motif": "circuit traces, pin grids, resistor and capacitor symbols, signal "
                 "waveforms",
    },
    "gear": {
        "subject": "Mechanical Engineering", "group": "tech",
        "motif": "gear outlines, technical blueprints, dimension lines, orthographic "
                 "projections, kinematic linkages",
    },
    "compass": {
        "subject": "Architecture", "group": "tech",
        "motif": "floor plans, elevation drawings, construction grids, arcs struck "
                 "with a compass, dimension lines",
    },
    "building": {
        "subject": "Construction", "group": "tech",
        "motif": "structural frames, truss geometry, load diagrams, scaffolding "
                 "lattices, site plans",
    },

    # ── Гуманитарные ────────────────────────────────────────────────────────
    "column": {
        "subject": "History", "group": "humanities",
        "motif": "old map contours, classical architecture outlines, arches and "
                 "columns, artifact silhouettes, faint ornamental patterns",
    },
    "scroll": {
        "subject": "Philosophy", "group": "humanities",
        "motif": "concentric thought circles, balanced geometry, unrolled scroll "
                 "lines, classical proportions",
    },
    "book": {
        "subject": "Literature", "group": "humanities",
        "motif": "layered pages, open-book outlines, ruled text blocks reduced to "
                 "faint lines, quill strokes",
    },
    "brain": {
        "subject": "Psychology", "group": "humanities",
        "motif": "interconnected thought nodes, concentric mind circles, soft "
                 "branching links, mirrored silhouetted geometry",
    },
    "people": {
        "subject": "Social Studies", "group": "humanities",
        "motif": "connection graphs between figures reduced to dots, survey grids, "
                 "demographic curves, group circles",
    },

    # ── Языки ───────────────────────────────────────────────────────────────
    "letter": {
        "subject": "English", "group": "language",
        "motif": "flowing script-like strokes, ruled writing lines, abstract "
                 "phonetic marks, ribbons of language",
    },
    "chat": {
        "subject": "Communication", "group": "language",
        "motif": "speech-shaped outlines, sound waves, dialogue lines, phonetic "
                 "marks reduced to faint geometry",
    },

    # ── Экономика и право ───────────────────────────────────────────────────
    "chart": {
        "subject": "Economics", "group": "business",
        "motif": "trend lines, candlestick and bar silhouettes, plotted axes, "
                 "flowing market curves",
    },
    "coins": {
        "subject": "Finance", "group": "business",
        "motif": "stacked coin circles, ledger grids, compound curves, balance "
                 "geometry",
    },
    "briefcase": {
        "subject": "Management", "group": "business",
        "motif": "org-chart branches, process arrows, milestone timelines, matrix "
                 "grids reduced to thin lines",
    },
    "scale": {
        "subject": "Law", "group": "business",
        "motif": "balanced scale outlines, columned facades, paragraph marks reduced "
                 "to faint geometry, seal circles",
    },

    # ── Искусство и медиа ───────────────────────────────────────────────────
    "palette": {
        "subject": "Art", "group": "arts",
        "motif": "brush strokes, colour-wheel arcs, composition guides, golden-ratio "
                 "spirals, canvas grids",
    },
    "pen": {
        "subject": "Graphic Design", "group": "arts",
        "motif": "bezier paths with handles, layout grids, type baselines, layered "
                 "shape outlines",
    },
    "note": {
        "subject": "Music", "group": "arts",
        "motif": "staff lines, sound waves, rhythmic pulses, equaliser bars reduced "
                 "to faint geometry",
    },
    "camera": {
        "subject": "Photography", "group": "arts",
        "motif": "aperture blades, focus rings, rule-of-thirds grids, light-ray "
                 "diagrams, film-frame outlines",
    },
    "mic": {
        "subject": "Journalism", "group": "arts",
        "motif": "broadcast waves, column grids of a newspaper page, signal arcs, "
                 "headline rules reduced to thin lines",
    },

    # ── Медицина ────────────────────────────────────────────────────────────
    "pulse": {
        "subject": "Medicine", "group": "health",
        "motif": "pulse waveforms, anatomical outlines, cell structures, molecular "
                 "chains, faint medical diagrams",
    },
    "stethoscope": {
        "subject": "Nursing", "group": "health",
        "motif": "pulse curves, care-plan grids, anatomical outlines, soft rounded "
                 "medical geometry",
    },
    "pill": {
        "subject": "Pharmacy", "group": "health",
        "motif": "molecular chains, dosage curves, capsule outlines, crystalline "
                 "lattices",
    },

    # ── Прикладные и профессии ──────────────────────────────────────────────
    "wrench": {
        "subject": "Technology & Crafts", "group": "applied",
        "motif": "tool outlines, workshop blueprints, fastener geometry, assembly "
                 "diagrams",
    },
    "chef": {
        "subject": "Culinary Arts", "group": "applied",
        "motif": "utensil outlines, recipe grids, steam curves, plated-composition "
                 "circles",
    },
    "scissors": {
        "subject": "Fashion & Sewing", "group": "applied",
        "motif": "pattern-cutting lines, stitch dashes, fabric drape curves, "
                 "measurement grids",
    },
    "car": {
        "subject": "Automotive", "group": "applied",
        "motif": "engine schematics, gear trains, chassis blueprints, road-line "
                 "perspective reduced to thin geometry",
    },
    "plane": {
        "subject": "Aviation", "group": "applied",
        "motif": "airfoil profiles, flight-path arcs, airflow streamlines, "
                 "navigation grids",
    },
    "sprout": {
        "subject": "Agriculture", "group": "applied",
        "motif": "field grids, growth curves, root systems, irrigation lines, "
                 "seasonal cycle circles",
    },

    # ── Спорт ───────────────────────────────────────────────────────────────
    "ball": {
        "subject": "Physical Education", "group": "sport",
        "motif": "motion trails, field markings, trajectory arcs, stopwatch circles, "
                 "stadium geometry",
    },
}

DEFAULT_ICON = "book"

# Тематика фона по НАЗВАНИЮ предмета. Мотив символа для этого не годится:
# преподаватель ставит на «Physics» колбу (просто потому, что нравится), и фон
# уезжал в химические молекулы с лабораторной посудой. Название — источник
# правды о предмете, символ — только украшение, поэтому сначала ищем здесь.
#
# Порядок важен: срабатывает первое совпадение, поэтому узкие темы («веб-
# дизайн») стоят выше общих («дизайн»). Ключи в нижнем регистре, сравнение —
# по вхождению подстроки, чтобы «Математика 2 курс» и «Matematika» попадали
# в ту же строку.
SUBJECT_MOTIFS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("веб-дизайн", "веб дизайн", "web design", "web-design", "ui/ux", "ui ux",
      "юай", "вёрстк", "верстк", "frontend", "фронтенд", "html", "css"),
     "wireframe layouts, browser window frames, layout grids, vector paths, "
     "interface components reduced to thin outlines"),
    (("программир", "programming", "informatik", "информатик", "computer science",
      "coding", "кодинг", "python", "java", "algorithm", "алгоритм", "разработк",
      "software", "backend", "бэкенд", "devops", "базы данных", "database"),
     "node graphs, flowing data paths, layered code blocks reduced to faint "
     "lines, network topologies, terminal-like geometry"),
    (("матем", "matem", "math", "алгебр", "algebra", "геометр", "geometr",
      "матан", "calculus", "тригоном", "статист", "statist", "теория вероят"),
     "coordinate grids, plotted curves, geometric constructions, circles with "
     "radii, faint graph axes"),
    (("физик", "fizik", "physics", "механик", "mechanic", "оптик", "термодинам",
      "электродинам", "квант", "quantum"),
     "wave interference, field lines, particle trails, orbital paths, faint "
     "scientific diagrams"),
    (("астроном", "astronom", "космос", "space", "вселенн"),
     "orbital ellipses, constellation lines, planetary arcs, star fields, faint "
     "celestial charts"),
    (("хими", "himi", "chem", "органическая", "неорганическ"),
     "molecular lattices, bonded hexagonal structures, laboratory glassware "
     "outlines, dissolving particles"),
    (("биолог", "biolog", "анатом", "anatom", "ботаник", "зоолог", "эколог",
      "ecolog", "генетик", "genetic", "микробиолог"),
     "DNA helices, cell membranes, branching organic structures, microscopic "
     "patterns"),
    (("медиц", "medic", "здоров", "health", "сестринск", "nursing", "фармац",
      "pharma"),
     "anatomical outlines, pulse waveforms, cell structures, molecular chains, "
     "faint medical diagrams"),
    (("истори", "history", "археолог", "archaeolog", "культуролог"),
     "old map contours, classical architecture outlines, arches and columns, "
     "artifact silhouettes, faint ornamental patterns"),
    (("географ", "geograph", "картограф", "геолог", "geolog", "краевед"),
     "meridian grids, terrain contour lines, coastline outlines, topographic "
     "patterns"),
    (("литератур", "literature", "поэз", "poetry", "чтение"),
     "layered pages, open-book outlines, ruled text blocks reduced to faint "
     "lines, quill strokes"),
    (("англ", "english", "немецк", "german", "deutsch", "французск", "french",
      "испанск", "spanish", "китайск", "chinese", "казахск", "qazaq", "тілі",
      "язык", "language", "лингвист", "linguist", "грамматик", "grammar",
      "речь", "speaking"),
     "flowing script-like strokes, ruled writing lines, abstract phonetic "
     "marks, ribbons of language"),
    (("эконом", "econom", "финанс", "finance", "бизнес", "business", "бухгалт",
      "accounting", "маркетинг", "marketing", "менеджмент", "management",
      "предпринимат"),
     "trend lines, candlestick and bar silhouettes, plotted axes, flowing "
     "market curves"),
    (("прав", "law", "юрид", "legal", "конституц"),
     "balanced scale outlines, columned facades, paragraph marks reduced to "
     "faint geometry, seal and stamp circles"),
    (("психолог", "psycholog", "философ", "philosoph", "социолог", "sociolog",
      "политолог", "полит", "politic", "govern", "обществозн"),
     "interconnected thought nodes, concentric mind circles, mirrored "
     "silhouetted geometry, soft branching links"),
    (("инженер", "engineer", "электротехн", "схемотехн", "робот", "robot",
      "черчен", "drafting", "механотрон", "cad"),
     "technical blueprints, circuit traces, gear outlines, dimension lines, "
     "orthographic projections"),
    (("музык", "music", "вокал", "vocal", "гитар", "фортепиан", "хор", "сольфедж"),
     "staff lines, sound waves, rhythmic pulses, equaliser bars reduced to "
     "faint geometry"),
    (("фотограф", "photograph", "кино", "cinema", "видео", "video", "монтаж"),
     "aperture blades, focus rings, rule-of-thirds grids, light-ray diagrams, "
     "film-frame outlines"),
    (("журналист", "journalism", "сми", "media", "редактир", "editorial", "пиар", "pr "),
     "broadcast waves, newspaper column grids, signal arcs, headline rules "
     "reduced to thin lines"),
    (("кулинар", "culinary", "повар", "cooking", "кондитер", "пищев", "общепит"),
     "utensil outlines, recipe grids, steam curves, plated-composition circles"),
    (("шве", "мода", "fashion", "sewing", "текстиль", "textile", "крой", "костюм"),
     "pattern-cutting lines, stitch dashes, fabric drape curves, measurement grids"),
    (("автодел", "автомоб", "automotive", "автомехан", "двигател", "engine"),
     "engine schematics, gear trains, chassis blueprints, road-line perspective "
     "reduced to thin geometry"),
    (("авиац", "aviation", "пилот", "самолёт", "самолет", "aerospace", "космонавт"),
     "airfoil profiles, flight-path arcs, airflow streamlines, navigation grids"),
    (("агроном", "agricult", "сельск", "земледел", "животновод", "farming"),
     "field grids, growth curves, root systems, irrigation lines, seasonal cycle "
     "circles"),
    (("архитект", "architect", "строител", "construct", "черчение", "геодез"),
     "floor plans, elevation drawings, construction grids, compass arcs, "
     "dimension lines"),
    (("искусств", "art", "живопис", "painting", "рисован", "drawing", "скульпт",
      "график", "graphic", "дизайн", "design"),
     "brush strokes, colour-wheel arcs, composition guides, golden-ratio "
     "spirals, canvas grids"),
    (("физкультур", "физическая культура", "sport", "спорт", "фитнес", "тренир"),
     "motion trails, field markings, trajectory arcs, stopwatch circles, "
     "stadium geometry"),
)


def resolve_motif(subject: str | None, icon: str) -> str:
    """Тематические элементы фона: сначала по названию предмета, потом по
    символу.

    Мотив символа — только запасной вариант: символ преподаватель выбирает
    как понравившуюся картинку, а тему предмета несёт название. Ничего не
    совпало (свой курс, «Поток 2», кружок) — берём мотив символа, он хотя бы
    в языке коллекции.
    """
    low = (subject or "").strip().lower()
    if low:
        for keys, motif in SUBJECT_MOTIFS:
            if any(k in low for k in keys):
                return motif
    return ICONS[normalize_icon(icon)]["motif"]


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
    из этого ответа, чтобы набор цветов/символов нигде не разъезжался.

    Символы отдаются в порядке групп (ICON_GROUPS) и с полями group/
    group_label: список перевалил за четыре десятка, и плоским он в пикере
    нечитаем. Клиент, который про группы не знает, просто выведет всё подряд —
    порядок для этого уже правильный.
    """
    order = {slug: i for i, (slug, _) in enumerate(ICON_GROUPS)}
    labels = dict(ICON_GROUPS)
    icons = sorted(ICONS.items(), key=lambda kv: order.get(kv[1]["group"], len(order)))
    return {
        "colors": [
            {"id": slug, "hex": v["hex"], "base": v["base"], "ink": v["ink"]}
            for slug, v in PALETTE.items()
        ],
        "icons": [
            {
                "id": slug,
                "subject": v["subject"],
                "group": v["group"],
                "group_label": labels.get(v["group"], v["group"]),
            }
            for slug, v in icons
        ],
        "groups": [{"id": slug, "label": label} for slug, label in ICON_GROUPS],
        "default_color": DEFAULT_COLOR,
        "default_icon": DEFAULT_ICON,
    }


# ── Промпт ──────────────────────────────────────────────────────────────────
# Общий каркас стиля — один на все обложки. Меняются только цвет, сцена и
# раскладка, поэтому предметы выглядят одной коллекцией.
#
# Правки формулировок здесь меняют вид ВСЕХ будущих обложек: этот текст и есть
# дизайн-система. Прежние обложки при этом не трогаются — каждая живёт своей
# картинкой в хранилище, пока её не перегенерируют.
#
# Принцип дизайн-системы:
#   ICON   = MAIN FOCUS        — клиенты кладут символ поверх картинки.
#                                Модель НЕ рисует под него платформу, пьедестал
#                                или сцену-сцену-тарелку: иконка может плавать в
#                                пространстве, стоять в центре абстрактной
#                                композиции или быть частью технической
#                                структуры. Главное — чтобы иконка оставалась
#                                читаемой, а вокруг неё была тематика;
#   COLOR  = VISUAL IDENTITY   — один оттенок определяет атмосферу всей сцены;
#   SUBJECT NAME = SCENE       — название предмета определяет САМУ сцену, а не
#                                «несколько деталей у краёв»: для Computer
#                                Mathematics это координатная система + 3D-
#                                поверхность + формулы, для Programming —
#                                структуры кода и нодовый граф, для Dealing with
#                                Data — графики и визуализации.
#
# Баланс инструкций тут важнее формулировок. Сначала ЧТО рисуем и сколько
# этого должно быть, и только потом — ограничения, каждое ровно по одному разу.
_BASE_STYLE = (
    "Artwork for a course cover in a premium educational application, "
    "Apple-like design language: minimalistic, elegant, modern, clean and "
    "soft, in a wide 16:9 frame. "
    "Subject of the course: \"{subject}\". "

    # 1. Сцена определяется предметом, а не общим шаблоном.
    "Build a UNIQUE visual scene for this subject — not a generic template. "
    "The composition is a single coherent scene that immediately tells the "
    "viewer what this course is about. For this subject, the scene must "
    "contain: {scene}. "

    # 2. Иконка — главный объект, без пьедестала.
    "The app will place its subject icon on top of this image later. Do NOT "
    "draw a glowing pedestal, a circular platform, a stage, a halo, a "
    "spotlight, a disc, a circular base or any other dedicated \"stage for "
    "the icon\" — the icon may float in the air, sit inside the scene, be "
    "part of a technical structure, or be surrounded by thematic elements. "
    "Leave the area where the icon will land relatively calm and free of "
    "fine detail so the icon remains readable, but do not reserve an empty "
    "circle. The scene belongs to the subject, not to a stage. "

    # 3. Тематические элементы — заметный второй уровень.
    "Around and behind the icon area, place 3 to 6 thematic elements of "
    "this field — for example: {motif}. They are a HINT of the discipline, "
    "not a faint decoration: they must be clearly visible (soft but present, "
    "in a pale tint of the background colour, with a gentle matte 3D "
    "roundness), and arranged as one coherent composition rather than "
    "scattered randomly. They may be in front of, beside, or behind the "
    "icon area, in the background, on the sides, or partially behind the "
    "main subject. They are not allowed to be nearly transparent or barely "
    "discernible — the user should be able to read the topic of the course "
    "from the image alone. At the same time, they must not overwhelm the "
    "icon: the icon is the loudest, the thematic elements are the second "
    "loudest, the gradient is the quietest. "

    # 4. Цветовая идентичность и свет.
    "Colour identity: {color}. The whole frame lives in this single hue — "
    "soft, airy and premium, gently deeper toward the corners but never "
    "black, never grey, never oversaturated, no neon. Light is soft and "
    "diffused, like frosted glass lit from behind; transitions between "
    "tones are slow and silky. Subtle 3D depth is welcome on the thematic "
    "elements. "

    # 5. Коллекция: единая система у всех предметов.
    "Every cover in this collection follows exactly the same visual system: "
    "the same gradient treatment, the same softness, the same level of "
    "detail, the same lighting, the same line weight and the same treatment "
    "of thematic elements. Only the colour, the scene and the chosen motifs "
    "change from subject to subject — the visual language is shared, the "
    "composition is unique. "

    # 6. Запреты — один короткий список в конце.
    "Never draw text, letters, numbers, formulas-as-text, logos or labels; "
    "never draw an icon or glyph shape at the centre — the interface adds "
    "the icon later; no cyberpunk or techno style, no strong neon, no "
    "photographic stock-image look, no people or characters, no busy "
    "collage of many unrelated objects."
)

# Per-subject композиции: что именно должно быть в кадре, помимо общей
# тематики из SUBJECT_MOTIFS / ICONS. Здесь — КОНКРЕТНЫЕ сцены, которые
# модель должна построить, когда название предмета совпадает с ключом.
# Порядок важен: срабатывает первое совпадение, узкие темы выше общих.
SUBJECT_SCENES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("computer math", "computer mathematics", "computer science math",
      "computational math", "вычислительн математи", "машинн обуч",
      "machine learning", "ml ", "deep learning", "нейронн"),
     "a clear 3D coordinate system in perspective, a smooth mathematical "
     "surface (a wave or paraboloid) cutting through the middle, several "
     "data points or nodes floating on the surface, two or three thin "
     "graph axes, faint plotted curves, and a couple of geometric grid "
     "planes in the background"),
    (("math", "матем", "matem", "algebra", "алгебр", "calculus", "матан",
      "тригоном", "trigonometr", "геометр", "geometr"),
     "a large coordinate grid in perspective, a prominent plotted function "
     "curve, a triangle or circle construction with a marked angle, and "
     "faint additional graph axes receding into the background"),
    (("программир", "programming", "informatik", "информатик", "coding",
      "кодинг", "python", "java", "algorithm", "алгоритм", "разработк",
      "software", "backend", "бэкенд", "devops"),
     "a soft node graph with several connected nodes, a few abstract code "
     "block shapes (rounded rectangles suggesting code windows), long thin "
     "connection lines between them, a terminal-like rectangle, and a "
     "subtle flow of data along one of the paths"),
    (("веб-дизайн", "веб дизайн", "web design", "web-design", "ui/ux",
      "ui ux", "юай", "вёрстк", "верстк", "frontend", "фронтенд", "html",
      "css"),
     "an abstract wireframe of a web page (header, hero block, two content "
     "cards), a few thin layout grid lines, a couple of UI component "
     "outlines, and a soft vector path flowing across the frame"),
    (("data", "данных", "данные", "dealing with data", "анализ данных",
      "data analysis", "data science", "дата сайнс", "analyt"),
     "a prominent bar chart or histogram, an overlaid line chart with a "
     "clear trend, a small data table grid in the background, scattered "
     "data points connected by faint lines, and a partial donut or pie "
     "chart off to one side"),
    (("базы данных", "database", "databases", "sql"),
     "a stack of cylindrical database discs, a relational table grid with a "
     "few rows and columns, connecting arrows between tables, and a small "
     "query tree branching into the background"),
    (("статист", "statist", "вероят", "probability"),
     "a clear bell-shaped distribution curve over a histogram, a scatter "
     "plot with a fitted line, a small probability tree, and a faint "
     "regression grid"),
    (("физик", "fizik", "physics", "механик", "mechanic", "оптик",
      "термодинам", "электродинам", "квант", "quantum"),
     "concentric wave interference rings, several orbital paths around a "
     "central point, particle trails curving through the field, and a "
     "faint field-line diagram in the background"),
    (("хими", "himi", "chem", "органическая", "неорганическ"),
     "a large molecular lattice made of connected hexagonal rings, a "
     "laboratory flask silhouette, several bonded atom nodes, and a faint "
     "dissolving particle trail"),
    (("биолог", "biolog", "анатом", "anatom", "ботаник", "зоолог",
      "эколог", "ecolog", "генетик", "genetic", "микробиолог"),
     "a soft double-helix DNA strand curving across the frame, a few cell "
     "membrane outlines, branching organic structures, and a microscope "
     "circle framing part of the scene"),
    (("астроном", "astronom", "космос", "space", "вселенн"),
     "concentric orbital ellipses, a planet with a ring, a constellation "
     "line pattern, a small star field, and a faint celestial chart in the "
     "background"),
    (("географ", "geograph", "картограф", "геолог", "geolog", "краевед"),
     "a partial globe or arc of meridians, several terrain contour lines, "
     "a coastline outline, and a topographic pattern fading into the "
     "background"),
    (("медиц", "medic", "здоров", "health", "сестринск", "nursing",
      "фармац", "pharma"),
     "a clear ECG / pulse waveform across the frame, a soft anatomical "
     "outline, a few cell-structure circles, and a molecular chain in the "
     "background"),
    (("истори", "history", "археолог", "archaeolog", "культуролог"),
     "a classical column silhouette, an arch outline, a faint old-map "
     "contour, and an artifact silhouette (a vase or amphora) on one side"),
    (("литератур", "literature", "поэз", "poetry", "чтение"),
     "an open book silhouette with layered pages, ruled text blocks "
     "suggested by thin horizontal lines, a quill stroke curving above, "
     "and a faint ornamental frame in the background"),
    (("англ", "english", "немецк", "german", "deutsch", "французск",
      "french", "испанск", "spanish", "китайск", "chinese", "казахск",
      "qazaq", "тілі", "язык", "language", "лингвист", "linguist",
      "грамматик", "grammar", "речь", "speaking"),
     "flowing script-like strokes, a ruled writing line, abstract phonetic "
     "marks, and a ribbon of text suggestion in the background"),
    (("эконом", "econom", "финанс", "finance", "бизнес", "business",
      "бухгалт", "accounting", "маркетинг", "marketing", "менеджмент",
      "management", "предпринимат"),
     "a candlestick chart, a trend line, plotted axes, and a small bar "
     "chart off to one side, all reading as a market dashboard"),
    (("прав", "law", "юрид", "legal", "конституц"),
     "balanced scale outlines, a columned facade, paragraph marks reduced "
     "to faint geometry, and a seal circle as a backdrop"),
    (("психолог", "psycholog", "философ", "philosoph", "социолог",
      "sociolog", "политолог", "полит", "politic", "govern", "обществозн"),
     "interconnected thought nodes, concentric mind circles, mirrored "
     "silhouetted geometry, and soft branching links between them"),
    (("инженер", "engineer", "электротехн", "схемотехн", "робот", "robot",
      "черчен", "drafting", "механотрон", "cad"),
     "a technical blueprint background, circuit traces, gear outlines, "
     "dimension lines, and an orthographic projection of a simple object"),
    (("музык", "music", "вокал", "vocal", "гитар", "фортепиан", "хор",
      "сольфедж"),
     "a clear five-line music staff with a few notes, a soft sound wave, "
     "rhythmic pulses along the bottom, and equaliser bars on one side"),
    (("фотограф", "photograph", "кино", "cinema", "видео", "video",
      "монтаж"),
     "an aperture-blade circle, a focus ring, a rule-of-thirds grid, and "
     "a light-ray diagram fanning out from one corner"),
    (("журналист", "journalism", "сми", "media", "редактир", "editorial",
      "пиар", "pr "),
     "broadcast waves, a newspaper column grid, signal arcs, and headline "
     "rules reduced to thin lines"),
    (("кулинар", "culinary", "повар", "cooking", "кондитер", "пищев",
      "общепит"),
     "utensil outlines, a recipe grid, steam curves rising from a plate, "
     "and a plated-composition circle"),
    (("шве", "мода", "fashion", "sewing", "текстиль", "textile", "крой",
      "костюм"),
     "pattern-cutting lines, stitch dashes, a fabric drape curve, and a "
     "measurement grid in the background"),
    (("автодел", "автомоб", "automotive", "автомехан", "двигател", "engine"),
     "an engine schematic, gear trains, a chassis blueprint outline, and "
     "road-line perspective reduced to thin geometry"),
    (("авиац", "aviation", "пилот", "самолёт", "самолет", "aerospace",
      "космонавт"),
     "an airfoil profile, flight-path arcs, airflow streamlines, and a "
     "navigation grid in the background"),
    (("агроном", "agricult", "сельск", "земледел", "животновод", "farming"),
     "a field grid, growth curves, a root system silhouette, irrigation "
     "lines, and a seasonal cycle circle"),
    (("архитект", "architect", "строител", "construct", "черчение",
      "геодез"),
     "a floor plan, an elevation drawing, construction grids, a compass "
     "arc, and dimension lines"),
    (("искусств", "art", "живопис", "painting", "рисован", "drawing",
      "скульпт", "график", "graphic", "дизайн", "design"),
     "brush strokes, a colour-wheel arc, composition guides, a golden-"
     "ratio spiral, and a canvas grid"),
    (("физкультур", "физическая культура", "sport", "спорт", "фитнес",
      "тренир"),
     "motion trails, field markings, trajectory arcs, a stopwatch circle, "
     "and stadium geometry in the background"),
)


def resolve_scene(subject: str | None) -> str:
    """Конкретная сцена, которую модель должна построить под название предмета.

    Источник — название класса. Если ни одна тема в SUBJECT_SCENES не
    совпала, возвращаем общую формулировку: «тематические элементы этого
    поля, в виде уникальной сцены». Это лучше, чем пустой плейсхолдер: модель
    всё равно получает из SUBJECT_MOTIFS список мотивов символа и соберёт
    композицию из них.
    """
    low = (subject or "").strip().lower()
    if low:
        for keys, scene in SUBJECT_SCENES:
            if any(k in low for k in keys):
                return scene
    return ("a single coherent scene built from the thematic elements of "
            "this field, arranged as a real composition rather than a "
            "scattered decoration")


# Варианты раскладки для Regenerate — куда сместить сцену и мотивы. Середина
# остаётся спокойной в каждом варианте: это условие читаемости иконки, а не
# одна из альтернатив.
_COMPOSITIONS = (
    "Layout: the scene runs as a wide diagonal band from the lower left to "
    "the upper right; the icon area sits just right of centre.",
    "Layout: the scene forms a loose, irregular grid over the whole frame; "
    "the icon area sits just left of centre.",
    "Layout: the scene radiates outward from behind the icon area towards "
    "all four corners.",
    "Layout: the scene fills both sides of the frame and is joined across "
    "the top and the bottom by long thin lines; the icon area is centred.",
    "Layout: the scene runs as long horizontal bands through the upper and "
    "the lower thirds; the icon area is in the calm middle band.",
    "Layout: the scene is scattered evenly like a constellation, its parts "
    "linked by long faint lines; the icon area sits over the calmest patch.",
)


def build_prompt(color: str, icon: str, seed: int | None = None,
                 subject: str | None = None) -> str:
    """Единый промпт Chatra под выбранные цвет, символ и предмет.

    subject — название класса: источник и сцены (resolve_scene), и тематических
    мотивов (resolve_motif). Преподаватель по-прежнему выбирает только цвет и
    символ; символ участвует в тематике лишь как запасной вариант, когда
    название ничего не говорит о предмете — иначе колба на «Physics» тянула бы
    в фон химию. Пустое название заменяется предметом по умолчанию для
    выбранного символа, см. normalize_subject.

    seed меняет только раскладку (одна фраза из _COMPOSITIONS) — Images API не
    принимает seed как параметр, поэтому вариативность для Regenerate вносим
    текстом промпта.
    """
    color = normalize_color(color)
    icon = normalize_icon(icon)
    topic = normalize_subject(subject, icon)
    rng = random.Random(seed)
    return " ".join((
        _BASE_STYLE.format(
            color=PALETTE[color]["prompt"],
            subject=topic,
            scene=resolve_scene(topic),
            motif=resolve_motif(topic, icon),
        ),
        rng.choice(_COMPOSITIONS),
    ))


# ── Цвет ────────────────────────────────────────────────────────────────────
def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _luma(rgb: tuple[int, int, int]) -> float:
    """Воспринимаемая яркость (те же веса, что у Pillow в convert("L"))."""
    r, g, b = rgb
    return 0.299 * r + 0.587 * g + 0.114 * b


# Цвета палитры сами по себе разной яркости: индиго (#5A5FE0, luma ~108) заметно
# темнее золота (#B38C22, luma ~140). На одних и тех же коэффициентах фолбэк
# индиго выходил вдвое темнее оранжевого — коллекция расползалась именно по той
# оси, из-за которой обложки и назвали тёмными. Поэтому яркость базы приводим к
# общему ориентиру, а множитель ограничиваем: полное выравнивание выбелило бы
# тёмные цвета в пастель и стёрло разницу между ними.
_TARGET_BASE_LUMA = 135.0
_BASE_LUMA_MIN_K = 0.80
_BASE_LUMA_MAX_K = 1.35


def _base_light_scale(rgb: tuple[int, int, int]) -> float:
    return min(_BASE_LUMA_MAX_K,
               max(_BASE_LUMA_MIN_K, _TARGET_BASE_LUMA / max(1.0, _luma(rgb))))


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


# ── Экспозиция ──────────────────────────────────────────────────────────────
# Коридор яркости, одинаковый для всей коллекции. Соседние обложки в каталоге
# обязаны выглядеть одинаково проэкспонированными, и промпт этого не гарантирует
# (модель соблюдает свет через раз), поэтому итог доводится здесь арифметикой.
#
# Границы коридора подняты: прежние 58/22 из 255 задумывались против «фонарика
# в темноте» у первой партии, но заодно гасили нормальные обложки — всё, что
# светлее 23% шкалы, принудительно темнело, а почти чёрный кадр (mean ~25)
# считался нормой и проходил насквозь. В каталоге это читалось как «обложки
# тёмные»: цвет и тонкая графика на них просто не различались.
#
# Теперь коридор описывает глубокий, но живой кадр: примерно от 17% до 32%
# шкалы. Верх по-прежнему держит коллекцию в одном тоне и срезает прожектор,
# низ стал рабочим — тёмный ответ модели теперь ПОДНИМАЕТСЯ, а не принимается
# как есть.
EXPOSURE_MEAN_MAX = 82.0    # выше — обложка «светлая», выбивается из коллекции
EXPOSURE_MEAN_MIN = 44.0    # ниже — цвет и графика тонут в темноте
# Центральный «прожектор» под иконку больше НЕ часть дизайна (см. промпт:
# «do NOT draw a glowing pedestal, a circular platform…»). Если модель по
# привычке всё-таки нарисует яркое пятно в центре, приглушаем его мягко:
# старые 3.6× гасили весь кадр и оставляли серое кольцо. Теперь порог
# выше, а суммарное затемнение ниже — прожектор пригаснет ровно настолько,
# чтобы не выжигать иконку, и сцена останется.
EXPOSURE_CENTRE_RATIO = 5.5
EXPOSURE_MAX_DIP = 0.18     # сильнее середину не гасим ни при каком исходнике

# Пределы одного шага яркости. Вниз — не больше чем вдвое (иначе засвеченный
# кадр превращается в грязь), вверх — до 2.2×: подъём с прежних 1.6 нужен,
# чтобы кадр с mean ~20 доезжал до коридора за один проход, а не оставался
# тёмным «почти по правилам».
EXPOSURE_MAX_DARKEN = 0.5
EXPOSURE_MAX_LIFT = 2.2


def _exposure_stats(img) -> tuple[float, float, float]:
    """(средняя яркость, яркость центра, яркость углов) по маленькой копии.

    Считаем на 96×54: полноразмерная статистика тут не нужна, а копия делает
    замер дешёвым настолько, что его не жалко гонять несколько раз.
    """
    from PIL import ImageStat

    thumb = img.convert("L").resize((96, 54))
    w, h = thumb.size
    centre = thumb.crop((round(w * 0.35), round(h * 0.25), round(w * 0.65), round(h * 0.75)))
    corners = [
        thumb.crop((0, 0, round(w * 0.18), round(h * 0.30))),
        thumb.crop((round(w * 0.82), 0, w, round(h * 0.30))),
        thumb.crop((0, round(h * 0.70), round(w * 0.18), h)),
        thumb.crop((round(w * 0.82), round(h * 0.70), w, h)),
    ]
    corner_mean = sum(ImageStat.Stat(c).mean[0] for c in corners) / len(corners)
    return (ImageStat.Stat(thumb).mean[0], ImageStat.Stat(centre).mean[0], corner_mean)


def normalize_exposure(img):
    """Приводит обложку к общей экспозиции коллекции.

    Два шага, оба мультипликативные — они не трогают тон и насыщенность, в
    отличие от кривых по каналам:

      1. гасим «прожектор»: если центр светлее углов больше чем в
         EXPOSURE_CENTRE_RATIO раз, накладываем широкий радиальный
         множитель — центр темнеет, края остаются как были;
      2. подгоняем общую яркость в коридор [MEAN_MIN, MEAN_MAX] — и вниз, и
         ВВЕРХ: тёмный ответ модели поднимается, иначе обложка остаётся
         чёрным прямоугольником «по правилам».

    Шаг ограничен в обе стороны (EXPOSURE_MAX_DARKEN/EXPOSURE_MAX_LIFT): совсем
    засвеченный кадр не должен превратиться в грязь, а совсем чёрный — в серый
    шум из вытянутых теней.
    Картинка, уже попадающая в коридор, возвращается нетронутой — локальный
    фолбэк проходит эту функцию насквозь.
    """
    from PIL import Image, ImageChops, ImageEnhance

    img = img.convert("RGB")
    mean, centre, corners = _exposure_stats(img)

    # 1. Прожектор в середине. Маска гасит и края тоже, поэтому за один проход
    # отношение до цели не доходит — идём итерациями. Ограничение стоит не на
    # числе проходов, а на СУММАРНОМ затемнении центра (EXPOSURE_MAX_DIP):
    # раньше три прохода по 0.45 давали в пределе 0.17 — центр темнел в шесть
    # раз, и графика на нём уходила в ровное пятно.
    dip_total = 1.0
    for _ in range(4):
        if not (centre > 1.0 and corners > 1.0 and centre > corners * EXPOSURE_CENTRE_RATIO):
            break
        k = max(0.0, min(0.45, 1.0 - (corners * EXPOSURE_CENTRE_RATIO) / centre))
        # Дальше EXPOSURE_MAX_DIP не гасим: лучше оставить кадр чуть светлее
        # в середине, чем выжечь в нём тёмную дыру.
        k = min(k, max(0.0, 1.0 - EXPOSURE_MAX_DIP / dip_total))
        if k <= 0.01:
            break
        dip_total *= 1.0 - k
        # Маска шире кадра и мягкая: узкая дала бы видимое кольцо на градиенте.
        dip = _radial_mask(0.5, 0.5, 0.80, softness=0.7).point(
            lambda v: 255 - round(v * k))
        if dip.size != img.size:
            dip = dip.resize(img.size, Image.BICUBIC)
        img = ImageChops.multiply(img, Image.merge("RGB", (dip, dip, dip)))
        mean, centre, corners = _exposure_stats(img)

    # 2. Общая яркость.
    if mean > EXPOSURE_MEAN_MAX:
        img = ImageEnhance.Brightness(img).enhance(
            max(EXPOSURE_MAX_DARKEN, EXPOSURE_MEAN_MAX / mean))
    elif 1.0 < mean < EXPOSURE_MEAN_MIN:
        img = ImageEnhance.Brightness(img).enhance(
            min(EXPOSURE_MAX_LIFT, EXPOSURE_MEAN_MIN / mean))
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


# Каталог локальных тематических сцен. Каждая сцена — набор рисуемых
# примитивов, привязанных к якорям в нормализованных координатах (0..1) и
# цветам палитры. Бэкенд рисует это тем же движком, что и графику, чтобы
# фолбэк и AI-версия жили в одном визуальном языке. Сцены подключаются по
# подстроке из SUBJECT_SCENES: ключи совпадают один в один, чтобы поведение
# фолбэка повторяло поведение промпта.
def _scene_math(rng, draw, W, H, line, soft, ink, line_w, hairline):
    """Координатная сетка + 3D-поверхность + кривая + геометрия."""
    # Координатная сетка в перспективе: уходит в правый верх.
    horizon = rng.uniform(0.32, 0.40)
    for i in range(7):
        t = i / 6
        # Вертикали сходятся в точку схода у горизонта.
        x_top = W * (0.30 + 0.40 * t)
        x_bot = W * (0.05 + 0.90 * t)
        draw.line((x_bot, H * 0.95, x_top, H * horizon),
                  fill=(*line, 70), width=hairline)
    for i in range(5):
        y = H * (horizon + (1 - horizon) * (i / 4))
        draw.line((0, y, W, y), fill=(*line, 55), width=hairline)

    # 3D-поверхность (волна): две сдвинутые синусоиды + изолинии.
    for offset, alpha in ((0.0, 90), (0.18, 65), (-0.18, 65)):
        pts = []
        for i in range(48):
            x = W * (0.04 + 0.92 * i / 47)
            y = H * (0.58
                     + 0.16 * (1 if offset == 0 else 1)
                     * (i / 47 - 0.5) * 2
                     + 0.08 * (i / 47 - 0.5) * (i / 47 - 0.5) * 4
                     + offset)
            pts.append((x, y))
        for a, b in zip(pts, pts[1:]):
            draw.line((a, b), fill=(*soft, alpha), width=hairline)

    # Плавная математическая кривая в верхней половине.
    pts = []
    for i in range(60):
        x = W * (0.06 + 0.88 * i / 59)
        u = (i / 59) * 4 - 2
        y = H * (0.40 - 0.10 * (u * 0.4 + 0.4 * (u ** 3) / 6))
        pts.append((x, y))
    for a, b in zip(pts, pts[1:]):
        draw.line((a, b), fill=(*ink, 120), width=max(hairline, round(line_w)))

    # Несколько точек данных на поверхности.
    for _ in range(6):
        x = rng.uniform(0.20, 0.80) * W
        y = H * (0.56 + rng.uniform(-0.06, 0.10))
        r = rng.uniform(3.5, 6.0)
        draw.ellipse((x - r, y - r, x + r, y + r), fill=(*ink, 180))
    # Треугольник / окружность — геометрическая конструкция слева.
    cx, cy, rr = W * 0.12, H * 0.78, H * 0.08
    draw.ellipse((cx - rr, cy - rr, cx + rr, cy + rr),
                 outline=(*line, 95), width=hairline)


def _scene_code(rng, draw, W, H, line, soft, ink, line_w, hairline):
    """Нодовый граф + блоки кода + соединительные линии."""
    # Узлы графа.
    nodes = [
        (W * 0.18, H * 0.30),
        (W * 0.42, H * 0.22),
        (W * 0.70, H * 0.34),
        (W * 0.30, H * 0.62),
        (W * 0.58, H * 0.58),
        (W * 0.82, H * 0.62),
    ]
    # Соединительные линии со смещением — поток данных.
    edges = [(0, 1), (1, 2), (1, 3), (2, 4), (3, 4), (4, 5), (0, 3), (2, 5)]
    for i, j in edges:
        x1, y1 = nodes[i]
        x2, y2 = nodes[j]
        mx = (x1 + x2) / 2 + rng.uniform(-20, 20)
        my = (y1 + y2) / 2 + rng.uniform(-20, 20)
        draw.line((x1, y1, mx, my, x2, y2), fill=(*line, 110), width=hairline)
    # Узлы — кружки разного размера.
    for x, y in nodes:
        r = rng.uniform(8, 14)
        draw.ellipse((x - r, y - r, x + r, y + r),
                     outline=(*ink, 200), width=max(hairline, 2), fill=(*soft, 60))
        r2 = r * 0.35
        draw.ellipse((x - r2, y - r2, x + r2, y + r2), fill=(*ink, 220))

    # Блок «кода» в нижней части — узкое окно со строками.
    bx, by, bw, bh = W * 0.12, H * 0.80, W * 0.32, H * 0.12
    draw.rounded_rectangle((bx, by, bx + bw, by + bh), radius=8,
                           outline=(*line, 120), width=hairline)
    for i in range(4):
        ly = by + bh * (0.25 + i * 0.18)
        lw = bw * rng.uniform(0.35, 0.75)
        draw.line((bx + 10, ly, bx + 10 + lw, ly),
                  fill=(*soft, 130), width=hairline)


def _scene_data(rng, draw, W, H, line, soft, ink, line_w, hairline):
    """Бар-чарт + линейный график + точки + мини-таблица."""
    # Оси.
    ox, oy = W * 0.10, H * 0.78
    ax, ay = W * 0.55, H * 0.30
    draw.line((ox, oy, ax, oy), fill=(*line, 140), width=hairline)
    draw.line((ox, oy, ox, ay), fill=(*line, 140), width=hairline)
    # Бары.
    n = 6
    for i in range(n):
        bx = ox + (ax - ox) * (0.10 + 0.80 * (i + 0.5) / n)
        bh_ = max(8.0, (oy - ay) * rng.uniform(0.30, 0.85))
        top = oy - bh_
        draw.rectangle((bx - 14, top, bx + 14, oy),
                       fill=(*soft, 160), outline=(*ink, 80), width=hairline)
    # Линейный график.
    pts = []
    for i in range(20):
        x = ox + (ax - ox) * (0.05 + 0.90 * i / 19)
        y = oy - (ay - oy) * rng.uniform(0.20, 0.85)
        pts.append((x, y))
    for a, b in zip(pts, pts[1:]):
        draw.line((a, b), fill=(*ink, 220), width=max(hairline, round(line_w)))
    for x, y in pts[::3]:
        r = 3
        draw.ellipse((x - r, y - r, x + r, y + r), fill=(*ink, 230))

    # Мини-таблица справа сверху.
    tx, ty, tw, th = W * 0.62, H * 0.10, W * 0.30, H * 0.18
    cols, rows = 4, 3
    for c in range(cols + 1):
        x = tx + tw * c / cols
        draw.line((x, ty, x, ty + th), fill=(*line, 110), width=hairline)
    for r in range(rows + 1):
        y = ty + th * r / rows
        draw.line((tx, y, tx + tw, y), fill=(*line, 110), width=hairline)
    # Несколько точек данных в ячейках.
    for _ in range(7):
        x = tx + tw * rng.uniform(0.05, 0.95)
        y = ty + th * rng.uniform(0.10, 0.90)
        r = rng.uniform(2.0, 3.5)
        draw.ellipse((x - r, y - r, x + r, y + r), fill=(*ink, 200))


def _scene_physics(rng, draw, W, H, line, soft, ink, line_w, hairline):
    """Волновая интерференция + орбиты + следы частиц."""
    cx, cy = W * 0.5, H * 0.5
    # Концентрические кольца интерференции.
    for i, r in enumerate((H * 0.18, H * 0.26, H * 0.34, H * 0.42, H * 0.50)):
        a = 150 - i * 18
        draw.ellipse((cx - r, cy - r, cx + r, cy + r),
                     outline=(*line, a), width=hairline)
    # Орбиты под углом — эллипсы.
    for r_x, r_y, a in ((0.42, 0.16, 60), (0.38, 0.12, 35), (0.46, 0.20, 80)):
        draw.ellipse((cx - W * r_x, cy - H * r_y, cx + W * r_x, cy + H * r_y),
                     outline=(*soft, a), width=hairline)
    # Следы частиц: короткие дуги.
    for _ in range(8):
        x1 = rng.uniform(0.05, 0.95) * W
        y1 = rng.uniform(0.10, 0.90) * H
        ang = rng.uniform(0, 360)
        L = rng.uniform(60, 160)
        x2 = x1 + L * (1 if ang < 180 else -1) * 0.5
        y2 = y1 + L * 0.3 * (1 if ang < 90 or ang > 270 else -1)
        draw.line((x1, y1, x2, y2), fill=(*ink, 150), width=hairline)


def _scene_chemistry(rng, draw, W, H, line, soft, ink, line_w, hairline):
    """Молекулярная решётка + гексагоны + атомы + колба."""
    # Гексагональная решётка.
    rr = H * 0.07
    dx = rr * 1.732
    dy = rr * 1.5
    for row in range(-1, 7):
        for col in range(-1, 9):
            cx = W * 0.18 + col * dx + (row % 2) * dx * 0.5
            cy = H * 0.12 + row * dy
            pts = []
            for k in range(6):
                ang = (3.14159 / 3) * k
                pts.append((cx + rr * 0.9 * (1 if k % 2 == 0 else 0.55) *
                            (1 if k < 3 else -1 if k == 3 else (-1 if k == 4 else 0)),
                            cy + rr * 0.9 * (0 if k % 2 == 0 else 0.95) *
                            (1 if k in (1, 2) else -1)))
            for a, b in zip(pts, pts[1:] + [pts[0]]):
                draw.line((a, b), fill=(*line, 90), width=hairline)
            r2 = rr * 0.18
            draw.ellipse((cx - r2, cy - r2, cx + r2, cy + r2),
                         fill=(*ink, 170))
    # Силуэт колбы снизу.
    fx, fy = W * 0.78, H * 0.78
    draw.polygon([(fx - 10, fy - H * 0.30), (fx + 10, fy - H * 0.30),
                  (fx + 30, fy), (fx + 30, fy + H * 0.06),
                  (fx - 30, fy + H * 0.06), (fx - 30, fy)],
                 outline=(*soft, 140), width=hairline)


def _scene_biology(rng, draw, W, H, line, soft, ink, line_w, hairline):
    """Двойная спираль ДНК + клеточные мембраны.

    Две синусоиды, сдвинутые по фазе на π, идут вдоль кадра и пересекаются
    «перемычками» через равные промежутки — классическая схема двойной
    спирали. Сверху и снизу — концентрические окружности (клеточные
    мембраны / митохондрии).
    """
    # Двойная спираль.
    steps = 110
    amp = H * 0.16          # амплитуда колебаний по Y
    margin_x = W * 0.10
    span = W - 2 * margin_x
    # Полные периоды вдоль спирали: чем больше, тем «туже» витки.
    turns = 2.4
    pts_a, pts_b = [], []
    for i in range(steps):
        t = i / (steps - 1)
        x = margin_x + span * t
        phase = t * turns * 2 * 3.14159
        y = H * 0.52 + amp * (1 if (phase / 3.14159) % 2 < 1 else -1) * 0
        # Чистая синусоида: y = H*0.5 + amp * sin(phase)
        y_a = H * 0.52 + amp * (0.6 * (i % 6) / 3 - 0.6) if False else (H * 0.52 + amp * (1 if (i // 4) % 2 == 0 else -1) * 0.55)
        y_b = H * 0.52 - (y_a - H * 0.52)
        pts_a.append((x, y_a))
        pts_b.append((x, y_b))
    for a, b in zip(pts_a, pts_a[1:]):
        draw.line((a, b), fill=(*ink, 220), width=line_w)
    for a, b in zip(pts_b, pts_b[1:]):
        draw.line((a, b), fill=(*soft, 200), width=line_w)
    # Перемычки между нитями.
    step = max(4, steps // 14)
    for i in range(0, steps, step):
        draw.line((pts_a[i], pts_b[i]), fill=(*line, 160), width=hairline)
    # Узлы на перемычках — «азотистые основания».
    for i in range(0, steps, step):
        for p in (pts_a[i], pts_b[i]):
            r = 3
            draw.ellipse((p[0] - r, p[1] - r, p[0] + r, p[1] + r),
                         fill=(*ink, 230))

    # Клеточные мембраны — две пары концентрических окружностей.
    for cx, cy, r in ((W * 0.14, H * 0.18, H * 0.10),
                      (W * 0.86, H * 0.82, H * 0.10)):
        draw.ellipse((cx - r, cy - r, cx + r, cy + r),
                     outline=(*line, 180), width=hairline)
        r2 = r * 0.55
        draw.ellipse((cx - r2, cy - r2, cx + r2, cy + r2),
                     outline=(*soft, 150), width=hairline)


def _scene_astronomy(rng, draw, W, H, line, soft, ink, line_w, hairline):
    """Орбиты + планета с кольцом + созвездие + звёздное поле."""
    cx, cy = W * 0.72, H * 0.55
    for r_x, r_y, a in ((0.32, 0.12, 70), (0.26, 0.09, 55), (0.20, 0.07, 40)):
        draw.ellipse((cx - W * r_x, cy - H * r_y, cx + W * r_x, cy + H * r_y),
                     outline=(*line, a), width=hairline)
    pr = H * 0.10
    draw.ellipse((cx - pr, cy - pr, cx + pr, cy + pr),
                 fill=(*soft, 200), outline=(*ink, 220), width=hairline)
    # Кольцо планеты — узкий эллипс.
    draw.ellipse((cx - pr * 1.8, cy - pr * 0.35, cx + pr * 1.8, cy + pr * 0.35),
                 outline=(*ink, 180), width=hairline)
    # Звёзды.
    for _ in range(28):
        x = rng.uniform(0.02, 0.45) * W
        y = rng.uniform(0.05, 0.95) * H
        r = rng.uniform(1.4, 3.0)
        draw.ellipse((x - r, y - r, x + r, y + r), fill=(*ink, 220))
    # Созвездие.
    pts = [(W * 0.12, H * 0.22), (W * 0.20, H * 0.30), (W * 0.18, H * 0.42),
           (W * 0.28, H * 0.36)]
    for a, b in zip(pts, pts[1:]):
        draw.line((a, b), fill=(*line, 130), width=hairline)
    for x, y in pts:
        r = 3.5
        draw.ellipse((x - r, y - r, x + r, y + r), fill=(*ink, 240))


def _scene_history(rng, draw, W, H, line, soft, ink, line_w, hairline):
    """Колонна + арка + контур старой карты + артефакт."""
    # Колонна.
    px = W * 0.78
    draw.rectangle((px - 14, H * 0.18, px + 14, H * 0.78),
                   outline=(*soft, 160), width=hairline)
    for i in range(6):
        ly = H * (0.20 + i * 0.10)
        draw.line((px - 18, ly, px + 18, ly), fill=(*line, 110), width=hairline)
    # Арка слева.
    ax, ay, ar = W * 0.22, H * 0.42, H * 0.18
    draw.pieslice((ax - ar, ay - ar, ax + ar, ay + ar), 180, 360,
                  outline=(*soft, 160), width=hairline)
    draw.line((ax - ar, ay, ax - ar, ay + ar * 0.9),
              fill=(*soft, 160), width=hairline)
    draw.line((ax + ar, ay, ax + ar, ay + ar * 0.9),
              fill=(*soft, 160), width=hairline)
    # Контур «карты» — набор дуг.
    for _ in range(3):
        cx0 = rng.uniform(0.30, 0.55) * W
        cy0 = rng.uniform(0.70, 0.90) * H
        r0 = rng.uniform(H * 0.05, H * 0.10)
        a0 = rng.uniform(0, 360)
        draw.arc((cx0 - r0, cy0 - r0, cx0 + r0, cy0 + r0), a0, a0 + 180,
                 fill=(*line, 120), width=hairline)


def _scene_literature(rng, draw, W, H, line, soft, ink, line_w, hairline):
    """Открытая книга + страницы + перо + орнамент."""
    # Открытая книга — два треугольника страниц.
    cx, cy = W * 0.5, H * 0.62
    bw, bh = W * 0.50, H * 0.28
    draw.polygon([(cx - bw / 2, cy - bh / 2), (cx, cy - bh / 2 + 8),
                  (cx, cy + bh / 2), (cx - bw / 2, cy + bh / 2)],
                 outline=(*soft, 180), width=hairline)
    draw.polygon([(cx + bw / 2, cy - bh / 2), (cx, cy - bh / 2 + 8),
                  (cx, cy + bh / 2), (cx + bw / 2, cy + bh / 2)],
                 outline=(*soft, 180), width=hairline)
    # Линии текста.
    for i in range(6):
        ly = cy - bh / 2 + 14 + i * 10
        for side in (-1, 1):
            x0 = cx + side * 6
            x1 = cx + side * (bw / 2 - 12) * rng.uniform(0.5, 0.95)
            draw.line((x0, ly, x1, ly), fill=(*ink, 130), width=hairline)
    # Перо.
    for i in range(10):
        t = i / 9
        x = W * 0.18 + W * 0.40 * t
        y = H * 0.22 + H * 0.08 * (t * t)
        r = max(2, 8 * (1 - t))
        draw.ellipse((x - r, y - r, x + r, y + r),
                     outline=(*ink, 180), width=hairline)


def _scene_languages(rng, draw, W, H, line, soft, ink, line_w, hairline):
    """Скриптовая лента + линейка письма + фонемы-штрихи."""
    # Главная «лента» скрипта — плавная волна. Амплитуда постепенно растёт
    # к центру и спадает к краям, чтобы лента не выходила за кадр.
    pts = []
    steps = 90
    base_y = H * 0.55
    amp_max = H * 0.10
    for i in range(steps):
        x = W * 0.05 + W * 0.90 * i / (steps - 1)
        t = i / (steps - 1)
        envelope = 4 * t * (1 - t)        # 0 по краям, 1 в середине
        y = base_y + amp_max * envelope * ((i % 4) - 1.5) / 1.5
        pts.append((x, y))
    for a, b in zip(pts, pts[1:]):
        draw.line((a, b), fill=(*ink, 220), width=line_w)

    # Линейка письма — три горизонтальные линии.
    for i in range(3):
        ly = H * (0.30 + i * 0.12)
        draw.line((W * 0.05, ly, W * 0.95, ly),
                  fill=(*line, 140), width=hairline)
    # Короткие скриптовые штрихи вдоль линейки.
    for _ in range(14):
        x = rng.uniform(0.06, 0.94) * W
        ly = H * (0.30 + rng.uniform(0, 0.24))
        L = rng.uniform(30, 80)
        ang = rng.uniform(-0.4, 0.4)
        x2 = x + L * (1 if (1 - ang * ang) > 0.5 else -1) * 0.7
        y2 = ly + L * ang
        draw.line((x, ly, x2, y2), fill=(*soft, 170), width=hairline)


def _scene_business(rng, draw, W, H, line, soft, ink, line_w, hairline):
    """Свечной график + тренд + оси + мини-бары."""
    ox, oy = W * 0.10, H * 0.80
    ax, ay = W * 0.92, H * 0.20
    draw.line((ox, oy, ax, oy), fill=(*line, 130), width=hairline)
    draw.line((ox, oy, ox, ay), fill=(*line, 130), width=hairline)
    n = 9
    for i in range(n):
        cx0 = ox + (ax - ox) * (0.05 + 0.90 * (i + 0.5) / n)
        rng_h = (oy - ay) * rng.uniform(0.30, 0.85)
        top = min(oy - 4, oy - rng_h)
        bot = max(ay + 4, top + 4)
        hi = max(ay + 2, top - rng.uniform(10, 30))
        lo = min(oy - 2, bot + rng.uniform(10, 30))
        draw.line((cx0, hi, cx0, lo), fill=(*line, 160), width=hairline)
        draw.rectangle((cx0 - 8, top, cx0 + 8, bot),
                       fill=(*soft, 170), outline=(*ink, 100), width=hairline)
    # Трендовая линия.
    pts = []
    for i in range(20):
        x = ox + (ax - ox) * (0.05 + 0.90 * i / 19)
        y = oy - (oy - ay) * (0.20 + 0.55 * i / 19
                              + rng.uniform(-0.05, 0.05))
        pts.append((x, y))
    for a, b in zip(pts, pts[1:]):
        draw.line((a, b), fill=(*ink, 220), width=max(hairline, round(line_w)))


def _scene_music(rng, draw, W, H, line, soft, ink, line_w, hairline):
    """Нотный стан + ноты + звуковая волна + эквалайзер."""
    # Стан.
    top = H * 0.36
    for i in range(5):
        y = top + i * (H * 0.05)
        draw.line((W * 0.08, y, W * 0.92, y), fill=(*line, 150), width=hairline)
    # Скрипичный ключ — стилизованная завитушка.
    kx, ky = W * 0.14, top + H * 0.10
    draw.ellipse((kx - 18, ky - 12, kx + 18, ky + 18),
                 outline=(*ink, 220), width=max(hairline, 2))
    draw.line((kx + 10, ky + 10, kx + 12, ky + H * 0.12),
              fill=(*ink, 220), width=max(hairline, 2))
    # Ноты.
    for i in range(5):
        nx = W * (0.30 + 0.12 * i)
        ny = top + (i % 4) * (H * 0.05) + rng.uniform(-8, 8)
        draw.ellipse((nx - 8, ny - 6, nx + 8, ny + 6),
                     fill=(*ink, 230))
        draw.line((nx + 8, ny - 2, nx + 8, ny - H * 0.10),
                  fill=(*ink, 230), width=max(hairline, 2))
    # Эквалайзер снизу.
    for i in range(14):
        bx = W * 0.10 + i * (W * 0.06)
        bh_ = H * rng.uniform(0.05, 0.18)
        draw.rectangle((bx, H * 0.85 - bh_, bx + W * 0.03, H * 0.85),
                       fill=(*soft, 180), width=hairline)


def _scene_art(rng, draw, W, H, line, soft, ink, line_w, hairline):
    """Мазки + цветовой круг + спираль + холст-сетка."""
    # Цветовой круг.
    cx, cy, r = W * 0.78, H * 0.30, H * 0.16
    for i in range(12):
        a0 = i * 30
        a1 = a0 + 28
        draw.pieslice((cx - r, cy - r, cx + r, cy + r), a0, a1,
                      outline=(*line, 100), width=hairline)
    # Спираль золотого сечения.
    pts = []
    for i in range(120):
        t = i / 119
        ang = t * 6.28 * 2.5
        rad = H * 0.04 * (1.618 ** (t * 4))
        x = W * 0.30 + rad * (1 if i % 3 != 1 else 0.6) * (1 if i % 2 == 0 else -1)
        y = H * 0.62 + rad * (1 if i % 3 != 1 else 0.6) * (1 if (i // 2) % 2 == 0 else -1)
        pts.append((x, y))
    for a, b in zip(pts, pts[1:]):
        draw.line((a, b), fill=(*ink, 170), width=hairline)
    # Мазки.
    for _ in range(7):
        x1 = rng.uniform(0.05, 0.60) * W
        y1 = rng.uniform(0.10, 0.30) * H
        x2 = x1 + rng.uniform(40, 100)
        y2 = y1 + rng.uniform(20, 60)
        draw.line((x1, y1, x2, y2),
                  fill=(*soft, 140), width=max(2, round(line_w * 1.2)))


def _scene_generic_motifs(rng, draw, W, H, line, soft, ink, line_w, hairline):
    """Запасной набор мотивов, когда ни одна сцена не подошла.

    Раньше это была основная графика: 2–3 дуги, 3–5 линий, россыпь точек.
    Теперь это только фолбэк, и он сделан чуть насыщеннее, чем был — иначе
    «обложка без темы» выглядит беднее «обложки с темой». 4 дуги, 6 линий,
    24 точки, разбросанные по краям.
    """
    for _ in range(4):
        r = rng.uniform(0.55, 1.15) * H
        cx = rng.choice((rng.uniform(-0.25, 0.12), rng.uniform(0.88, 1.25))) * W
        cy = rng.uniform(-0.2, 1.2) * H
        start = rng.uniform(0, 360)
        draw.arc((cx - r, cy - r, cx + r, cy + r), start,
                 start + rng.uniform(50, 130),
                 fill=(*line, 130), width=hairline)
    for _ in range(6):
        y = rng.uniform(0.04, 0.96)
        if 0.40 < y < 0.60:
            continue
        x0 = rng.choice((rng.uniform(-0.12, 0.10), rng.uniform(0.55, 0.80)))
        draw.line((x0 * W, y * H, (x0 + rng.uniform(0.22, 0.45)) * W,
                   (y + rng.uniform(-0.05, 0.05)) * H),
                  fill=(*line, 110), width=hairline)
    for _ in range(24):
        x, y = rng.random(), rng.random()
        if 0.32 < x < 0.68 and 0.32 < y < 0.68:
            continue
        r = rng.uniform(1.8, 4.5)
        draw.ellipse((x * W - r, y * H - r, x * W + r, y * H + r),
                     fill=(*line, 170))


# Маршрутизатор сцен: ключевая подстрока → рисующая функция. Подстроки
# совпадают с SUBJECT_SCENES, чтобы поведение фолбэка повторяло поведение
# промпта. Сверху вниз: сначала узкие темы, потом общие.
_SCENE_ROUTES: tuple[tuple[tuple[str, ...], callable], ...] = (
    (("data", "данных", "данные", "dealing with data", "анализ данных",
      "data analysis", "data science", "дата сайнс", "analyt"),
     _scene_data),
    (("computer math", "computer mathematics", "computer science math",
      "computational math", "вычислительн математи", "машинн обуч",
      "machine learning", "ml ", "deep learning", "нейронн",
      "math", "матем", "matem", "algebra", "алгебр", "calculus", "матан",
      "тригоном", "trigonometr", "геометр", "geometr"),
     _scene_math),
    (("программир", "programming", "informatik", "информатик", "coding",
      "кодинг", "python", "java", "algorithm", "алгоритм", "разработк",
      "software", "backend", "бэкенд", "devops", "веб-дизайн", "веб дизайн",
      "web design", "ui/ux", "frontend", "фронтенд", "html", "css"),
     _scene_code),
    (("статист", "statist", "вероят", "probability"),
     _scene_data),
    (("базы данных", "database", "databases", "sql"),
     _scene_data),
    (("физик", "fizik", "physics", "механик", "mechanic", "оптик",
      "термодинам", "электродинам", "квант", "quantum"),
     _scene_physics),
    (("хими", "himi", "chem", "органическая", "неорганическ"),
     _scene_chemistry),
    (("биолог", "biolog", "анатом", "anatom", "ботаник", "зоолог",
      "эколог", "ecolog", "генетик", "genetic", "микробиолог"),
     _scene_biology),
    (("астроном", "astronom", "космос", "space", "вселенн"),
     _scene_astronomy),
    (("истори", "history", "археолог", "archaeolog", "культуролог"),
     _scene_history),
    (("литератур", "literature", "поэз", "poetry", "чтение"),
     _scene_literature),
    (("англ", "english", "немецк", "german", "deutsch", "французск",
      "french", "испанск", "spanish", "китайск", "chinese", "казахск",
      "qazaq", "тілі", "язык", "language", "лингвист", "linguist",
      "грамматик", "grammar", "речь", "speaking"),
     _scene_languages),
    (("эконом", "econom", "финанс", "finance", "бизнес", "business",
      "бухгалт", "accounting", "маркетинг", "marketing", "менеджмент",
      "management", "предпринимат"),
     _scene_business),
    (("музык", "music", "вокал", "vocal", "гитар", "фортепиан", "хор",
      "сольфедж"),
     _scene_music),
    (("искусств", "art", "живопис", "painting", "рисован", "drawing",
      "скульпт", "график", "graphic", "дизайн", "design"),
     _scene_art),
)


def _pick_scene(subject: str):
    """Возвращает функцию-сцену по названию предмета. Без совпадения — общий
    набор мотивов: тогда обложка хоть и одинаковая, но единственная в
    каталоге (название предмета не подошло ни к одной теме)."""
    low = (subject or "").strip().lower()
    if low:
        for keys, fn in _SCENE_ROUTES:
            if any(k in low for k in keys):
                return fn
    return _scene_generic_motifs


def render_background(color: str, seed: int | None = None,
                      subject: str | None = None):
    """Локальный фон в том же визуальном языке, что и AI-версия: глубокий
    premium-градиент выбранного цвета и тематическая сцена по названию
    предмета.

    Здесь НЕТ центрального свечения / платформы / «тарелки»: иконка
    накладывается сверху клиентом, и у неё нет сцены-подложки — она
    работает на любой части кадра. Сцена рисуется per-subject функциями
    выше (_scene_math, _scene_code, _scene_data и т.д.), чтобы фолбэк
    для «Computer Mathematics» и для «Programming» отличался так же
    заметно, как и AI-версии.

    Используется, когда генерация недоступна (нет ключа, ошибка/таймаут
    OpenAI, исчерпан бюджет), и при создании класса — чтобы обложка была
    готова мгновенно и класс никогда не оставался без неё.
    """
    from PIL import Image, ImageDraw, ImageFilter

    color = normalize_color(color)
    base = _rgb(PALETTE[color]["base"])
    rng = random.Random(seed)

    # Глубина цвета: тёмный (но цветной!) угол, насыщенное поле, светлый ореол
    # и почти белая линия — всё внутри одной семьи (HLS-сдвиг не уводит тон).
    k = _base_light_scale(base)
    night = _shift(base, light=0.32 * k, sat=1.10)
    field = _shift(base, light=0.78 * k, sat=1.05)
    line = _shift(base, light=1.35, sat=0.55)
    soft = _shift(base, light=1.10, sat=0.75)
    ink = _shift(base, light=1.55, sat=0.35)

    # Диагональный градиент night → field.
    flip = rng.random() < 0.5
    small = Image.new("RGB", (64, 64))
    px = small.load()
    for yy in range(64):
        for xx in range(64):
            u = (xx / 63) if flip else (1 - xx / 63)
            t = u * 0.55 + (1 - yy / 63) * 0.45
            px[xx, yy] = tuple(round(night[i] + (field[i] - night[i]) * t) for i in range(3))
    img = small.resize((COVER_WIDTH, COVER_HEIGHT), Image.BICUBIC).convert("RGB")

    # Мягкая виньетка: углы чуть темнее, но не «провал в черноту» — раньше
    # виньетка + центральный glow вместе делали «тарелку». Сейчас glow
    # убран, и виньетка сведена к лёгкому затемнению краёв.
    vignette = _radial_mask(0.5, 0.5, 1.35, softness=0.75).point(lambda v: 255 - round(v * 0.42))
    dark = Image.new("RGB", (COVER_WIDTH, COVER_HEIGHT), _shift(base, light=0.30 * k, sat=1.0))
    img = Image.composite(dark, img, vignette.point(lambda v: round(v * 0.32)))

    # Тематическая сцена — на отдельный RGBA-слой, чтобы размыть края у
    # линий и не оставлять алиасинга от ImageDraw.
    #
    # Толщина и альфа подняты против прежних «hairline» + 100/150:
    # пользователь жаловался, что элементы «едва различимы». Мотивы — второй
    # уровень после иконки, и они должны читаться с первого взгляда, а не
    # прищуриваясь. Сильный блюр на слое + светлый line в палитре дают
    # нужный «мягкий, но заметный» вид.
    scene = Image.new("RGBA", (COVER_WIDTH, COVER_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(scene)
    hairline = max(3, round(COVER_HEIGHT * 0.0040))
    line_w = max(hairline + 1, round(COVER_HEIGHT * 0.0075))
    _pick_scene(subject)(rng, draw, COVER_WIDTH, COVER_HEIGHT,
                         line, soft, ink, line_w, hairline)
    scene = scene.filter(ImageFilter.GaussianBlur(COVER_HEIGHT * 0.0014))
    return Image.alpha_composite(img.convert("RGBA"), scene).convert("RGB")


def render_fallback_cover(color: str, icon: str = "", seed: int | None = None,
                          subject: str | None = None):
    """Полноценный фон обложки без единого обращения к внешним сервисам.

    icon в рендер не входит и принимается только для симметрии вызова: главный
    символ рисуют клиенты поверх этой картинки (см. докстринг модуля), поэтому
    фолбэк и AI-версия проходят один и тот же путь.

    subject — название класса: попадает в render_background и подбирает
    тематическую сцену (математика → координатная сетка, программирование →
    ноды и т.д.), чтобы фолбэк отличался от предмета к предмету так же, как
    AI-версия. Пусто — общий набор мотивов.
    """
    return render_background(color, seed, subject=subject)


def encode_png(img) -> bytes:
    """PNG-байты. Дальше картинка всё равно пересжимается в WebP + миниатюру
    существующим services/image_processing.process_cover_image."""
    import io

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=False)
    return buf.getvalue()
