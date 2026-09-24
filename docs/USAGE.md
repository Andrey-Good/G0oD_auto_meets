# Запуск и команды

## Установка на Windows 11

Нужны Python 3.11+, 64-битный GStreamer 1.24+ с плагинами WASAPI2/D3D11, whisper.cpp с `whisper-cli` и локальная модель GGML. Для русского нужна многоязычная модель, не вариант `.en`.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m auto_meets init --profile profiles/study.toml
```

Далее для краткости используется `auto-meets`. Полный эквивалент без активации окружения: `.\.venv\Scripts\python.exe -m auto_meets`. Можно также запускать `.\.venv\Scripts\auto-meets.exe`.

GStreamer бери из [официальных загрузок](https://gstreamer.freedesktop.org/download/). Установи Runtime с нужными плагинами. Укажи полные пути к `gst-launch-1.0.exe` и `gst-inspect-1.0.exe`, либо добавь каталог `bin` в PATH. Если Windows не находит DLL, проверь целостность установки и перезапусти терминал после изменения PATH. Python/GI и отдельный SDK проекту не нужны.

whisper.cpp бери из [официального репозитория](https://github.com/ggml-org/whisper.cpp) и [его релизов](https://github.com/ggml-org/whisper.cpp/releases). Укажи существующие `whisper-cli.exe` и `ggml-*.bin`. При сборке используй инструкции выбранного релиза: CUDA для совместимой NVIDIA, Vulkan для поддерживаемого устройства или CPU. Не смешивай DLL разных сборок. `use_gpu=false` добавляет `-ng`; `true` лишь не отключает ускорение, а не устанавливает CUDA/Vulkan.

Пример полей в `profiles/study.toml` (пути нужно заменить существующими):

```toml
[capture]
gst = 'C:/gstreamer/1.0/msvc_x86_64/bin/gst-launch-1.0.exe'
inspect = 'C:/gstreamer/1.0/msvc_x86_64/bin/gst-inspect-1.0.exe'

[asr]
executable = '../tools/whisper/whisper-cli.exe'
model = '../tools/whisper/models/ggml-small.bin'
language = 'ru'
```

**Измени соответствующие поля уже существующих секций**, не добавляй повторные `[capture]`/`[asr]`. TOML использует прямые слеши или одинарные строки для Windows-путей. Все относительные пути считаются от файла профиля, а не текущего терминала. Инструмент без `/` и `\` ищется в PATH.

```powershell
auto-meets doctor --profile profiles/study.toml
auto-meets demo
```

`doctor.ok=true` означает, что обнаружены инструменты/плагины и модель, а не что проверено оборудование или содержание записи. Затем нужна приёмка из `TESTING.md`. В отдельном `profiles/work.toml` можно поставить `report.kind="meeting"` и включить микрофон. Для собственного микрофона предпочтительны наушники, чтобы не записать звук колонок повторно.

## Рабочий сценарий

```powershell
auto-meets browser --profile profiles/study.toml --url "https://meeting.example/session"
auto-meets windows
```

После входа через локальный интерфейс возьми **настоящие** PID и HWND из результата. В следующем примере числа и адрес условные:

```powershell
auto-meets start --profile profiles/study.toml --title "Лекция по сетям" --pid 1234 --window 5678 --event-key "calendar:uid:2026-10-01T09:00:00Z" --consent
```

Возвращаемое поле `session` — полный путь папки. Во всех дальнейших командах используй именно его:

```powershell
auto-meets status --session "C:/project/data/SESSION"
auto-meets stop --session "C:/project/data/SESSION"
auto-meets status --session "C:/project/data/SESSION"
```

`start` ждёт до 20 секунд первоначального статуса (`--wait 0` не ждёт). `stop` по умолчанию не ждёт окончания ASR; `--wait 60` ждёт до 60 секунд. Даже после ожидания проверь фактический `phase`. Код возврата 2 означает ошибку/`needs_retry`/неудачный `doctor`; 0 означает успешное выполнение команды, **не обязательно завершение фоновой работы**. `summary_present` — наличие файла, не проверка его корректности.

Если `event_key` уже встречался в том же `storage.root`, возвращается прежняя сессия с `already_exists=true`. Чтобы начать отдельную попытку после обрыва, задай явный новый суффикс, например `:retry-1`. Не используй повторный `start` для дописывания аудио в старую сессию.

## Восстановление и повторное распознавание

```powershell
auto-meets stop --session "C:/project/data/SESSION"
auto-meets process --session "C:/project/data/SESSION" --profile profiles/study.toml
```

Если процесс ещё жив, дождись окончания или изучи `worker.log`; `process` не запускает вторую обработку поверх первой. При осиротевшем GStreamer/ASR команда `stop` остановит только проверенный процесс этой сессии. Затем `process` исправит WAV-заголовок, дочитает кадры из журнала и обработает аудио.

`--profile` у `process` меняет **только ASR-настройки**; тип встречи и пожелания остаются из исходного снимка профиля. Без него используются последние параметры распознавания. Успешные совпадающие блоки берутся из кэша, ошибочные повторяются. `--force` перераспознаёт даже успешные блоки. Смена `chunk_seconds` пересоздаёт производные блоки. Исходный WAV сохраняется. После изменения текста старый пересказ может стать неактуальным: обнови его по новой схеме и источникам.

Импорт готовой записи:

```powershell
auto-meets import-wav --profile profiles/study.toml --file "C:/recordings/lecture.wav" --title "Импорт лекции"
```

Принимается PCM WAV: mono, 16 бит, 16 кГц. Другие форматы предварительно преобразуй внешним инструментом, например:

```text
ffmpeg -i input.mp3 -ac 1 -ar 16000 -c:a pcm_s16le output.wav
```

FFmpeg не является зависимостью проекта. Он нужен только для такого внешнего преобразования. Импорт не извлекает кадры из видео и не угадывает таймлайн сторонних картинок.

## Пересказ и HTML

После записи уже существуют черновик `report.html`, `AGENT_BRIEF.md` и `summary.example.json`. Агент читает исходники, копирует схему в `summary.json` и заполняет её. `source_digest` копируется **без изменений** из текущей схемы. Ниже пример структуры, не готовый пересказ:

```json
{
  "source_digest": "ЗНАЧЕНИЕ ИЗ summary.example.json",
  "brief": [
    {"text": "Главный вывод из фактической речи.", "sources": ["c000000s0000"]}
  ],
  "sections": [
    {
      "title": "Название темы",
      "text": "Связное изложение.\n\nСледующий абзац.",
      "sources": ["c000000s0000"],
      "frames": ["f000000"]
    }
  ],
  "decisions": [],
  "tasks": [
    {
      "text": "Задание, которое действительно было названо.",
      "owner": "",
      "deadline": "",
      "sources": ["c000001s0000"]
    }
  ],
  "organization": [],
  "uncertainties": [
    {"text": "Что нужно уточнить по исходной записи.", "sources": ["c000001s0000"]}
  ]
}
```

Все указанные id должны реально существовать в этой сессии. В `brief`, `decisions`, `organization`, `uncertainties` элемент имеет ровно `text` и `sources`. Раздел добавляет `title` и `frames`, задание — `owner` и `deadline`. Уточнения могут не иметь источников, например замечание об отсутствующем контексте; остальные утверждения требуют ссылку на речь либо кадр. Текст — обычный, **не HTML и не Markdown**. Переносы строк сохраняются. Непустой пересказ требует хотя бы `brief` или `sections`; для `detail="brief"` обязательно подготовь `brief`.

```powershell
auto-meets render --session "C:/project/data/SESSION"
```

HTML открывается локально и не требует сервера. Ссылка времени перемещает позицию аудио, воспроизведение включается пользователем. Галерея и транскрипция сворачиваются. Отчёт `brief` скрывает развёрнутые разделы, но сохраняет поручения, организационные указания и доступ к оригиналам. Неотмеченная как неуверенная речь всё равно может содержать ошибки.

Для передачи отчёта скопируй/заархивируй **всю папку сессии**: HTML ссылается на соседние аудио, текст и изображения. Учитывай, что такая папка также содержит профиль и диагностические логи; не публикуй её без проверки приватности.
