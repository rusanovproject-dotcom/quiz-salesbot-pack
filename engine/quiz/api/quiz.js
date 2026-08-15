// Приём ответов квиза — тонкий прокси до твоего приёмника.
//
// Зачем прослойка: браузер открывает квиз по https, а приёмник обычно
// висит на сервере по http и на нестандартном порту. Напрямую браузер туда
// не пустит (смешанный контент). Прокси решает это: страница шлёт на свой же
// адрес /api/quiz, а сервер-серверу переправляет дальше.
//
// Адрес приёмника задаётся переменной окружения QUIZ_INTAKE_URL.
// Например: http://198.51.100.10:8087/quiz-submit
//
// Ответ приёмника {ok, quiz_id, klass, track, link} возвращаем странице как есть —
// из него берётся готовая ссылка на диалог с ботом.

const INTAKE_URL = process.env.QUIZ_INTAKE_URL;

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    res.status(405).json({ ok: false, error: 'method_not_allowed' });
    return;
  }
  if (!INTAKE_URL) {
    // Приёмник не настроен — не роняем прохождение квиза.
    // Страница уйдёт в запасной вариант: покажет код и ссылку на бота без кода.
    res.status(503).json({ ok: false, error: 'intake_not_configured' });
    return;
  }
  try {
    const body = typeof req.body === 'string' ? req.body : JSON.stringify(req.body || {});
    const upstream = await fetch(INTAKE_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
    });
    const text = await upstream.text();
    res.status(upstream.status);
    res.setHeader('Content-Type', 'application/json; charset=utf-8');
    res.send(text);
  } catch (e) {
    res.status(502).json({ ok: false, error: 'intake_unreachable' });
  }
}
