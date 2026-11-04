# infoboard

Информационный стенд курса: очередь лабораторных на проверку, ожидающиеся
исправления и статистика по студентам.

**Актуальный отчёт:** <https://intropy24.github.io/infoboard/>

Как это работает: GitHub Actions каждые 30 минут (`.github/workflows/refresh.yml`)
запускает `labs_review_queue.py --md` от имени преподавателя (PAT в секрете
`GH_PAT`, только чтение репо организации) и деплоит HTML на GitHub Pages.
Коммитов в репо робот не делает. README здесь — просто вывеска.

Локальный прогон (из корня репо):

```bash
pip install -r requirements.txt
GH_PAT=... python labs_review_queue.py --md   # отчёт на stdout
```
