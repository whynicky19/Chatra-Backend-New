-- Оформление обложки класса: обложки больше не загружаются пользователем, а
-- генерируются по паре «цвет + предметная иконка» (см. services/cover_art.py,
-- services/cover_generator.py, POST /classes/{id}/cover/generate).
--
--   cover_color   — слаг цвета из палитры Chatra (blue/purple/green/orange/
--                   red/pink/teal/indigo)
--   cover_icon    — слаг предметной иконки (sigma/atom/flask/dna/code/column/
--                   globe/letter/book/chart/palette/note)
--   cover_source  — откуда взялась текущая картинка: 'ai' | 'fallback' | 'upload'
--
-- Миграция ТОЛЬКО добавляет колонки. Существующие классы остаются с NULL во
-- всех трёх — это и есть признак «обложка загружена пользователем по старой
-- системе»: cover_image/cover_thumbnail у них не трогаются, картинки из
-- хранилища не удаляются, показ нигде не меняется. Класс переходит на новую
-- систему в тот момент, когда преподаватель сам выберет цвет с иконкой и
-- сгенерирует обложку; вот тогда прежний файл подчистит существующий
-- механизм замены обложки (crud/classes.py: update_class, BE-10).
--
-- Обратная совместимость: колонки nullable и без DEFAULT, поэтому старый код
-- приложения продолжает работать на новой схеме, а новый — на старой базе
-- деградирует ровно как для класса с легаси-обложкой.
ALTER TABLE classes ADD COLUMN IF NOT EXISTS cover_color VARCHAR(16);
ALTER TABLE classes ADD COLUMN IF NOT EXISTS cover_icon VARCHAR(32);
ALTER TABLE classes ADD COLUMN IF NOT EXISTS cover_source VARCHAR(16);
