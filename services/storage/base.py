"""Интерфейс объектного хранилища для новых файлов.

Структура хранилища — по типу содержимого, а не одна общая папка (см.
CATEGORIES). Ключ объекта строится StorageService.build_key(): '<category>/
<человекочитаемое-имя>[.<ext>]', с оригинальным именем файла вместо
случайного UUID и авто-суффиксом при конфликте имён (см. docstring build_key).
Новые загрузки от клиента (upload_unique) дополнительно получают случайный
токен в имени — путь непредсказуем для перебора (см. docstring upload_unique).
"""
import os
import re
from abc import ABC, abstractmethod
from uuid import uuid4

# Категории верхнего уровня для организации файлов в R2. Внутри категории
# допустимы дополнительные подпапки (например "lectures/slides/<batch>") —
# это тоже "автоматически формируемый путь", просто более специфичный.
CATEGORIES = {"materials", "assignments", "submissions", "attachments"}

# Категория для серверных производных артефактов (не принимает загрузку от
# клиента напрямую через /upload/ — её нет в CATEGORIES выше, форма туда не
# сможет отправить файл). Сейчас: PDF, сконвертированный из .ppt/.pptx для
# предпросмотра на сайте (см. routers/uploads.py: get_preview_pdf). Ключ
# детерминирован от пути исходника, поэтому конвертация — по сути кэш.
PREVIEW_CATEGORY = "previews"

# Префиксы ключей, которые не содержат ничего приватного (их и так видит
# любой, кто открыл список классов) — в отличие от сдач/эталонов, им не
# нужна HMAC-подпись/прокси через бэкенд (SEC-1 продолжает защищать всё
# остальное). Для них get_url() отдаёт прямую ссылку на R2, если задан
# R2_PUBLIC_BASE_URL (см. r2_storage.py).
PUBLIC_KEY_PREFIXES = ("materials/covers/",)


def is_public_key(key: str) -> bool:
    return key.startswith(PUBLIC_KEY_PREFIXES)

_UNSAFE_CHARS_RE = re.compile(r"[^\w\-. ]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")
_MAX_STEM_LENGTH = 150
_MAX_COLLISION_ATTEMPTS = 20


class StorageError(Exception):
    """Неустранимый сбой хранилища (после исчерпания ретраев)."""


def _sanitize_filename(original_filename: str) -> tuple[str, str]:
    """Возвращает (stem, ext) — безопасные для ключа R2 и для заголовка
    Content-Disposition: без разделителей пути, управляющих символов и т.п.
    Юникод (кириллица) сохраняется — файл должен остаться понятным юзеру."""
    base = os.path.basename((original_filename or "").strip())
    # Не os.path.splitext(): она считает ведущую точку частью unix hidden-file
    # имени, а не разделителем — для original_filename вида ".pdf" отдавала бы
    # stem=".pdf", ext="" (после нашей санитизации — stem="pdf", ext="" —
    # ключ строился бы вообще БЕЗ расширения). Здесь просто имя файла из
    # формы загрузки, а не путь к скрытому файлу — берём последнюю точку в
    # любой позиции.
    dot = base.rfind(".")
    if dot != -1:
        stem, ext = base[:dot], base[dot + 1:]
    else:
        stem, ext = base, ""
    ext = re.sub(r"[^A-Za-z0-9]", "", ext).lower()
    stem = _UNSAFE_CHARS_RE.sub("_", stem)
    stem = _WHITESPACE_RE.sub(" ", stem).strip(" ._")
    stem = stem[:_MAX_STEM_LENGTH] or "file"
    return stem, ext


class StorageService(ABC):
    @abstractmethod
    def upload(self, content: bytes, key: str, content_type: str = "application/octet-stream",
               cache_control: str | None = None) -> str:
        """Загружает файл под указанным ключом. Возвращает URL для сохранения в БД.
        cache_control, если задан, идёт в S3 Cache-Control метаданные объекта."""

    @abstractmethod
    def replace(self, content: bytes, key: str, content_type: str = "application/octet-stream",
                cache_control: str | None = None) -> str:
        """Перезаписывает файл по существующему ключу. Возвращает URL."""

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Удаляет файл. True — файл был удалён, False — его и так не было."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Проверяет наличие файла в хранилище."""

    @abstractmethod
    def get_url(self, key: str) -> str:
        """Возвращает URL для отдачи файла клиенту (см. docstring в r2_storage.py)."""

    @abstractmethod
    def put_if_absent(self, content: bytes, key: str, content_type: str,
                       cache_control: str | None) -> bool:
        """Атомарно записывает content под key, только если key ещё свободен
        (condition write). True — записано, False — key уже занят (запись не
        произошла, содержимое существующего объекта не тронуто)."""

    def build_key(self, category: str, original_filename: str) -> str:
        """Строит ключ '<category>/<имя>[.<ext>]', сохраняя оригинальное имя
        файла (а не случайный UUID), чтобы Content-Disposition при скачивании
        показывал юзеру понятное имя. При конфликте имени внутри категории
        добавляет числовой суффикс (_1, _2, ...), а после 20 неудачных попыток —
        короткий UUID-хвост, чтобы загрузка никогда не падала из-за коллизии.

        ВНИМАНИЕ: сам по себе (без немедленной атомарной записи по этому же
        ключу) подвержен TOCTOU — см. upload_unique(), который и нужно
        использовать для новых загрузок от клиента. build_key() оставлен
        только для мест, где ключ и так гарантированно уникален (UUID уже
        в original_filename, см. services/image_storage.py) или где гонка
        невозможна по конструкции (одноразовые миграции)."""
        category = category.strip("/")
        stem, ext = _sanitize_filename(original_filename)
        suffix = f".{ext}" if ext else ""

        candidate = f"{category}/{stem}{suffix}"
        if not self.exists(candidate):
            return candidate
        for i in range(1, _MAX_COLLISION_ATTEMPTS + 1):
            candidate = f"{category}/{stem}_{i}{suffix}"
            if not self.exists(candidate):
                return candidate
        return f"{category}/{stem}_{uuid4().hex[:8]}{suffix}"

    def upload_unique(self, content: bytes, category: str, original_filename: str,
                       content_type: str = "application/octet-stream",
                       cache_control: str | None = None) -> str:
        """Как build_key()+upload(), но без TOCTOU-гонки между ними: раньше
        exists()-проверка и запись были раздельными вызовами, и два
        параллельных запроса с одинаковым (после санитайза) именем файла в
        одной категории — например, два студента, назвавших сдачу от одного
        шаблона задания одинаково, — могли оба увидеть "ключ свободен" и оба
        записать по нему; второй PUT молча перезаписывал байты первого без
        единой ошибки или лога, и файл первого студента терялся насовсем, а
        оба запроса возвращали 200 с одним и тем же file_url. Здесь каждая
        попытка — один атомарный condition write (put_if_absent).

        Ключ нового файла всегда получает случайный токен ('<имя>_<token>'):
        доступ к приватным файлам = знание точного пути + HMAC-подписи.
        Предсказуемые человекочитаемые пути ('submissions/report.pdf') можно
        было перебрать, а мидлварь подписей подпишет ЛЮБОЙ /uploads/-путь,
        встреченный в JSON-ответе (в т.ч. вставленный злоумышленником в текст
        сообщения/описания) — случайный токен делает такой перебор
        неосуществимым. Понятное имя файла для скачивания не страдает:
        клиенты хранят оригинальное имя в #fragment, а Content-Disposition
        строится из ключа, который по-прежнему читаем."""
        category = category.strip("/")
        stem, ext = _sanitize_filename(original_filename)
        suffix = f".{ext}" if ext else ""

        for _ in range(_MAX_COLLISION_ATTEMPTS):
            candidate = f"{category}/{stem}_{uuid4().hex[:8]}{suffix}"
            if self.put_if_absent(content, candidate, content_type, cache_control):
                return self.get_url(candidate)
        raise StorageError(f"Не удалось подобрать свободный ключ для {category}/{stem}{suffix}")
