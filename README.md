# YouTube UA Dubbing

Автоматичне дублювання YouTube відео з англійської на українську мову.

## Пайплайн

```
YouTube URL → yt-dlp → Whisper (транскрипція) → Google Translate → edge-tts (синтез) → MP4
```

## Можливості

- Транскрипція аудіо через **Whisper large-v3** (OpenAI)
- Переклад через **Google Translate** (EN → UK)
- Синтез українського мовлення через **edge-tts** (Microsoft)
  - `uk-UA-PolinaNeural` — жіночий голос
  - `uk-UA-OstapNeural` — чоловічий голос
- Збірка фінального відео через **ffmpeg** з оригінальним відеорядом
- Генерація субтитрів у форматі `.srt`
- Веб-інтерфейс через **Gradio**

## Вимоги

- Python 3.9–3.12
- GPU (рекомендовано T4 або вище для Whisper)
- Інтернет-з'єднання (для Google Translate та edge-tts)

## Запуск у Google Colab

1. Відкрий `YouTube_UA_Dubbing.ipynb` у Google Colab
2. `Runtime → Change runtime type → T4 GPU`
3. Запускай клітинки по порядку (кроки 1–5)
4. Введи YouTube посилання у Gradio інтерфейсі та натисни **Запустити дублювання**

## Структура проєкту

```
YouTube_UA_Dubbing.ipynb   # Основний ноутбук
```

## Залежності

| Пакет | Призначення |
|---|---|
| `faster-whisper` | Транскрипція аудіо |
| `deep-translator` | Переклад EN → UK |
| `edge-tts` | Синтез українського мовлення |
| `yt-dlp` | Завантаження відео з YouTube |
| `pydub` | Обробка аудіо, таймінг фрагментів |
| `gradio` | Веб-інтерфейс |
| `ffmpeg` | Збірка фінального відео |

## Примітки

- **edge-tts** вимагає активного інтернет-з'єднання (хмарний сервіс Microsoft)
- Lip-sync не реалізовано — для цього потрібен окремий крок з Wav2Lip
- Безкоштовний Colab-сеанс: до 12 годин
