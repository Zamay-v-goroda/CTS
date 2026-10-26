import os
import sys
import time
import argparse
from parser import CTSParser
from median_partition import MedianPartitionCTS
from delay_calc import DelayCalculator
from optimizer import CTSOptimizer
from validator import CTSValidator
from visualizer import CTSVisualizer

class RunLog:
    """Собирает ошибки и предупреждения запуска и сохраняет их в файл."""
    def __init__(self, path):
        self.path = path
        self.lines = []

    def add(self, level, msg):
        self.lines.append(f"[{level}] {msg}")

    def finish(self, code=0):
        """Записывает журнал (если он не пуст; пустой журнал удаляет старый файл) и завершает работу."""
        if self.lines:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write("\n".join(self.lines) + "\n")
            print(f"-> Ошибки и предупреждения сохранены в: {self.path}")
        elif os.path.exists(self.path):
            os.remove(self.path)
        if code:
            sys.exit(code)

def print_divider():
    print("-" * 60)

def main():
    arg_parser = argparse.ArgumentParser(description="CTS Compiler Tool")
    arg_parser.add_argument("--input", type=str, default=os.path.join("data", "input.json"), 
                            help="Путь к входному JSON файлу конфигурации")
    arg_parser.add_argument("--output_dir", type=str, default="data", 
                            help="Папка для сохранения выходных файлов (отчетов и графиков)")
    arg_parser.add_argument("--name", type=str, default="", 
                            help="Префикс имени для выходных файлов (например, test_easy)")
    
    args = arg_parser.parse_args()
    input_file = args.input
    
    # ИСПРАВЛЕНО: Форматируем имена строго под маску clock_tree_test_easy.png и output_test_easy.txt
    prefix = f"_{args.name}" if args.name else ""
    output_file = os.path.join(args.output_dir, f"output{prefix}.txt")
    image_file = os.path.join(args.output_dir, f"clock_tree{prefix}.png")
    
    os.makedirs(args.output_dir, exist_ok=True)
    log = RunLog(os.path.join(args.output_dir, f"errors{prefix}.log"))
    
    if not os.path.exists(input_file):
        print(f"[Ошибка] Пожалуйста, положите входной JSON-файл по пути: {input_file}")
        log.add("ОШИБКА", f"Входной файл не найден: {input_file}")
        log.finish(1)
        
    print_divider()
    print("=== ЗАПУСК СИНТЕЗА ЦЕПЕЙ СИНХРОНИЗАЦИИ (CTS) ===")
    print_divider()
    
    start_time = time.time()
    
    # 1. Шаг Парсинга
    print("[1/6] Чтение и валидация конфигурации JSON...")
    parser = CTSParser(input_file)
    try:
        clock_source, sinks, buffers, wire_model, tree_params, opt_params = parser.parse()
        print(f"-> Успешно загружено потребителей (Sinks): {len(sinks)}")
        print(f"-> Доступно типов буферов: {len(buffers)}")
        print(f"-> Выбранная модель проводов: {wire_model.model.upper()}")
        for w in parser.warnings:
            print(f"-> [Замечание] {w}")
            log.add("ПРЕДУПРЕЖДЕНИЕ", w)
    except Exception as e:
        print(f"[КРИТИЧЕСКАЯ ОШИБКА ВАЛИДАЦИИ JSON]: {e}")
        log.add("ОШИБКА", f"Валидация JSON: {e}")
        log.finish(1)
        
    # 2. Шаг Синтеза дерева
    print("[2/6] Запуск рекурсивного медианного разбиения...")
    try:
        cts_engine = MedianPartitionCTS(
            clock_source, sinks, buffers, wire_model, tree_params,
            grid_size=parser.grid_size,
            chip_width=parser.chip_width,
            chip_height=parser.chip_height,
        )
        root_node = cts_engine.build_tree()
    except ValueError as e:
        print(f"[КРИТИЧЕСКАЯ ОШИБКА СИНТЕЗА]: {e}")
        log.add("ОШИБКА", f"Синтез: {e}")
        log.finish(1)
    print("-> Топология дерева успешно построена.")

    # 3. Первичный расчет задержек
    print("[3/6] Расчет первоначальных задержек...")
    calculator = DelayCalculator(wire_model)
    initial_delays, initial_skew = calculator.calculate(root_node)
    print(f"-> Исходный разброс задержек (Initial Skew): {initial_skew:.4f} нс")
    
    # 4. Постоптимизация (Wire Snaking)
    print(f"[4/6] Оптимизация дерева (цель: {opt_params['objective']}"
          f"{', ' + opt_params['secondary_objective'] if opt_params['secondary_objective'] else ''})...")
    optimizer = CTSOptimizer(wire_model, opt_params, tree_params,
                             candidate_buffers=cts_engine.eligible_buffers)
    final_delays, final_skew = optimizer.optimize(root_node)
    print(f"-> Финальный разброс задержек (Final Skew): {final_skew:.4f} нс")
    print(f"-> Итераций: {optimizer.iterations}, заменено буферов: {optimizer.buffers_resized}, "
          f"удлинено проводов: {optimizer.edges_snaked}")
    
    # 5. Верификация результатов (Валидатор дерева)
    print("[5/6] Верификация физических и временных ограничений...")
    validator = CTSValidator(
        max_fanout=int(tree_params.get("max_fanout", 4)),
        max_depth=int(tree_params.get("max_depth", 10)),
        chip_width=parser.chip_width,
        chip_height=parser.chip_height,
        grid_size=parser.grid_size,
        available_buffers=buffers,
        min_buffer_input_cap=float(tree_params.get("min_buffer_input_cap", 0.0))
    )
    is_valid, errors, warnings = validator.validate(root_node, len(sinks), final_delays)
    
    for w in warnings:
        log.add("ПРЕДУПРЕЖДЕНИЕ", w)
    for err in errors:
        log.add("ОШИБКА", err)

    # Время работы алгоритма фиксируется ДО построения графика (отрисовка в него не входит)
    execution_time = time.time() - start_time
    print(f"-> Время работы алгоритма (без построения графика): {execution_time:.4f} сек.")
    
    # ПЕРЕНЕСЕНО НАВЕРХ: Отрисовка полученной топологии выполняется ДО блокировки.
    # Это гарантирует обновление .png файлов при каждом запуске пайплайна для визуального дебага.
    print("[6/6] Генерация графического отчета...")
    try:
        visualizer = CTSVisualizer(parser.chip_width, parser.chip_height)
        visualizer.plot_tree(root_node, save_path=image_file)
    except Exception as e:
        print(f"-> [Предупреждение] Не удалось построить график: {e}")
            
    # Проверка строгого запрета на запись текстовых отчетов при ошибках
    if not is_valid:
        print_divider()
        print(f"-> [КРИТИЧЕСКАЯ ОШИБКА] Дерево синхронизации НАГЛЯДНО НАРУШИЛО ТРЕБОВАНИЯ ТЗ! ({len(errors)} ошибок)")
        for err in errors:
            print(f"   ! {err}")
        print("\n[БЛОКИРОВКА] Запись текстового отчета отменена ради целостности данных проекта.")
        print("-> [ВНИМАНИЕ] График топологии сохранен для анализа геометрии ошибок.")
        print_divider()
        log.finish(1)  # Завершаем работу с ошибкой после отрисовки графика

    # Целевой разброс не достигнут: предупреждение, отчет не записывается.
    # Проверка применяется, если skew входит в цели оптимизации.
    skew_is_goal = "min_skew" in (opt_params["objective"], opt_params["secondary_objective"])
    if skew_is_goal and final_skew > opt_params["skew_target"] + 1e-12:
        print_divider()
        print(f"-> [ПРЕДУПРЕЖДЕНИЕ] Целевой разброс задержек не достигнут: "
              f"Skew = {final_skew:.4f} нс > Skew Target = {opt_params['skew_target']} нс "
              f"(итераций: {optimizer.iterations}).")
        print("[БЛОКИРОВКА] Текстовый отчет не записан. График топологии сохранен для анализа.")
        print_divider()
        log.add("ПРЕДУПРЕЖДЕНИЕ",
                f"Целевой разброс не достигнут: Skew = {final_skew:.4f} нс > Skew Target = "
                f"{opt_params['skew_target']} нс (итераций: {optimizer.iterations}). Отчет не записан.")
        log.finish(2)

    # Запись текстового отчета (Выполняется ТОЛЬКО если дерево полностью валидно)
    print_divider()
    print(f"Сохранение результатов в отчет: {output_file}...")
    
    with open(output_file, "w", encoding="utf-8") as out:
        out.write("=== РЕЗУЛЬТАТЫ СИНТЕЗА ЦЕПЕЙ СИНХРОНИЗАЦИИ (CTS) ===\n")
        out.write("Статус валидации: УСПЕШНО\n")
        out.write(f"Время работы алгоритма (без построения графика): {execution_time:.4f} сек.\n")
        out.write(f"Максимальная задержка: {max(final_delays.values()):.4f} нс\n")
        out.write(f"Минимальная задержка: {min(final_delays.values()):.4f} нс\n")
        out.write(f"Разброс задержек (Skew): {final_skew:.4f} нс\n")
        out.write(f"Целевой разброс (Skew Target): {opt_params['skew_target']} нс\n")
        out.write(f"Цель оптимизации: {opt_params['objective']}"
                  f"{', вторичная: ' + opt_params['secondary_objective'] if opt_params['secondary_objective'] else ''}\n")
        out.write(f"Суммарная ёмкость дерева (оценка мощности): {optimizer.final.get('power', 0.0):.2f} фФ "
                  f"(до оптимизации: {optimizer.initial.get('power', 0.0):.2f} фФ)\n")
        out.write(f"Итераций оптимизации: {optimizer.iterations}; заменено буферов: {optimizer.buffers_resized}; "
                  f"удлинено проводов: {optimizer.edges_snaked}\n")

        config_warnings = parser.warnings
        if config_warnings:
            out.write(f"\nЗамечания конфигурации ({len(config_warnings)}):\n")
            for warn in config_warnings:
                out.write(f" * {warn}\n")

        if warnings:
            out.write(f"\nЗамечания верификации ({len(warnings)}):\n")
            for warn in warnings:
                out.write(f" * {warn}\n")
        
        out.write("\n--- ЗАДЕРЖКИ ДО ТЕРМИНАЛОВ (SINKS) ---\n")
        sorted_sinks = sorted(final_delays.items(), key=lambda x: int(x[0][1:]) if x[0][1:].isdigit() else x[0])
        for s_id, delay in sorted_sinks:
            out.write(f"Терминал {s_id}: задержка = {delay:.4f} нс\n")
            
        out.write("\n--- ТОПОЛОГИЯ ДЕРЕВА (СПИСОК СОЕДИНЕНИЙ) ---\n")
        def dump_topology(node):
            for child in node.children:
                out.write(
                    f"Узел {node.id} ({node.node_type}) [{node.x}, {node.y}] -> "
                    f"Узел {child.id} ({child.node_type}) [{child.x}, {child.y}] | "
                    f"Длина провода: {child.wire_length_to_parent:.2f}\n"
                )
                dump_topology(child)
        dump_topology(root_node)

    log.finish(0)
    print("=== CTS КОМПИЛЯТОР УСПЕШНО ЗАВЕРШИЛ РАБОТУ ===")
    print_divider()

if __name__ == "__main__":
    main()
