"""Подписанные ссылки на файлы в /uploads (SEC-1).

Файлы больше не отдаются как публичная статика. Доступ к файлу = наличие
действующей HMAC-подписи в URL. Подпись выдаёт сам сервер, когда возвращает
ресурс, ссылающийся на файл (сдачу, эталон, обложку, вложение) — а значит
подпись получает только тот, кто имеет право прочитать сам ресурс.

Модель:
  * ACL проверяется в момент ВЫДАЧИ ссылки (эндпоинт ресурса уже защищён
    правами доступа), а подпись лишь удостоверяет «сервер это авторизовал».
  * Прокси-эндпоинт /api/uploads/<file> отдаёт файл только с валидной непросро-
    ченной подписью — прямой публичный доступ по угадыванию UUID закрыт.

Подпись: HMAC-SHA256(secret, "<path>:<exp>") в hex. Секрет — SECRET_KEY
приложения. exp — unix-время истечения (по умолчанию сутки).
"""
import hashlib
import hmac
import os
import re
import time
from urllib.parse import urlencode

from security import SECRET_KEY

# Срок жизни подписи. Достаточно долгий, чтобы не рвать открытые страницы и
# кэш <img>, но ограниченный — утёкшая ссылка живёт не вечно.
_DEFAULT_TTL = int(os.getenv("FILE_URL_TTL", str(24 * 3600)))

_SIG_PARAMS = ("exp", "sig")


def _sign(path: str, exp: int) -> str:
    msg = f"{path}:{exp}".encode()
    return hmac.new(SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()


def make_signature(path: str, ttl: int = _DEFAULT_TTL) -> tuple[int, str]:
    """Возвращает (exp, sig) для относительного пути файла (без /uploads/)."""
    exp = int(time.time()) + ttl
    return exp, _sign(path, exp)


def verify_signature(path: str, exp, sig: str) -> bool:
    """True, если подпись верна и не истекла. Постоянное время сравнения."""
    if not sig:
        return False
    try:
        exp_int = int(exp)
    except (TypeError, ValueError):
        return False
    if time.time() > exp_int:
        return False
    expected = _sign(path, exp_int)
    return hmac.compare_digest(expected, sig)


def _app_base() -> str:
    return os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")


def _uploads_base() -> str:
    return f"{_app_base()}/api/uploads/"


def _legacy_uploads_base() -> str:
    """Старая (до переноса под /api) база — эндпоинт по этому пути больше не
    существует, но в БД могли остаться ссылки, записанные до переноса.
    Распознаём и нормализуем их к новой базе, не трогая данные в БД."""
    return f"{_app_base()}/uploads/"


def sign_upload_url(url: str, ttl: int = _DEFAULT_TTL) -> str:
    """Подписывает абсолютный URL вида {APP_BASE_URL}/api/uploads/<path>
    (или старый {APP_BASE_URL}/uploads/<path> — нормализуется к новому).
    Идемпотентна: старые exp/sig в query отбрасываются и подпись ставится
    заново от «чистого» пути. URL не из своего хранилища возвращается как есть.
    """
    base = _uploads_base()
    if url and url.startswith(base):
        rest = url[len(base):]
    else:
        legacy_base = _legacy_uploads_base()
        if url and url.startswith(legacy_base):
            rest = url[len(legacy_base):]
        else:
            return url
    path = rest.split("?", 1)[0].split("#", 1)[0]
    if not path:
        return url
    exp, sig = make_signature(path, ttl)
    return f"{base}{path}?{urlencode({'exp': exp, 'sig': sig})}"


# Символы, допустимые в пути файла внутри JSON-текста. Ключи в R2 хранят
# оригинальное имя файла (services/storage/base.py: build_key), которое может
# содержать юникод (кириллицу) и пробелы — поэтому \w (юникодный по умолчанию
# в Python 3) вместо A-Za-z0-9, плюс пробел и разделитель категорий "/".
# Кавычки, "?", "&", "#" и т.п. в имя не попадают (см. _sanitize_filename),
# так что путь всегда корректно обрывается на границе значения в JSON. Пробел
# разрешён только МЕЖДУ путевыми символами (не в начале/конце совпадения) —
# _sanitize_filename никогда не оставляет пробел на конце имени, так что
# завершающий пробел в совпадении — это уже посторонний текст, а не часть
# ключа. Общая длина ограничена — реальные ключи короче лимита build_key.
#
# "%XX" тоже входит: сам ключ в R2 percent-encoding не содержит, НО клиенты
# (Nuxt encodeURI(), Flutter Uri.encodeComponent()) percent-кодируют путь
# ПЕРЕД тем, как вставить ссылку в текст description — то есть в самом JSON-
# ответе путь уже содержит "%20" и т.п. Без этого альтерната regex обрывался
# на первом "%" (например, "card%202.jpg" матчился только как "card"), и
# остаток строки ("%202.jpg?exp=...&sig=...#card%202.jpg") оставался в тексте
# как есть — сырой мусор, видимый пользователю в описании.
_PATH_MAX_LEN = 400
_PATH_SEGMENT = r"(?:[\w\-./]|%[0-9A-Fa-f]{2})+"
_PATH_CHARS = _PATH_SEGMENT + r"(?:[ ]+" + _PATH_SEGMENT + r")*"


# Регулярка для поиска uploads-URL в JSON-ответах (см. middleware в main.py).
# Захватывает базу + путь + необязательный старый query (чтобы переподписать).
# Матчит и текущую базу (.../api/uploads/), и старую (.../uploads/, из записей
# в БД, сделанных до переноса под /api) — обе нормализуются к новой при подписи.
def _uploads_url_regex() -> re.Pattern:
    base = re.escape(_app_base()) + r"(?:/api)?/uploads/"
    return re.compile(base + r"(" + _PATH_CHARS + r")(\?[A-Za-z0-9_\-.=&%]*)?")


# Относительная ссылка «/uploads/<файл>» (и её вариант с /api): так вложения
# сохраняет мобильное приложение (оно намеренно режет хост, чтобы ссылка не
# привязывалась к конкретному LAN-адресу). Без подписи такой URL отдавал 422 —
# фото не открывалось ни в приложении, ни на сайте, поэтому подписываем и его.
# Ограничение по предшествующему символу (кавычка/пробел/скобка) не даёт
# зацепить путь внутри уже подписанного абсолютного URL и чужие хосты.
_RELATIVE_UPLOADS_RE = re.compile(
    r"(?P<pre>[\"'\s,\[(])(?:/api)?/uploads/(?P<path>" + _PATH_CHARS + r")"
    r"(?P<query>\?[A-Za-z0-9_\-.=&%]*)?"
)


def sign_uploads_in_text(text: str) -> str:
    """Подписывает все ссылки на своё хранилище в готовом текстовом теле
    ответа (JSON) — и абсолютные, и относительные. Существующие подписи
    заменяются на свежие."""
    base = _uploads_base()

    def _signed(path: str) -> str:
        exp, sig = make_signature(path)
        return f"{base}{path}?{urlencode({'exp': exp, 'sig': sig})}"

    def _sub_absolute(m: re.Match) -> str:
        path = m.group(1)
        if len(path) > _PATH_MAX_LEN:
            return m.group(0)
        return _signed(path)

    def _sub_relative(m: re.Match) -> str:
        path = m.group("path")
        if len(path) > _PATH_MAX_LEN:
            return m.group(0)
        return f"{m.group('pre')}{_signed(path)}"

    text = _uploads_url_regex().sub(_sub_absolute, text)
    return _RELATIVE_UPLOADS_RE.sub(_sub_relative, text)
