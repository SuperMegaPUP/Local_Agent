# Механические гейты (gateway выполняет АВТОМАТИЧЕСКИ после coder/qa)
1. npx tsc --noEmit          (типы)
2. npm run lint              (eslint)
3. npm run test:ci           (jest, forceExit)
Любой FAIL -> карточка возвращается в todo с комментарием gateway.
Модельное ревью (code-reviewer) идёт ПОСЛЕ механических гейтов, не вместо.
