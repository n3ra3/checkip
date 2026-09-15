# steam-capacity

Замер, сколько запросов к Steam Market выдерживает IP конкретной точки
(Northflank-аккаунт, VM Oracle) и сколько пользователей бота это даёт.

Не запускать на сервере, где с того же IP уже работает другой Steam-бот (pscheck).

## Режимы (`MODE`)

| MODE | Что делает | Время | Запросов к Steam |
|---|---|---|---|
| `ip` | только внешний IP и провайдер | секунды | 0 |
| `check` | + какой клиент пропускает Steam (urllib / curl) | ~10 с | 2 |
| `full` | + подъём частоты до 429, снятие лимита, удержание | ~40–90 мин | сотни |

Прочие переменные: `RATES`, `STAGE_MINUTES`, `SUSTAIN_MINUTES`,
`MAX_RECOVERY_MINUTES`, `LOTS`, `LABEL`, `KEEP_ALIVE`, `TG_BOT_TOKEN`, `TG_CHAT_ID`
(описание в начале `measure.py`).

## Northflank

1. Положить папку в GitHub-репозиторий.
2. Создать **Job → Manual job**, источник — репозиторий, сборка — `Dockerfile`.
3. Переменные: `MODE`, `LABEL` (например `nf-1`), по желанию `TG_BOT_TOKEN` и `TG_CHAT_ID`.
4. **Run job** → смотреть Logs. Итог — блок `=== Steam capacity ===` и строка `RESULT_JSON`.

Если Job недоступен на бесплатном плане — Service из того же репозитория с `KEEP_ALIVE=1`,
иначе после завершения сервис перезапустит тест по кругу. После теста сервис удалить.

## Oracle / любой Linux

```bash
sudo apt-get install -y curl python3
LABEL=oracle-a1 MODE=full nohup python3 measure.py > result.log 2>&1 &
tail -f result.log
```
