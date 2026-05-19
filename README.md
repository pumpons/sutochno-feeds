# sutochno-feeds

Конструктор кастомных рекламных фидов для Яндекс.Директа на базе исходного фида sutochno.ru.

## Что делает

1. Качает исходный CSV (Google Hotel Ads формат) с `static.sutochno.ru`.
2. Грузит в SQLite, нормализует цены и оценки.
3. По описаниям сегментов из `config/segments.yaml` фильтрует объекты.
4. Рендерит для каждого свою картинку (HTML-шаблон + Playwright): фото + лого/рамка/бейдж/цена/город.
5. Складывает картинки в `output/images/`, выходной CSV (тот же Google Hotel формат, но с заменённым `Image URL`) — в `output/feeds/`.
6. GitHub Actions раз в сутки публикует всё в ветку `gh-pages` → доступно по `https://<user>.github.io/sutochno-feeds/feeds/<segment>.csv`.

## Локальный запуск

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/playwright install chromium

.venv/bin/python -m src.pipeline                    # полный прогон
.venv/bin/python -m src.pipeline --skip-ingest      # без перекачки исходника
.venv/bin/python -m src.pipeline --segment sochi-premium
```

## Структура

```
src/
  ingest.py        # source CSV → SQLite
  render.py        # Playwright + Jinja2, кэш по хэшу
  build_feed.py    # фильтр сегмента → render → выходной CSV
  pipeline.py      # оркестратор
config/
  segments.yaml    # описание сегментов и фильтров
  templates/*.html # HTML-шаблоны креативов
output/
  images/          # отрендеренные PNG (кэшируются между запусками)
  feeds/           # готовые CSV для подключения в Директ
.github/workflows/
  daily.yml        # cron 03:00 MSK + публикация в gh-pages
```

## Добавить новый сегмент

В `config/segments.yaml` добавить блок:

```yaml
- id: spb-budget
  filter:
    destination_in: ["Санкт-Петербург"]
    max_price: 3000
  template: premium_gold      # или новый шаблон в config/templates/
```

## После первого деплоя

В `config/segments.yaml` заменить `EXAMPLE` в `base_image_url` на реальный GitHub Pages хост, например `https://username.github.io/sutochno-feeds/images`.
