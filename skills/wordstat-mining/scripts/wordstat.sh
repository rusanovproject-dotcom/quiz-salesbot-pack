#!/usr/bin/env bash
# Wordstat через официальный Yandex Search API v2 (сервис поднят в Yandex Cloud).
# Частотность запросов, похожие фразы, распределение спроса по регионам.
#
# Ключ и folder_id: ~/.secrets/wordstat.json
#   { "api_key": "<API-ключ сервисного аккаунта>", "folder_id": "<id каталога>" }
#
# Откуда взять ключ и как читать вывод — SKILL.md рядом (skills/wordstat-mining/SKILL.md).
#
# Использование:
#   wordstat.sh top "пластиковые окна" [numPhrases=50] [--region ID[,ID...]]
#   wordstat.sh top "кухни на заказ" 100 --region 65
#   wordstat.sh regions "кухни на заказ" [--top N]      # где спрос по регионам
#
# ⚠️ БЕЗ --region частотность считается по ВСЕЙ России и завышена в разы.
#    Пример на живых цифрах: «кухни на заказ» — 76094 по РФ, 9763 Москва (213),
#    1090 Новосибирск (65). Разница федеральная/региональная — десятки раз.
#
# Частые geoId: Россия 225 · Москва 213 · СПб 2 · Новосибирск 65 · Екатеринбург 54
#   Казань 43 · Краснодар 35 · Н.Новгород 47 · Ростов-на-Дону 39 · Красноярск 62
#   Самара 51 · Уфа 172 · Челябинск 56 · Пермь 50 · Воронеж 193 · Волгоград 38
#   Владивосток 75 · Тюмень 55 · Иркутск 63 · Хабаровск 76 · Омск 66 · Саратов 194
#   Барнаул 197 · Ижевск 44 · Ярославль 16
# Своего города тут нет — гони `regions`: он печатает названия для этих же
# geoId, а незнакомые оставляет числом.

set -euo pipefail

SECRETS="${HOME}/.secrets/wordstat.json"
[ -f "$SECRETS" ] || { echo "нет $SECRETS — положи {api_key, folder_id}, см. SKILL.md" >&2; exit 1; }

API_KEY=$(python3 -c "import json;print(json.load(open('$SECRETS'))['api_key'])")
FOLDER=$(python3 -c "import json;print(json.load(open('$SECRETS'))['folder_id'])")

CMD="${1:-top}"
PHRASE="${2:-}"

# дефолты + разбор хвоста: позиционное число (numPhrases/top) и флаги
NUM=50
REGIONS=""
TOPN=15
shift 2 2>/dev/null || true
while [ $# -gt 0 ]; do
  case "$1" in
    --region|--regions) [ $# -ge 2 ] || { echo "--region требует значение: geoId (см. список в шапке)" >&2; exit 1; }
                        REGIONS="$2"; shift 2 ;;
    --top)              [ $# -ge 2 ] || { echo "--top требует число" >&2; exit 1; }
                        TOPN="$2"; shift 2 ;;
    ''|*[!0-9]*)        echo "неизвестный аргумент: $1" >&2; exit 1 ;;
    *)                  NUM="$1"; TOPN="$1"; shift ;;
  esac
done

[ -n "$PHRASE" ] || { echo "укажи фразу: wordstat.sh top \"фраза\" [N] [--region ID]" >&2; exit 1; }

api() {  # api <метод> <json-тело>
  # ключ уходит через config на файловом дескрипторе, а не аргументом:
  # аргументы curl видны любому соседу по серверу в `ps`
  curl -s --request POST \
    --config <(printf 'header = "Authorization: Api-Key %s"\n' "$API_KEY") \
    --header "Content-Type: application/json" \
    --data "$2" \
    "https://searchapi.api.cloud.yandex.net/v2/wordstat/$1"
}

case "$CMD" in
  top)
    BODY=$(PHRASE="$PHRASE" NUM="$NUM" FOLDER="$FOLDER" REGIONS="$REGIONS" python3 -c "
import json, os
b = {'phrase': os.environ['PHRASE'],
     'numPhrases': int(os.environ['NUM']),
     'folderId': os.environ['FOLDER']}
r = os.environ.get('REGIONS', '')
if r:
    b['regions'] = [x.strip() for x in r.split(',') if x.strip()]
print(json.dumps(b, ensure_ascii=False))")
    api topRequests "$BODY"
    ;;

  regions)
    BODY=$(PHRASE="$PHRASE" FOLDER="$FOLDER" python3 -c "
import json, os
print(json.dumps({'phrase': os.environ['PHRASE'],
                  'folderId': os.environ['FOLDER']}, ensure_ascii=False))")
    api regions "$BODY" | TOPN="$TOPN" python3 -c "
import json, os, sys
# Голый geoId ничего не говорит: по числу 40 город не узнать, а идти за
# справочником посреди разведки никто не будет. Ходовые подписываем здесь,
# остальные печатаем самим id — врать названием хуже, чем промолчать.
NAMES = {
    '225': 'Россия', '3': 'Центральный ФО', '1': 'Москва и область',
    '213': 'Москва', '2': 'Санкт-Петербург', '65': 'Новосибирск',
    '54': 'Екатеринбург', '43': 'Казань', '35': 'Краснодар',
    '47': 'Нижний Новгород', '39': 'Ростов-на-Дону', '62': 'Красноярск',
    '51': 'Самара', '172': 'Уфа', '56': 'Челябинск', '50': 'Пермь',
    '193': 'Воронеж', '38': 'Волгоград', '75': 'Владивосток', '55': 'Тюмень',
    '63': 'Иркутск', '76': 'Хабаровск', '66': 'Омск', '194': 'Саратов',
    '197': 'Барнаул', '44': 'Ижевск', '16': 'Ярославль',
}
d = json.load(sys.stdin)
rows = d.get('results', [])
if not rows:
    print(json.dumps(d, ensure_ascii=False, indent=1)); sys.exit(0)
rows.sort(key=lambda r: int(r.get('count', 0)), reverse=True)
print(f\"{'geoId':>8}  {'регион':<18}  {'показов/мес':>12}  {'affinity':>8}\")
for r in rows[:int(os.environ['TOPN'])]:
    gid = str(r.get('region', ''))
    print(f\"{gid:>8}  {NAMES.get(gid, gid):<18}  {int(r.get('count',0)):>12}  {r.get('affinityIndex',0):>8.0f}\")
print('\n# affinity >100 — в этом регионе спрос выше среднего по стране')
print('# регион = сам geoId — его нет в подписанных, посмотри в справочнике Яндекса')
"
    ;;

  *)
    echo "неизвестная команда: $CMD (есть: top, regions)" >&2
    exit 1
    ;;
esac
