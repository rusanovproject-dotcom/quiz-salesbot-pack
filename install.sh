#!/usr/bin/env bash
# Квиз + ИИ-продажник — установка конструктора в AI-офис на Claude Code, в один заход.
#
# Usage: ./install.sh <путь-к-офису>
#   <путь-к-офису>  корень твоего AI-офиса (папка должна существовать)
#
# Что делает: кладёт конструктор в engines/quiz-funnel/ твоего офиса и ставит
# навык-диспетчер в .claude/skills/ — после этого офис понимает фразы
# «собери квиз», «квиз-воронка», «квизодел».
#
# Повторный запуск безопасен: твои артефакты (PROJECT.md, заполненный конфиг
# квиза, персона бота, свой список анти-генерика, свои персонажи полигона)
# не перезаписываются — обновляются только движок, навыки и методология.
set -euo pipefail

PACK="$(cd "$(dirname "$0")" && pwd)"
WS=""

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*)        echo "неизвестный флаг: $1" >&2; exit 1 ;;
    *)         WS="$1"; shift ;;
  esac
done

say()  { printf '\033[36m▸ %s\033[0m\n' "$1"; }
ok()   { printf '\033[32m✓ %s\033[0m\n' "$1"; }
warn() { printf '\033[33m! %s\033[0m\n' "$1"; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$1" >&2; exit 1; }

if [ -z "$WS" ]; then
  echo ""
  die "укажи корень своего AI-офиса: ./install.sh <путь-к-офису>  (например ./install.sh ~/my-office)"
fi
[ -d "$WS" ] || die "папки $WS не существует. Проверь путь — сам офис я не создаю."
WS="$(cd "$WS" && pwd)"

DEST="$WS/engines/quiz-funnel"

echo ""
say "Квиз + ИИ-продажник → офис: $WS"

# 0. Структура офиса
for d in "engines" ".claude/skills"; do
  [ -d "$WS/$d" ] || { warn "нет $d — создаю"; mkdir -p "$WS/$d"; }
done

# 1. Личные артефакты — копируются только если их ещё нет.
#    Всё остальное — канон пака, перезаписывается при обновлении.
PERSONAL="PROJECT.md
engine/quiz/quiz.config.js
engine/salesbot/persona/persona.md
engine/salesbot/persona/connections.md
engine/salesbot/config.env
engine/salesbot/tools/antigeneric-custom.txt
engine/salesbot/tools/personas.json"

say "Ставлю конструктор → engines/quiz-funnel/"
mkdir -p "$DEST"

# 1a. Канон: всё дерево пака, кроме git-служебного и личных файлов
( cd "$PACK" && find . -type f \
    ! -path "./.git/*" ! -name ".DS_Store" ! -name "install.sh" \
    ! -path "./projects/*" ! -name ".env" ! -name "config.env" ! -name "*.pem" \
    ! -path "*/leads/*" ! -path "*/quiz-leads/*" ! -name "*.leads.json" \
    ! -path "./examples/*/config.env" | sort ) | while IFS= read -r rel; do
  rel="${rel#./}"
  if printf '%s\n' "$PERSONAL" | grep -qx "$rel"; then
    if [ -e "$DEST/$rel" ]; then
      continue                       # личное уже живёт — не трогаем
    fi
  fi
  mkdir -p "$DEST/$(dirname "$rel")"
  cp "$PACK/$rel" "$DEST/$rel"
done
ok "конструктор на месте (личные файлы, если были, не тронуты)"

# 2. Навык-диспетчер в офис
say "Ставлю навык-диспетчер → .claude/skills/quiz-funnel/"
mkdir -p "$WS/.claude/skills/quiz-funnel"
cat > "$WS/.claude/skills/quiz-funnel/SKILL.md" << 'SKILL'
---
name: quiz-funnel
description: Квиз-воронка с ИИ-продажником — конструктор в engines/quiz-funnel. Ведёт владельца от интервью по нише до диагностического квиза, приёма заявок в базу и бота-продажника в Telegram или ВК. Используй, когда просят собрать квиз или воронку с квизом. Триггеры — "собери квиз", "квиз-воронка", "квизодел", "ИИ-продажник", "лид-квиз", "заявки с квиза", "диагностический квиз".
---

# Квиз-воронка — диспетчер конструктора

Конструктор живёт в `engines/quiz-funnel/` этого офиса. Ты — диспетчер: не собираешь сам, а ведёшь по конструктору.

1. Прочитай `engines/quiz-funnel/START.md` — там цепочка фаз и три правила. Веди человека по ней по порядку.
2. Навыки фаз лежат в `engines/quiz-funnel/.claude/skills/<имя>/SKILL.md` — на каждой фазе читай нужный файл и исполняй его как инструкцию.
3. Все артефакты сборки (PROJECT.md, конфиг квиза, персона бота) — внутри `engines/quiz-funnel/`, в корень офиса ничего не выноси.
4. Законы пака — закон и для тебя: не собирать до интервью · фазы по порядку · один вопрос за раз.

Хочет только часть (например «только квиз, без бота») — фазы 0–1 обязательны всё равно, дальше по его выбору.
SKILL
ok "навык quiz-funnel установлен"

echo ""
ok "Готово. Открой Claude Code в корне офиса и скажи: «собери мне квиз-воронку»"
echo "   Обновление пака: запусти этот же установщик ещё раз."
