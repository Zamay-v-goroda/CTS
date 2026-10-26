# CTS — синтез дерева синхронизации (MMM)

Инструмент **Clock Tree Synthesis**: по JSON-описанию кристалла строит дерево тактовой сети методом **MMM (Method of Means and Medians)**, считает задержки, выравнивает skew (wire snaking) и сохраняет отчёт с топологией.

## Назначение

Компилятор принимает источник такта, набор потребителей (sinks), библиотеку буферов и модель провода. Дальше:

1. читает и проверяет входной JSON;
2. рекурсивно разбивает sinks по медиане и вставляет буферы;
3. считает задержки (Elmore или линейная модель);
4. при необходимости удлиняет быстрые ветви (snaking), чтобы снизить разброс задержек;
5. проверяет fanout, глубину, сетку и границы чипа;
6. пишет текстовый отчёт и PNG **только если дерево валидно**.

Цель оптимизации по умолчанию — **минимизация skew** (целевое значение задаётся во входе, обычно `0.05` нс).

## Стек

| Компонент | Использование |
|-----------|----------------|
| **Python 3** | язык проекта (без отдельного `requirements.txt`) |
| **Pydantic** | строгая схема входного JSON (`source/contracts.py`) |
| **dataclasses** | рабочие модели узлов дерева (`source/models.py`) |
| **matplotlib** | отрисовка топологии |
| **стандартная библиотека** | JSON, argparse, subprocess |

Внешние зависимости для запуска: `pydantic`, `matplotlib`.

```bash
pip install pydantic matplotlib
```

## Структура репозитория

```
CTS/
├── source/                 # ядро компилятора
├── tests/                  # входные JSON-кейсы
├── results/                # отчёты и графики после прогона
├── run_tests.py            # пакетный запуск всех (или одного) тестов
├── test_generator.py       # генерация случайных кейсов
└── build/                  # зарезервировано под скрипты сборки
```

### `source/` — алгоритм

| Файл | Роль |
|------|------|
| `main.py` | точка входа: пайплайн parse → build → delay → optimize → validate → report |
| `contracts.py` | Pydantic-схема входа: `design`, `clock_source`, `sinks`, `buffers`, `wire_model`, `tree_params`, `optimization` |
| `parser.py` | загрузка JSON, привязка координат к сетке, сбор объектов для синтеза |
| `models.py` | `ClockSource`, `Sink`, `BufferType`, `WireModel`, `ClockNode` |
| `median_partition.py` | MMM: медианное разбиение, выбор оси, вставка буферов |
| `delay_calc.py` | задержки до sinks и skew (Elmore / linear) |
| `optimizer.py` | итеративный wire snaking до `skew_target` |
| `validator.py` | связность, fanout, глубина, сетка, границы чипа |
| `visualizer.py` | PNG дерева на поле кристалла |

### Остальные каталоги

| Путь | Роль |
|------|------|
| `tests/*.json` | готовые сценарии: `test_easy` (10 sinks), `test_medium` (100), `test_hard` (500), `test_stress` (1000) |
| `results/output_*.txt` | задержки, skew, список рёбер дерева |
| `results/clock_tree_*.png` | визуализация топологии |
| `run_tests.py` | прогон `source/main.py` по файлам из `tests/`; **перед запуском очищает `results/`** |
| `test_generator.py` | пересоздаёт четыре JSON в `tests/` со случайными координатами sinks |

Входной JSON описывает размер чипа и `grid_size`, источник такта, sinks, библиотеку буферов (`standard` / `mid` / `large`), модель провода и параметры дерева (`max_fanout`, `max_depth`, критерий разбиения, snaking).

## Быстрый запуск

Из корня репозитория:

```bash
# все кейсы из tests/ → results/
python run_tests.py

# один кейс
python run_tests.py --case test_easy.json

# перегенерировать входные JSON
python test_generator.py

# напрямую, без пакетного раннера
python source/main.py --input tests/test_easy.json --output_dir results --name test_easy
```

После успешного прогона смотрите `results/output_test_easy.txt` и `results/clock_tree_test_easy.png`.

Если дерево нарушает ограничения (fanout, глубина, сетка и т.д.), компилятор завершается с ошибкой и **не пишет** отчёт и график.
