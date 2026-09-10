# Процесс разработки

## Ветки

| Ветка | Назначение |
|---|---|
| `main` | Стабильная. Только через merge из `develop` при релизе. Прямые коммиты запрещены. |
| `develop` | Основная рабочая ветка. Сюда идут все новые фичи и фиксы. |
| `feature/имя` | Ветки для отдельных фич. Мёрджатся в `develop`. |
| `fix/имя` | Ветки для фиксов. Мёрджатся в `develop`. |
| `hotfix/имя` | Критичные фиксы. Мёрджатся сразу в `main` и обратно в `develop`. |

```
main   ←──────────────────────── merge при релизе ───────────────────
         ↑                                                            ↑
develop ─┴──── feature/xxx ────┴──── fix/yyy ────┴──── hotfix/zzz ──┘
```

## Флоу работы

### Обычная фича
```bash
git checkout develop
git pull
git checkout -b feature/window-capture
# ... работа ...
git push -u origin feature/window-capture
# merge в develop (можно напрямую если один разработчик)
git checkout develop && git merge feature/window-capture
git push
```

### Релиз
```bash
git checkout develop && git pull
# убедиться что всё работает, потом:
git checkout main
git merge develop
git push
./release.sh 1.2.3 "Описание изменений"
# release.sh сам создаёт коммит с версией и GitHub Release
git checkout develop
git merge main   # синхронизировать version.py обратно
```

### Хотфикс (критичный баг на проде)
```bash
git checkout main
git checkout -b hotfix/audio-crash
# ... фикс ...
git checkout main && git merge hotfix/audio-crash
./release.sh 1.0.1 "Исправлен крэш при захвате звука"
git checkout develop && git merge main
```

## Changelog

Ведётся автоматически через описание GitHub Release (`./release.sh <version> "<текст>"`).

Формат описания изменений:
- `feat:` — новая фича
- `fix:` — исправление бага
- `perf:` — улучшение производительности
- `chore:` — инфраструктура, зависимости

Примеры:
```
./release.sh 1.1.0 "feat: захват звука через WASAPI Process Loopback"
./release.sh 1.0.1 "fix: исправлен крэш при свёрнутом окне игры"
```

## Версионирование

[Семантическое версионирование](https://semver.org/lang/ru/): `MAJOR.MINOR.PATCH`

- `PATCH` — багфиксы, не ломают совместимость
- `MINOR` — новые фичи, не ломают совместимость
- `MAJOR` — ломающие изменения (меняется формат config, протокол и т.п.)

Версия живёт в одном месте: `src/version.py` → `__version__`.
`release.sh` патчит её автоматически.
