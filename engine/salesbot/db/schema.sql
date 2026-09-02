-- Журнал заявок. Одна строка — один код квиза.
--
-- Накатывается сколько угодно раз подряд: всё через IF NOT EXISTS.
--   psql "$DB_URL" -f db/schema.sql
--
-- ПЕРСОНАЛЬНЫЕ ДАННЫЕ: тут телефон, имя и ответ про размер дела. База слушает
-- только 127.0.0.1 (см. db/docker-compose.yml), наружу порт не выставляется,
-- дамп бэкапа держи рядом с базой, а не в репозитории.

CREATE TABLE IF NOT EXISTS leads (
    quiz_id    text        PRIMARY KEY,          -- код квиза, он же ключ карточки-файла
    name       text        NOT NULL DEFAULT '',
    phone      text        NOT NULL DEFAULT '',
    klass      text        NOT NULL DEFAULT '',  -- класс-диагноз из первого ответа
    track      text        NOT NULL DEFAULT '',  -- дорожка оффера, человеку не называется
    answers    jsonb       NOT NULL DEFAULT '{}'::jsonb,   -- {"Q1": "...", "Q2": "..."}
    funnel     text        NOT NULL DEFAULT '',  -- откуда пришёл: имя воронки
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Выгрузка «кто пришёл за сегодня» — самый частый запрос владельца.
CREATE INDEX IF NOT EXISTS leads_created_at_idx ON leads (created_at DESC);

-- Примечание: живой тест исполняет этот файл целиком одним execute.
-- Держи файл валидным SQL без psql-специфичных команд (вроде \connect).
