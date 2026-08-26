"""Единая визуальная система обложек классов Chatra: палитра, промпт и
локальный рендер фона.

Обложка — это ДВА независимых слоя, и разделение принципиально:

  1. фон — глубокий premium-баннер 16:9 (генерирует OpenAI, см. services/
     cover_generator.py; при любом сбое — локальный render_background);
  2. главный символ предмета — НЕ часть картинки. Его рисуют клиенты поверх
     фона нативным компонентом (components/classes/SubjectCover.vue на вебе,
     widgets/subject_cover.dart в приложении).

Модель отвечает только за фон и атмосферу. Символы вроде Σ, спирали ДНК или
колбы генеративные модели воспроизводят нестабильно (лишние штрихи, зеркальные
буквы, псевдотекст), поэтому символ живёт в UI: там он всегда одного размера,
одной толщины линии и одного стиля во всех предметах, его можно перекрасить или
заменить без перегенерации всех обложек.

Визуальный ориентир — глубокий premium / Apple-like: насыщенный выбранный цвет
с мягким градиентом, много свободного пространства, мягкое свечение и глубина.
«Глубокий» здесь — НЕ «чёрный»: первая версия системы была выкручена в темноту
(промпт просил near-black по углам, коридор экспозиции резал среднюю яркость до
58 из 255), и обложки выходили почти чёрными прямоугольниками, на которых не
читались ни цвет, ни графика. Цвет обязан быть виден в каждой точке кадра,
включая углы; ориентир средней яркости — примерно четверть шкалы, а не десятая.
Тематика предмета живёт в ФОНЕ: тонкие полупрозрачные линии, сетки, волны,
частицы и другая геометрия, встроенные в градиент и уходящие в него, а не набор
отдельных картинок. Главный акцент обложки — символ, который кладёт UI, поэтому
середина кадра остаётся спокойной: фон вторичен и создаёт атмосферу.

Что здесь уже пробовали и почему не вернулись:
  • яркая «редакторская» иллюстрация из крупных цветных форм — обложки
    получались нарядными, но каждая жила своей жизнью, коллекции не было;
  • светлая пастель и чистые полосы под иконку — выглядело как SaaS-дашборд;
  • композиция без спокойной середины — белый символ терялся в декоре.
Отсюда нынешний набор правил: глубокий цвет (но не чернота), тонко, тематично,
с пустой серединой.
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
# Общий каркас стиля — один на все обложки. Меняются только цвет и тематика
# фона, поэтому предметы выглядят одной коллекцией.
#
# Правки формулировок здесь меняют вид ВСЕХ будущих обложек: этот текст и есть
# дизайн-система. Прежние обложки при этом не трогаются — каждая живёт своей
# картинкой в хранилище, пока её не перегенерируют.
#
# Принцип дизайн-системы (2026, редизайн по референсу «soft hero object»):
#   ICON   = MAIN FOCUS        — центр кадра это СЦЕНА под иконку, которую
#                                клиенты кладут поверх картинки; сама модель
#                                глиф не рисует, только свето-глубину под него;
#   COLOR  = VISUAL IDENTITY   — один оттенок, один мягкий градиент;
#   SUBJECT NAME = BACKGROUND CONTEXT — название предмета лишь источник двух-
#                                трёх ненавязчивых мотивов у краёв кадра.
#
# Баланс инструкций тут важнее формулировок. Версия на 4000 символов и 34
# запрета (одиннадцать из которых требовали пустоты: «hairline», «low
# contrast», «generous empty space», «empty margin along all four edges»,
# «stays dark and calm») дала ровно то, что и должна была: модель выполнила
# все ограничения сразу, не нарисовав ничего — голый градиент с парой пылинок.
# Поэтому сначала ЧТО рисуем и сколько этого должно быть, и только потом —
# ограничения, каждое ровно по одному разу.
_BASE_STYLE = (
    "Artwork for a course cover in a premium educational application, "
    "Apple-like design language: minimalistic, elegant, modern, clean and "
    "soft, in a wide 16:9 frame. "
    "Subject of the course: \"{subject}\" — use it only as background "
    "context, never write it or any other word on the image. "

    # 1. Единственный фокус композиции — центральная сцена под иконку.
    # Яркость сцены названа умеренной специально: кадр проходит через
    # normalize_exposure, и центр светлее углов больше чем в ~2.2 раза гасится
    # радиальной маской — белый «прожектор» превращался в грязное пятно.
    "The composition has ONE clear focal point at the centre: a softly "
    "glowing stage for the app's subject icon — a calm, gently luminous "
    "pool of the same hue as the background, lifted toward pastel, only "
    "moderately brighter than the field around it, with a wide soft halo "
    "and a whisper of depth beneath it, like a shallow pedestal of light. "
    "Its light is tinted, never white and never a spotlight. Every element "
    "of the drawing defers to this central glow: it is the calmest, most "
    "important place in the frame, and nothing may stand inside it. "

    # 2. Предмет — источник фоновых мотивов, всегда второстепенных.
    "Around that centre, the subject appears only as quiet background "
    "context: two or three large, understated motifs of this field — for "
    "example: {motif} — drifting near the left and right edges. These are "
    "only examples: if they do not fit the subject, use elements that do, "
    "the subject name always wins. Draw them as soft, semi-transparent "
    "shapes in a pale tint of the background colour, gently fading into "
    "the gradient, with a subtle matte 3D roundness. They are a hint of "
    "the discipline, not a catalogue: no more than three motifs, clear air "
    "between them, and they must never compete with the central glow in "
    "brightness or detail. "

    # 3. Цветовая идентичность и свет.
    "Colour identity: {color}. The whole frame is one smooth, subtle "
    "gradient of this single hue — soft, airy and premium, gently deeper "
    "toward the corners, never black, never grey, never oversaturated, no "
    "neon. Light is soft and diffused, like frosted glass lit from behind; "
    "transitions between tones are slow and silky. "

    # 4. Коллекция: единая система у всех предметов.
    "Every cover in this collection follows exactly the same visual system: "
    "the same central-glow composition, the same softness, the same level "
    "of detail, the same lighting and the same treatment of background "
    "motifs. Only the colour, the chosen motifs and their placement change "
    "from subject to subject. "

    # 5. Запреты — один короткий список в конце.
    "Never draw text, letters, numbers or logos; never draw an icon or "
    "glyph shape at the centre — the glow itself is the stage, the "
    "interface adds the icon later; no busy backgrounds, no collages of "
    "many objects, no rows of icons, no cyberpunk or techno style, no "
    "strong neon, no clutter in the corners, no photographic stock-image "
    "look, no people or characters."
)

# Варианты раскладки для Regenerate — куда сместить свет и где собрались
# мотивы. Центральная сцена остаётся свободной в каждом варианте: это условие
# читаемости иконки, а не одна из альтернатив.
_COMPOSITIONS = (
    "Layout: two motifs balance each other at the lower left and upper right "
    "edges, the central glow sits just above centre.",
    "Layout: one large motif half-fades beyond the right edge, a smaller "
    "echo answers it near the lower left, the glow centred.",
    "Layout: the motifs drift as a loose arc through the upper third, the "
    "glow rests slightly below centre.",
    "Layout: the motifs sit far apart at opposite edges, linked by one long "
    "thin horizon line passing behind the central glow.",
    "Layout: the motifs are small and scattered like distant constellations "
    "in the outer thirds, the middle of the frame almost entirely calm air.",
)

# Варианты раскладки для Regenerate — куда сместить свет и где собрать
# тематику. Середина остаётся спокойной в каждом варианте: это не «тоже
# направление», а условие читаемости символа.
_COMPOSITIONS = (
    "Layout: the drawing runs across the frame as a wide diagonal band from "
    "the lower left to the upper right, the glow sits just right of centre.",
    "Layout: the drawing forms a loose, irregular grid over the whole frame, "
    "the glow sits just left of centre.",
    "Layout: the drawing radiates outward from behind the central area towards "
    "all four corners.",
    "Layout: the drawing fills both sides of the frame and is joined across "
    "the top and the bottom by long thin lines.",
    "Layout: the drawing runs as long horizontal bands through the upper and "
    "the lower thirds, the glow spread wide across the middle.",
    "Layout: the drawing is scattered evenly like a constellation, its parts "
    "linked by long faint lines.",
)


def build_prompt(color: str, icon: str, seed: int | None = None,
                 subject: str | None = None) -> str:
    """Единый промпт Chatra под выбранные цвет, символ и предмет.

    subject — название класса: и тема фона, и источник примеров тематических
    элементов (resolve_motif). Преподаватель по-прежнему выбирает только цвет и
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
# Центральная сцена под иконку — НАМЕРЕННАЯ часть нового дизайна (см. промпт:
# «softly glowing stage»), поэтому порог «прожектора» поднят: свечение в
# центре вправе быть заметно ярче углов. Старые 2.2 давили лёгкое свечение
# до грязно-серого пятна посреди кадра.
EXPOSURE_CENTRE_RATIO = 3.6
EXPOSURE_MAX_DIP = 0.30     # сильнее середину не гасим ни при каком исходнике

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


def render_background(color: str, seed: int | None = None):
    """Локальный фон в том же визуальном языке, что и AI-версия: глубокий
    premium-градиент выбранного цвета, мягкое свечение к середине, тонкие
    полупрозрачные линии, дуги и частицы по краям, спокойный центр.

    Тематики предмета здесь нет намеренно: её приносит модель. Фолбэк должен
    оставаться нейтральным членом той же коллекции — глубина цвета, мягкость и
    пустая середина у него ровно те же, что у сгенерированной обложки.

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
    #
    # Коэффициенты подняты вместе с коридором экспозиции: на 0.17/0.42 фолбэк
    # давал среднюю яркость около 29 из 255 — цвет угадывался только у самого
    # свечения, а тонкая графика по краям пропадала в темноте. Фолбэк обязан
    # попадать в тот же коридор, что и AI-версия, иначе normalize_exposure
    # начнёт его переосветлять (см. EXPOSURE_MEAN_MIN).
    k = _base_light_scale(base)
    night = _shift(base, light=0.32 * k, sat=1.10)
    field = _shift(base, light=0.75 * k, sat=1.05)
    glow = _shift(base, light=1.05 * k, sat=0.80)
    line = _shift(base, light=1.40, sat=0.45)

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

    # Свет у середины — источник глубины, а не прожектор: широкий и тусклый.
    # Узкое яркое пятно здесь уже было и выжигало центр, из-за чего обложка
    # выглядела как фонарик в темноте. Смещаем слабо: символ ложится по
    # центру, и уводить свет далеко от него нельзя.
    gx = 0.5 + rng.uniform(-0.09, 0.09)
    gy = 0.5 + rng.uniform(-0.07, 0.07)
    halo = Image.new("RGB", (COVER_WIDTH, COVER_HEIGHT), glow)
    img = Image.composite(halo, img, _radial_mask(gx, gy, rng.uniform(0.85, 1.05)).point(
        lambda v: round(v * 0.28)))

    # Виньетка — та самая premium-глубина: углы уходят в тень. Кладём её ДО
    # тонкой графики: наоборот она гасила линии до полной невидимости —
    # «тонкие и полупрозрачные» не значит «которых нет».
    #
    # Ослаблена (было 0.80/0.55 поверх почти чёрного 0.08): виньетка съедала
    # треть кадра в черноту, и обложка читалась как тёмный прямоугольник с
    # пятном света посередине. Теперь угол — тень выбранного цвета, а не тень
    # вообще.
    vignette = _radial_mask(0.5, 0.5, 1.35, softness=0.75).point(lambda v: 255 - round(v * 0.62))
    dark = Image.new("RGB", (COVER_WIDTH, COVER_HEIGHT), _shift(base, light=0.22 * k, sat=1.0))
    img = Image.composite(dark, img, vignette.point(lambda v: round(v * 0.42)))

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
                 fill=(*line, rng.randint(105, 150)), width=hairline)

    for _ in range(rng.randint(3, 5)):
        y = rng.uniform(0.04, 0.96)
        if not far_from_centre(0.5, y):
            continue
        x0 = rng.choice((rng.uniform(-0.12, 0.10), rng.uniform(0.55, 0.80)))
        draw.line((x0 * COVER_WIDTH, y * COVER_HEIGHT,
                   (x0 + rng.uniform(0.22, 0.45)) * COVER_WIDTH,
                   (y + rng.uniform(-0.05, 0.05)) * COVER_HEIGHT),
                  fill=(*line, rng.randint(75, 110)), width=hairline)

    for _ in range(rng.randint(16, 24)):
        x, y = rng.random(), rng.random()
        if not far_from_centre(x, y):
            continue
        r = rng.uniform(1.8, 4.5)
        draw.ellipse((x * COVER_WIDTH - r, y * COVER_HEIGHT - r,
                      x * COVER_WIDTH + r, y * COVER_HEIGHT + r),
                     fill=(*line, rng.randint(130, 200)))

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
