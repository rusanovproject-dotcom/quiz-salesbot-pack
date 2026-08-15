#!/usr/bin/env bash
# check-pack.sh — гейт перед публикацией.
#
# Ловит то, что улетает в открытый доступ незаметно: токены, ключи, телефоны,
# карточки людей, абсолютные пути с чужой машины.
#
#   bash scripts/check-pack.sh            # проверить репозиторий
#   bash scripts/check-pack.sh --names "Имя,Фамилия"   # плюс поиск конкретных имён
#
# Вернул блокеры — не публикуем. Без обсуждений.

# Без pipefail намеренно: grep -q закрывает пайп раньше времени, xargs получает
# SIGPIPE, и при включённом pipefail проверка молча возвращает «чисто» при живом
# секрете в репозитории. Проверено канарейкой — гейт врал.
set -u
cd "$(dirname "$0")/.." || exit 1

RED=$'\033[31m'; YEL=$'\033[33m'; GRN=$'\033[32m'; OFF=$'\033[0m'
BLOCKERS=0
WARNINGS=0

NAMES=""
if [ "${1:-}" = "--names" ]; then NAMES="${2:-}"; fi

block() { echo "${RED}БЛОКЕР${OFF}  $1"; BLOCKERS=$((BLOCKERS+1)); }
warn()  { echo "${YEL}СМОТРИ${OFF}  $1"; WARNINGS=$((WARNINGS+1)); }
ok()    { echo "${GRN}ок${OFF}      $1"; }

# что смотрим: файлы под контролем версий, кроме самого гейта
files() {
  git ls-files 2>/dev/null | grep -v '^scripts/check-pack.sh$' || true
}

echo "── Секреты ───────────────────────────────────────────"

# Токены и ключи по форме, а не по имени переменной
HIT_VK=$(files | xargs grep -lE 'vk1\.a\.[A-Za-z0-9_-]{50,}' 2>/dev/null)
if [ -n "$HIT_VK" ]; then block "токен ВКонтакте: $(echo "$HIT_VK" | tr '\n' ' ')"
else ok "токена ВКонтакте нет"; fi

HIT_TG=$(files | xargs grep -lE '[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}' 2>/dev/null)
if [ -n "$HIT_TG" ]; then block "токен Telegram: $(echo "$HIT_TG" | tr '\n' ' ')"
else ok "токена Telegram нет"; fi

HIT_KEY=$(files | xargs grep -lE 'sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}' 2>/dev/null)
if [ -n "$HIT_KEY" ]; then block "ключ API или токен GitHub: $(echo "$HIT_KEY" | tr '\n' ' ')"
else ok "ключей API нет"; fi

# .env под контролем версий
HIT_ENV=$(files | grep -E '(^|/)(config\.env|\.env)$')
if [ -n "$HIT_ENV" ]; then
  block "config.env или .env под контролем версий: $(echo "$HIT_ENV" | tr '\n' ' ')"
else ok "файлы с секретами не отслеживаются"; fi

echo
echo "── Персональные данные ───────────────────────────────"

# Карточки людей
HIT_LEADS=$(files | grep -E '(^|/)(leads|quiz-leads)/')
if [ -n "$HIT_LEADS" ]; then
  block "каталог с карточками людей в репозитории"
else ok "карточек людей нет"; fi

# Телефоны в тексте
PHONES=$(files | xargs grep -nE '\+7[0-9]{10}|8[0-9]{10}' 2>/dev/null \
         | grep -vE '1234567|0000000|9990001122' || true)
if [ -n "$PHONES" ]; then
  warn "похоже на телефон:"; echo "$PHONES" | head -3 | sed 's/^/         /'
else ok "телефонов нет"; fi

# Живые ссылки на конкретные сообщества и ботов
LIVE=$(files | xargs grep -nE 'vk\.me/club[0-9]{4,}|t\.me/[a-zA-Z_]{4,}' 2>/dev/null \
       | grep -viE 'example|твой_бот|clubХ|XXXX' || true)
if [ -n "$LIVE" ]; then
  warn "ссылка на конкретное сообщество или бота:"; echo "$LIVE" | head -5 | sed 's/^/         /'
else ok "живых ссылок нет"; fi

# Имена, если переданы
if [ -n "$NAMES" ]; then
  # Корни слов считаем питоном: обрезка кириллицы средствами bash врёт.
  # Корень вместо целого имени — чтобы падежи тоже попадались («Иванову», «Ивановым»).
  # Режем только последнюю букву и не короче пяти символов: корень «Стей» ловится
  # внутри слова «зависимостей», и гейт начинает врать блокерами на пустом месте
  ROOTS=$(python3 -c "
import sys
for n in sys.argv[1].split(','):
    n = n.strip()
    if n:
        print(n[:-1] if len(n) >= 6 else n)
" "$NAMES")
  FOUND=""
  while IFS= read -r root; do
    [ -z "$root" ] && continue
    HIT=$(files | xargs grep -linF "$root" 2>/dev/null || true)
    [ -n "$HIT" ] && FOUND="$FOUND
  «$root...» → $(echo "$HIT" | tr '\n' ' ')"
  done <<< "$ROOTS"
  if [ -n "$FOUND" ]; then
    block "найдены имена:$FOUND"
  else ok "переданных имён нет"; fi
fi

echo
echo "── Чистота пакета ────────────────────────────────────"

# Абсолютные пути с чужой машины
PATHS=$(files | xargs grep -lE '/Users/[a-zA-Z0-9._-]+/|/home/[a-zA-Z0-9._-]+/' 2>/dev/null \
        | grep -v 'check-pack' || true)
if [ -n "$PATHS" ]; then
  warn "абсолютный путь с чьей-то машины: $(echo "$PATHS" | tr '\n' ' ')"
else ok "абсолютных путей нет"; fi

# IP-адреса
IPS=$(files | xargs grep -nE '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b' 2>/dev/null \
      | grep -vE '127\.0\.0\.1|0\.0\.0\.0|198\.51\.100|203\.0\.113|192\.0\.2' || true)
if [ -n "$IPS" ]; then
  warn "адрес сервера в коде:"; echo "$IPS" | head -3 | sed 's/^/         /'
else ok "адресов серверов нет"; fi

# Обещанное в README против того, что есть
for d in playbook engine/quiz engine/salesbot examples scripts; do
  [ -d "$d" ] || block "README обещает каталог $d, а его нет"
done
ok "структура на месте"

# Навыки с описанием
for s in .claude/skills/*/SKILL.md; do
  [ -f "$s" ] || continue
  head -5 "$s" | grep -q '^description:' || block "у навыка $s нет описания в заголовке"
done
ok "навыки описаны"

echo
echo "──────────────────────────────────────────────────────"
if [ "$BLOCKERS" -gt 0 ]; then
  echo "${RED}НЕ ПУБЛИКОВАТЬ${OFF}: блокеров — $BLOCKERS, замечаний — $WARNINGS"
  exit 1
fi
if [ "$WARNINGS" -gt 0 ]; then
  echo "${YEL}МОЖНО, НО ГЛЯНЬ${OFF}: замечаний — $WARNINGS"
  exit 0
fi
echo "${GRN}ЧИСТО${OFF} — можно публиковать"
